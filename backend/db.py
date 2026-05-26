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
                    frequency TEXT DEFAULT 'daily',
                    active BOOLEAN DEFAULT TRUE,
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
                    found_at TIMESTAMP DEFAULT NOW()
                )
            """)
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


# ── Alerts ─────────────────────────────────────────────────────────────────────

def create_alert(user_id: str, data: dict) -> str:
    aid = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alerts (id, user_id, name, brand, model, price_min, price_max, km_max, year_min, fuel, region, frequency)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (aid, user_id, data.get('name',''), data.get('brand',''), data.get('model',''),
                  data.get('price_min', 0), data.get('price_max', 50000), data.get('km_max', 200000),
                  data.get('year_min', 2010), data.get('fuel',''), data.get('region','France'),
                  data.get('frequency','daily')))
        conn.commit()
    return aid


def get_alerts(user_id: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM alerts WHERE user_id=%s ORDER BY created_at DESC", (user_id,))
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


def get_vehicles(user_id: str, alert_id: str = None, limit: int = 50) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if alert_id:
                cur.execute("SELECT * FROM vehicles WHERE user_id=%s AND alert_id=%s ORDER BY score DESC, found_at DESC LIMIT %s", (user_id, alert_id, limit))
            else:
                cur.execute("SELECT * FROM vehicles WHERE user_id=%s ORDER BY score DESC, found_at DESC LIMIT %s", (user_id, limit))
            return _rows(cur)


def get_unsent_vehicles(user_id: str) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM vehicles WHERE user_id=%s AND sent=FALSE ORDER BY score DESC LIMIT 20", (user_id,))
            return _rows(cur)


def mark_vehicles_sent(user_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE vehicles SET sent=TRUE WHERE user_id=%s AND sent=FALSE", (user_id,))
        conn.commit()


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
