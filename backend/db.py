import os
import uuid
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "")


def get_conn():
    return psycopg2.connect(DATABASE_URL)


def _row(cur):
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in cur.description], row))


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    garage_name TEXT DEFAULT '',
                    subscription_status TEXT DEFAULT 'trial',
                    stripe_customer_id TEXT,
                    reset_token TEXT,
                    reset_token_expiry TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT DEFAULT '',
                    brand TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    price_min INTEGER DEFAULT 0,
                    price_max INTEGER DEFAULT 50000,
                    km_max INTEGER DEFAULT 200000,
                    year_min INTEGER DEFAULT 2010,
                    fuel TEXT DEFAULT '',
                    region TEXT DEFAULT 'France',
                    zip TEXT DEFAULT '',
                    radius_km INTEGER DEFAULT 0,
                    alert_hour INTEGER DEFAULT 8,
                    frequency TEXT DEFAULT 'daily',
                    active BOOLEAN DEFAULT TRUE,
                    last_scan TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vehicles (
                    id TEXT PRIMARY KEY,
                    alert_id TEXT NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT DEFAULT '',
                    price INTEGER DEFAULT 0,
                    km INTEGER DEFAULT 0,
                    year INTEGER DEFAULT 0,
                    brand TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    location TEXT DEFAULT '',
                    image_url TEXT DEFAULT '',
                    score INTEGER DEFAULT 0,
                    sent BOOLEAN DEFAULT FALSE,
                    favorited BOOLEAN DEFAULT FALSE,
                    hidden BOOLEAN DEFAULT FALSE,
                    found_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # Migrations for existing tables
            for col, definition in [
                ("last_scan", "TIMESTAMP"),
                ("favorited", "BOOLEAN DEFAULT FALSE"),
                ("hidden", "BOOLEAN DEFAULT FALSE"),
            ]:
                try:
                    if col in ("favorited", "hidden"):
                        cur.execute(f"ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS {col} {definition}")
                    else:
                        cur.execute(f"ALTER TABLE alerts ADD COLUMN IF NOT EXISTS {col} {definition}")
                except Exception:
                    pass
            try:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'starter'")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS zip TEXT DEFAULT ''")
                cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS radius_km INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS alert_hour INTEGER DEFAULT 8")
            except Exception:
                pass
        conn.commit()


# ── Users ──────────────────────────────────────────────────────────────────────

def create_user(email: str, password_hash: str, garage_name: str = '') -> str:
    uid = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (id, email, password_hash, garage_name) VALUES (%s, %s, %s, %s)",
                (uid, email, password_hash, garage_name)
            )
        conn.commit()
    return uid


def get_user_by_email(email: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            return _row(cur)


def get_user_by_id(uid: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
            return _row(cur)


def update_subscription(user_id: str, status: str, customer_id: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET subscription_status=%s, stripe_customer_id=COALESCE(%s, stripe_customer_id) WHERE id=%s",
                (status, customer_id, user_id)
            )
        conn.commit()


def update_subscription_by_customer(customer_id: str, status: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET subscription_status=%s WHERE stripe_customer_id=%s", (status, customer_id))
        conn.commit()


def set_reset_token(user_id: str, token: str, expiry):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET reset_token=%s, reset_token_expiry=%s WHERE id=%s", (token, expiry, user_id))
        conn.commit()


def get_user_by_reset_token(token: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE reset_token=%s AND reset_token_expiry > NOW()", (token,))
            return _row(cur)


def update_password(user_id: str, password_hash: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash=%s, reset_token=NULL, reset_token_expiry=NULL WHERE id=%s", (password_hash, user_id))
        conn.commit()


def update_profile(user_id: str, garage_name: str, email: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET garage_name=%s, email=%s WHERE id=%s", (garage_name, email, user_id))
        conn.commit()


def update_plan(user_id: str, plan: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET plan=%s WHERE id=%s", (plan, user_id))
        conn.commit()


def count_user_alerts(user_id: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM alerts WHERE user_id=%s", (user_id,))
            return cur.fetchone()[0]


def get_all_active_alerts_with_plan() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.*, u.subscription_status, u.plan, u.created_at as user_created_at
                FROM alerts a
                JOIN users u ON u.id = a.user_id
                WHERE a.active = TRUE
            """)
            return _rows(cur)


# ── Alerts ─────────────────────────────────────────────────────────────────────

def create_alert(user_id: str, data: dict) -> str:
    aid = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alerts (id, user_id, name, brand, model, price_min, price_max, km_max, year_min, fuel, region, zip, radius_km, frequency, alert_hour)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (aid, user_id, data.get('name',''), data.get('brand',''), data.get('model',''),
                  data.get('price_min', 0), data.get('price_max', 50000), data.get('km_max', 200000),
                  data.get('year_min', 2010), data.get('fuel',''), data.get('region','France'),
                  data.get('zip',''), data.get('radius_km', 0), data.get('frequency','daily'),
                  data.get('alert_hour', 8)))
        conn.commit()
    return aid


def get_alerts(user_id: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.*, COUNT(v.id) as vehicle_count
                FROM alerts a
                LEFT JOIN vehicles v ON v.alert_id = a.id AND v.hidden = FALSE
                WHERE a.user_id = %s
                GROUP BY a.id
                ORDER BY a.created_at DESC
            """, (user_id,))
            return _rows(cur)


def get_all_active_alerts() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM alerts WHERE active=TRUE")
            return _rows(cur)


def delete_alert(alert_id: str, user_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM alerts WHERE id=%s AND user_id=%s", (alert_id, user_id))
        conn.commit()


def toggle_alert(alert_id: str, user_id: str, active: bool):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET active=%s WHERE id=%s AND user_id=%s", (active, alert_id, user_id))
        conn.commit()


def update_alert(alert_id: str, user_id: str, data: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE alerts SET name=%s, brand=%s, model=%s, price_max=%s, km_max=%s, year_min=%s, fuel=%s, region=%s, zip=%s, radius_km=%s, frequency=%s, alert_hour=%s
                WHERE id=%s AND user_id=%s
            """, (data.get('name',''), data.get('brand',''), data.get('model',''),
                  data.get('price_max', 50000), data.get('km_max', 200000),
                  data.get('year_min', 2010), data.get('fuel',''), data.get('region','France'),
                  data.get('zip',''), data.get('radius_km', 0), data.get('frequency','daily'),
                  data.get('alert_hour', 8),
                  alert_id, user_id))
        conn.commit()


def update_alert_last_scan(alert_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET last_scan=NOW() WHERE id=%s", (alert_id,))
        conn.commit()


# ── Vehicles ───────────────────────────────────────────────────────────────────

def save_vehicle(data: dict) -> bool:
    vid = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute("""
                    INSERT INTO vehicles (id, alert_id, user_id, source, url, title, price, km, year, brand, model, location, image_url, score)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (url) DO NOTHING
                """, (vid, data['alert_id'], data['user_id'], data['source'], data['url'],
                      data.get('title',''), data.get('price',0), data.get('km',0), data.get('year',0),
                      data.get('brand',''), data.get('model',''), data.get('location',''),
                      data.get('image_url',''), data.get('score',0)))
                conn.commit()
                return cur.rowcount > 0
            except Exception:
                return False


def get_vehicles(user_id: str, alert_id: str = None, limit: int = 100,
                 source: str = None, min_score: int = 0,
                 sort: str = 'score', favorites_only: bool = False) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            conditions = ["user_id=%s", "hidden=FALSE"]
            params = [user_id]
            if alert_id:
                conditions.append("alert_id=%s")
                params.append(alert_id)
            if source:
                conditions.append("source=%s")
                params.append(source)
            if min_score:
                conditions.append("score>=%s")
                params.append(min_score)
            if favorites_only:
                conditions.append("favorited=TRUE")
            order = {
                'score': 'score DESC, found_at DESC',
                'price_asc': 'price ASC',
                'price_desc': 'price DESC',
                'km': 'km ASC',
                'date': 'found_at DESC',
            }.get(sort, 'score DESC, found_at DESC')
            where = " AND ".join(conditions)
            params.append(limit)
            cur.execute(f"SELECT * FROM vehicles WHERE {where} ORDER BY {order} LIMIT %s", params)
            return _rows(cur)


def toggle_vehicle_favorite(vehicle_id: str, user_id: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE vehicles SET favorited = NOT favorited WHERE id=%s AND user_id=%s RETURNING favorited", (vehicle_id, user_id))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else False


def hide_vehicle(vehicle_id: str, user_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE vehicles SET hidden=TRUE WHERE id=%s AND user_id=%s", (vehicle_id, user_id))
        conn.commit()


def get_unsent_vehicles(user_id: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vehicles WHERE user_id=%s AND sent=FALSE AND hidden=FALSE ORDER BY score DESC LIMIT 20", (user_id,))
            return _rows(cur)


def mark_vehicles_sent(user_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE vehicles SET sent=TRUE WHERE user_id=%s AND sent=FALSE", (user_id,))
        conn.commit()


def get_vehicle_stats_daily(user_id: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DATE(found_at) as date, COUNT(*) as count
                FROM vehicles
                WHERE user_id=%s AND found_at > NOW() - INTERVAL '14 days'
                GROUP BY DATE(found_at)
                ORDER BY date
            """, (user_id,))
            return _rows(cur)


def get_best_vehicle_today(user_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM vehicles
                WHERE user_id=%s AND hidden=FALSE AND found_at > NOW() - INTERVAL '24 hours'
                ORDER BY score DESC LIMIT 1
            """, (user_id,))
            return _row(cur)


def get_all_users_admin() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.email, u.garage_name, u.subscription_status, u.plan,
                       u.created_at,
                       json_agg(
                           json_build_object(
                               'id', a.id, 'brand', a.brand,
                               'model', a.model, 'price_max', a.price_max,
                               'region', a.region, 'alert_hour', a.alert_hour,
                               'active', a.active
                           )
                       ) FILTER (WHERE a.id IS NOT NULL) as alerts
                FROM users u
                LEFT JOIN alerts a ON a.user_id = u.id
                GROUP BY u.id, u.email, u.garage_name, u.subscription_status, u.plan, u.created_at
                ORDER BY u.created_at DESC
            """)
            return _rows(cur)


def get_all_users_with_alerts() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT u.id, u.email, u.garage_name, u.subscription_status
                FROM users u
                JOIN alerts a ON a.user_id = u.id
                WHERE a.active = TRUE
            """)
            return _rows(cur)


# ── Notifications ──────────────────────────────────────────────────────────────

def create_notification(user_id: str, message: str):
    nid = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO notifications (id, user_id, message) VALUES (%s, %s, %s)", (nid, user_id, message))
        conn.commit()


def get_notifications(user_id: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 20", (user_id,))
            return _rows(cur)


def mark_notifications_read(user_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET read=TRUE WHERE user_id=%s AND read=FALSE", (user_id,))
        conn.commit()


def get_unread_count(user_id: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM notifications WHERE user_id=%s AND read=FALSE", (user_id,))
            return cur.fetchone()[0]
