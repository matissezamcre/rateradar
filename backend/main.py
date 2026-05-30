import os
import smtplib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

TZ_PARIS = ZoneInfo("Europe/Paris")

import jwt
import stripe
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
from pydantic import BaseModel
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

try:
    from db import (
        init_db, create_user, get_user_by_email, get_user_by_id,
        update_subscription, update_subscription_by_customer,
        set_reset_token, get_user_by_reset_token, update_password,
        create_alert, get_alerts, delete_alert, toggle_alert, update_alert, update_alert_last_scan,
        get_all_active_alerts, get_all_active_alerts_with_plan, save_vehicle, get_vehicles,
        get_unsent_vehicles, mark_vehicles_sent, get_all_users_with_alerts, get_all_users_admin,
        toggle_vehicle_favorite, hide_vehicle,
        get_vehicle_stats_daily, get_best_vehicle_today,
        create_notification, get_notifications, mark_notifications_read, get_unread_count,
        update_profile, update_plan, count_user_alerts,
    )
except ModuleNotFoundError:
    from backend.db import (
        init_db, create_user, get_user_by_email, get_user_by_id,
        update_subscription, update_subscription_by_customer,
        set_reset_token, get_user_by_reset_token, update_password,
        create_alert, get_alerts, delete_alert, toggle_alert, update_alert, update_alert_last_scan,
        get_all_active_alerts, get_all_active_alerts_with_plan, save_vehicle, get_vehicles,
        get_unsent_vehicles, mark_vehicles_sent, get_all_users_with_alerts, get_all_users_admin,
        toggle_vehicle_favorite, hide_vehicle,
        get_vehicle_stats_daily, get_best_vehicle_today,
        create_notification, get_notifications, mark_notifications_read, get_unread_count,
        update_profile, update_plan, count_user_alerts,
    )

load_dotenv()

BASE_DIR     = Path(__file__).parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
SECRET_KEY   = os.getenv("SECRET_KEY", "change-me")
APP_URL      = os.getenv("APP_URL", "http://localhost:8000")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
scheduler = AsyncIOScheduler()

PLAN_LIMITS = {
    "trial":   {"max_alerts": 15,   "scans_per_day": 2, "manual_scan": True,  "csv_export": True},
    "starter": {"max_alerts": 5,    "scans_per_day": 1, "manual_scan": False, "csv_export": False},
    "pro":     {"max_alerts": 15,   "scans_per_day": 2, "manual_scan": True,  "csv_export": True},
    "agence":  {"max_alerts": None, "scans_per_day": 4, "manual_scan": True,  "csv_export": True},
}


def _alert_plan(alert: dict) -> str:
    if alert.get("subscription_status") == "active":
        return alert.get("plan") or "starter"
    try:
        created = datetime.fromisoformat(str(alert.get("user_created_at", "")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - created).days < 14:
            return "trial"
    except Exception:
        pass
    return "expired"


def get_user_plan(user: dict) -> str:
    if user.get("subscription_status") == "active":
        return user.get("plan") or "starter"
    try:
        created = datetime.fromisoformat(str(user["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - created).days < 14:
            return "trial"
    except Exception:
        pass
    return "expired"


async def run_hourly_scrape():
    from scripts.scraper import run_alert
    import sys
    sys.path.insert(0, str(BASE_DIR.parent))

    now_hour = datetime.now(TZ_PARIS).hour  # heure Paris (UTC+1/+2 selon DST)

    alerts = get_all_active_alerts_with_plan()
    to_run = []
    for a in alerts:
        plan = _alert_plan(a)
        if plan == "expired":
            continue
        ah = int(a.get("alert_hour") or 8)
        if ah == now_hour:
            to_run.append(a)
        elif plan == "agence" and (ah + 12) % 24 == now_hour:
            to_run.append(a)

    print(f"[Scraper] Hour {now_hour}h Paris — {len(to_run)} alerts to run")
    new_by_user: dict = {}

    for alert in to_run:
        try:
            vehicles = run_alert(alert)
            new = 0
            for v in vehicles:
                if save_vehicle(v):
                    new += 1
                    uid = v['user_id']
                    new_by_user[uid] = new_by_user.get(uid, 0) + 1
            update_alert_last_scan(alert['id'])
            print(f"  Alert {alert['id'][:8]}: {new} new")
        except Exception as e:
            print(f"  Error {alert['id'][:8]}: {e}")

    for uid, count in new_by_user.items():
        try:
            create_notification(uid, f"🚗 {count} nouveau(x) véhicule(s) trouvé(s) ce matin")
        except Exception:
            pass

    users = get_all_users_with_alerts()
    for user in users:
        try:
            unsent = get_unsent_vehicles(user['id'])
            if unsent:
                send_digest_email(user['email'], user.get('garage_name', ''), unsent)
                mark_vehicles_sent(user['id'])
        except Exception as e:
            print(f"  Email error {user['email']}: {e}")


def send_digest_email(to_email: str, garage_name: str, vehicles: list):
    gmail_user = os.getenv("GMAIL_USER", "")
    gmail_pass = os.getenv("GMAIL_PASS", "")
    if not gmail_user or not gmail_pass:
        return

    def score_color(s):
        if s >= 140: return "#22c55e"
        if s >= 110: return "#f59e0b"
        return "#64748b"

    def score_label(s):
        if s >= 140: return "Top affaire"
        if s >= 110: return "Bonne affaire"
        return "Correct"

    cards = ""
    for v in vehicles[:12]:
        img_block = ""
        if v.get("image_url"):
            img_block = f'<img src="{v["image_url"]}" width="130" height="90" style="display:block;object-fit:cover;width:130px;height:90px;" alt="">'
        else:
            img_block = '<div style="width:130px;height:90px;background:#0f172a;display:flex;align-items:center;justify-content:center"><span style="font-size:28px">🚗</span></div>'

        sc = v.get("score", 100)
        km_fmt = f"{v.get('km', 0):,}".replace(",", " ")
        price_fmt = f"{v.get('price', 0):,}".replace(",", " ")
        loc = v.get("location") or ""
        year = v.get("year") or ""
        meta = " · ".join(filter(None, [f"{km_fmt} km", str(year) if year else "", loc]))
        source_badge = {"autoscout24": "AS24", "leboncoin": "LBC", "lacentrale": "LC", "largus": "Argus"}.get(v.get("source",""), v.get("source",""))

        cards += f"""
<div style="background:#1e293b;border-radius:12px;margin-bottom:14px;border:1px solid #334155;overflow:hidden">
  <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
    <td width="130" valign="top" style="background:#0f172a">{img_block}</td>
    <td style="padding:12px 14px;vertical-align:top">
      <p style="margin:0 0 2px;font-size:12px;font-weight:700;color:#e2e8f0;line-height:1.3">{v.get("title","")[:50]}</p>
      <p style="margin:0 0 8px;font-size:20px;font-weight:800;color:#3b82f6;letter-spacing:-0.5px">{price_fmt} €</p>
      <p style="margin:0 0 8px;font-size:11px;color:#64748b">{meta}</p>
      <span style="background:#0f172a;color:#94a3b8;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;letter-spacing:.05em">{source_badge}</span>
    </td>
    <td width="90" style="padding:12px;vertical-align:top;text-align:center">
      <div style="background:{score_color(sc)};color:#fff;font-size:9px;font-weight:800;padding:4px 8px;border-radius:20px;margin-bottom:6px;white-space:nowrap">{score_label(sc)}</div>
      <div style="font-size:18px;font-weight:800;color:#fff;margin-bottom:10px">{sc}</div>
      <a href="{v.get('url','')}" style="display:block;background:#3b82f6;color:#fff;font-size:11px;font-weight:700;padding:7px 10px;border-radius:8px;text-decoration:none;text-align:center">Voir →</a>
    </td>
  </tr></table>
</div>"""

    n = len(vehicles)
    name = garage_name or "là"
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Inter',Arial,sans-serif">
<div style="max-width:600px;margin:0 auto;padding:24px 16px">

  <div style="text-align:center;padding:28px 0 20px">
    <span style="font-size:26px;font-weight:800;color:#fff;letter-spacing:-1px">Rate<span style="color:#3b82f6">Radar</span></span>
    <p style="margin:6px 0 0;font-size:12px;color:#475569;letter-spacing:.05em;text-transform:uppercase">Votre veille automatique VO</p>
  </div>

  <div style="background:#1e293b;border:1px solid #334155;border-radius:14px;padding:20px 22px;margin-bottom:24px">
    <p style="margin:0 0 4px;font-size:15px;font-weight:700;color:#e2e8f0">Bonjour {name} 👋</p>
    <p style="margin:0;font-size:13px;color:#94a3b8;line-height:1.6">Votre bot a trouvé <strong style="color:#3b82f6">{n} nouvelle{"s" if n>1 else ""} annonce{"s" if n>1 else ""}</strong> correspondant à vos alertes. Voici les meilleures opportunités du moment :</p>
  </div>

  {cards}

  <div style="text-align:center;margin-top:28px;margin-bottom:8px">
    <a href="{APP_URL}/dashboard" style="display:inline-block;background:#3b82f6;color:#fff;font-size:13px;font-weight:700;padding:12px 28px;border-radius:10px;text-decoration:none">Voir toutes les annonces →</a>
  </div>

  <div style="border-top:1px solid #1e293b;margin-top:28px;padding-top:16px;text-align:center">
    <p style="margin:0;font-size:11px;color:#334155">RateRadar · <a href="{APP_URL}/dashboard" style="color:#475569">Dashboard</a> · <a href="{APP_URL}/profile" style="color:#475569">Gérer mes alertes</a></p>
  </div>

</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"] = f"RateRadar 🚗 <{gmail_user}>"
    msg["To"] = to_email
    msg["Subject"] = f"🚗 {n} nouvelle{'s' if n>1 else ''} annonce{'s' if n>1 else ''} trouvée{'s' if n>1 else ''} — RateRadar"
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_user, gmail_pass)
        smtp.sendmail(gmail_user, to_email, msg.as_string())


@asynccontextmanager
async def lifespan(app):
    init_db()
    scheduler.add_job(run_hourly_scrape, 'cron', minute=0, id='scan_hourly')
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)

# ── Auth helpers ───────────────────────────────────────────────────────────────

def make_token(user_id: str) -> str:
    return jwt.encode({"sub": user_id}, SECRET_KEY, algorithm="HS256")

def get_current_user(request: Request):
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        return get_user_by_id(payload["sub"])
    except Exception:
        return None

def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return user

def user_can_use(user: dict) -> bool:
    if user.get("subscription_status") == "active":
        return True
    try:
        created = datetime.fromisoformat(str(user["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created).days < 14
    except Exception:
        return True

def html(path: str) -> HTMLResponse:
    return HTMLResponse((FRONTEND_DIR / path).read_text())

# ── Auth routes ────────────────────────────────────────────────────────────────

@app.get("/register")
def register_page():
    return html("register.html")

@app.post("/register")
async def register(email: str = Form(...), password: str = Form(...), garage_name: str = Form("")):
    if get_user_by_email(email):
        return JSONResponse({"error": "Email déjà utilisé"}, status_code=400)
    uid = create_user(email, pwd_ctx.hash(password), garage_name)
    response = RedirectResponse("/dashboard?welcome=1", status_code=303)
    response.set_cookie("session", make_token(uid), httponly=True)
    return response

@app.get("/login")
def login_page():
    return html("login.html")

@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...)):
    user = get_user_by_email(email)
    if not user or not pwd_ctx.verify(password, user["password_hash"]):
        return JSONResponse({"error": "Email ou mot de passe incorrect"}, status_code=401)
    response = RedirectResponse("/dashboard", status_code=303)
    response.set_cookie("session", make_token(user["id"]), httponly=True)
    return response

@app.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("session")
    return r

@app.get("/forgot-password")
def forgot_password_page():
    return html("forgot-password.html")

@app.post("/forgot-password")
async def forgot_password(email: str = Form(...)):
    import secrets, datetime as dt
    user = get_user_by_email(email)
    if not user:
        return JSONResponse({"error": "Aucun compte avec cet email"}, status_code=404)
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=1)
    set_reset_token(user["id"], token, expiry)
    return JSONResponse({"reset_url": f"{APP_URL}/reset-password?token={token}"})

@app.get("/reset-password")
def reset_password_page():
    return html("reset-password.html")

@app.post("/reset-password")
async def reset_password(token: str = Form(...), password: str = Form(...)):
    user = get_user_by_reset_token(token)
    if not user:
        return JSONResponse({"error": "Lien invalide ou expiré"}, status_code=400)
    update_password(user["id"], pwd_ctx.hash(password))
    return JSONResponse({"ok": True})

# ── Dashboard ──────────────────────────────────────────────────────────────────

@app.get("/dashboard")
def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    return html("dashboard.html")

ADMIN_EMAILS = {"matissezamcre@gmail.com", "matisse.zamcre@gmail.com"}

def is_admin(user):
    return user and user.get("email") in ADMIN_EMAILS

@app.get("/admin")
def admin_page(request: Request):
    user = get_current_user(request)
    if not is_admin(user):
        return RedirectResponse("/login")
    return html("admin.html")

@app.get("/api/admin/users")
def admin_users(request: Request):
    user = get_current_user(request)
    if not is_admin(user):
        raise HTTPException(status_code=403)
    return JSONResponse(get_all_users_admin())

@app.get("/profile")
def profile_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    return html("profile.html")

@app.get("/me")
def me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    try:
        raw = user.get("created_at")
        if raw:
            created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        else:
            created = datetime.now(timezone.utc)
        days_left = max(0, 14 - (datetime.now(timezone.utc) - created).days)
    except Exception:
        days_left = 0
    unread = get_unread_count(user["id"])
    plan = get_user_plan(user)
    return {
        "id": user["id"],
        "email": user["email"],
        "garage_name": user.get("garage_name", ""),
        "subscription_status": user.get("subscription_status", "trial"),
        "trial_days_left": days_left,
        "can_use": user_can_use(user),
        "unread_notifications": unread,
        "created_at": str(user.get("created_at", "")),
        "plan": plan,
        "plan_limits": PLAN_LIMITS.get(plan, PLAN_LIMITS["starter"]),
    }

@app.post("/profile/update")
async def profile_update(request: Request, user: dict = Depends(require_user)):
    data = await request.json()
    garage_name = data.get("garage_name", user.get("garage_name", ""))
    email = data.get("email", user["email"])
    if email != user["email"] and get_user_by_email(email):
        return JSONResponse({"error": "Email déjà utilisé"}, status_code=400)
    update_profile(user["id"], garage_name, email)
    return {"ok": True}

@app.post("/profile/password")
async def profile_password(request: Request, user: dict = Depends(require_user)):
    data = await request.json()
    if not pwd_ctx.verify(data.get("current", ""), user["password_hash"]):
        return JSONResponse({"error": "Mot de passe actuel incorrect"}, status_code=400)
    update_password(user["id"], pwd_ctx.hash(data.get("new", "")))
    return {"ok": True}

# ── Alerts API ─────────────────────────────────────────────────────────────────

class AlertCreate(BaseModel):
    name: str = ""
    brand: str = ""
    model: str = ""
    price_min: int = 0
    price_max: int = 50000
    km_max: int = 200000
    year_min: int = 2010
    fuel: str = ""
    region: str = "France"
    zip: str = ""
    radius_km: int = 0
    alert_hour: int = 8
    frequency: str = "daily"

@app.get("/alerts")
def alerts_list(user: dict = Depends(require_user)):
    alerts = get_alerts(user["id"])
    for a in alerts:
        if a.get("created_at"):
            a["created_at"] = str(a["created_at"])
        if a.get("last_scan"):
            a["last_scan"] = str(a["last_scan"])
    return alerts

@app.post("/alerts")
def alert_create(req: AlertCreate, user: dict = Depends(require_user)):
    if not user_can_use(user):
        raise HTTPException(status_code=402, detail="trial_expired")
    plan = get_user_plan(user)
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["starter"])
    max_alerts = limits.get("max_alerts")
    if max_alerts is not None and count_user_alerts(user["id"]) >= max_alerts:
        raise HTTPException(status_code=402, detail=f"alert_limit:{max_alerts}")
    # Starter plan locked to 8h
    if plan in ("starter", "expired") and req.alert_hour != 8:
        req = req.copy(update={"alert_hour": 8})
    aid = create_alert(user["id"], req.dict())
    return {"id": aid}

@app.put("/alerts/{alert_id}")
async def alert_update(alert_id: str, request: Request, user: dict = Depends(require_user)):
    data = await request.json()
    update_alert(alert_id, user["id"], data)
    return {"ok": True}

@app.delete("/alerts/{alert_id}")
def alert_delete(alert_id: str, user: dict = Depends(require_user)):
    delete_alert(alert_id, user["id"])
    return {"ok": True}

@app.patch("/alerts/{alert_id}/toggle")
def alert_toggle(alert_id: str, user: dict = Depends(require_user)):
    alerts = get_alerts(user["id"])
    alert = next((a for a in alerts if a["id"] == alert_id), None)
    if not alert:
        raise HTTPException(status_code=404)
    toggle_alert(alert_id, user["id"], not alert["active"])
    return {"active": not alert["active"]}

# ── Vehicles API ───────────────────────────────────────────────────────────────

@app.get("/vehicles")
def vehicles_list(
    request: Request,
    alert_id: Optional[str] = None,
    source: Optional[str] = None,
    min_score: int = 0,
    sort: str = "score",
    favorites: bool = False,
    user: dict = Depends(require_user)
):
    vehicles = get_vehicles(user["id"], alert_id, source=source, min_score=min_score,
                            sort=sort, favorites_only=favorites)
    # Filtre pertinence post-DB : élimine les anciens résultats hors-sujet
    if alert_id:
        alerts = get_alerts(user["id"])
        alert = next((a for a in alerts if a["id"] == alert_id), None)
        if alert:
            import re as _re
            brand = (alert.get("brand") or "").lower().strip()
            model = (alert.get("model") or "").lower().strip()
            model_num = _re.search(r'\b(\d+)\b', model)
            filtered = []
            for v in vehicles:
                title = (v.get("title") or "").lower()
                if brand and brand.split()[0] not in title:
                    continue
                if model_num:
                    n = model_num.group(1)
                    if not _re.search(rf'\b{n}\d*\b|\b\d*{_re.escape(n)}\b', title):
                        continue
                filtered.append(v)
            vehicles = filtered
    for v in vehicles:
        if v.get("found_at"):
            v["found_at"] = str(v["found_at"])
    return vehicles

@app.patch("/vehicles/{vehicle_id}/favorite")
def vehicle_favorite(vehicle_id: str, user: dict = Depends(require_user)):
    state = toggle_vehicle_favorite(vehicle_id, user["id"])
    return {"favorited": state}

@app.patch("/vehicles/{vehicle_id}/hide")
def vehicle_hide(vehicle_id: str, user: dict = Depends(require_user)):
    hide_vehicle(vehicle_id, user["id"])
    return {"ok": True}

@app.get("/stats/daily")
def stats_daily(user: dict = Depends(require_user)):
    rows = get_vehicle_stats_daily(user["id"])
    return [{"date": str(r["date"]), "count": r["count"]} for r in rows]

@app.get("/export/csv")
def export_csv(alert_id: Optional[str] = None, user: dict = Depends(require_user)):
    from fastapi.responses import StreamingResponse
    import csv, io
    plan = get_user_plan(user)
    if not PLAN_LIMITS.get(plan, {}).get("csv_export"):
        raise HTTPException(status_code=402, detail="csv_export_unavailable")
    rows = get_vehicles(user["id"], alert_id, limit=2000)
    fields = ["title", "price", "km", "year", "brand", "model", "location", "source", "url", "score", "found_at"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for v in rows:
        if v.get("found_at"):
            v["found_at"] = str(v["found_at"])
        w.writerow(v)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=rateradar_export.csv"})

@app.get("/stats/best-today")
def best_today(user: dict = Depends(require_user)):
    v = get_best_vehicle_today(user["id"])
    if v and v.get("found_at"):
        v["found_at"] = str(v["found_at"])
    return v or {}

@app.post("/scrape/run")
async def scrape_run(user: dict = Depends(require_user)):
    if not user_can_use(user):
        raise HTTPException(status_code=402, detail="trial_expired")
    plan = get_user_plan(user)
    if not PLAN_LIMITS.get(plan, {}).get("manual_scan"):
        raise HTTPException(status_code=402, detail="manual_scan_unavailable")
    import sys
    sys.path.insert(0, str(BASE_DIR.parent))
    from scripts.scraper import run_alert
    alerts = get_alerts(user["id"])
    active = [a for a in alerts if a.get("active")]
    if not active:
        return {"found": 0, "message": "Aucune alerte active"}
    total = 0
    for alert in active:
        vehicles = run_alert(alert)
        new = 0
        for v in vehicles:
            if save_vehicle(v):
                total += 1
                new += 1
        update_alert_last_scan(alert['id'])
    if total > 0:
        create_notification(user["id"], f"⚡ Scan manuel : {total} nouveau(x) véhicule(s) trouvé(s)")
    return {"found": total}

@app.get("/scrape/debug")
async def scrape_debug(brand: str = "BMW", model: str = "Serie 3", user: dict = Depends(require_user)):
    import sys, requests as req, importlib
    sys.path.insert(0, str(BASE_DIR.parent))
    import scripts.scraper as sc
    importlib.reload(sc)

    fake_alert = {"id": "debug", "user_id": user["id"], "brand": brand, "model": model,
                  "price_max": 25000, "km_max": 200000, "year_min": 2015}

    # Test HTTP brut pour chaque site
    probes = {
        "leboncoin_api": ("POST", "https://api.leboncoin.fr/api/frontend/v4/search", sc.LBC_HEADERS),
        "autoscout24": ("GET", f"https://www.autoscout24.fr/lst/{brand.lower()}/{model.lower().replace(' ','-')}?sort=price&desc=0&cy=F&atype=C", sc.AS24_HEADERS),
        "lacentrale": ("GET", f"https://www.lacentrale.fr/listing?makesModelsCommercialNames={brand}%3A{model}&priceMax=25000", sc.LC_HEADERS),
    }
    http_status = {}
    for name, (method, url, hdrs) in probes.items():
        try:
            if method == "POST":
                r = req.post(url, headers=hdrs, json={"limit":1,"filters":{"category":{"id":"2"},"enums":{"ad_type":["offer"]},"keywords":{"text":brand}}}, timeout=10)
            else:
                r = req.get(url, headers=hdrs, timeout=10)
            snippet = r.text[:300].replace("\n", " ")
            http_status[name] = {"status": r.status_code, "snippet": snippet}
        except Exception as e:
            http_status[name] = {"status": "error", "snippet": str(e)}

    # Dump __NEXT_DATA__ d'AutoScout24 pour voir la structure réelle
    import json as _json
    from bs4 import BeautifulSoup as _BS
    as24_url = f"https://www.autoscout24.fr/lst/{brand.lower()}/{model.lower().replace(' ','-')}?sort=price&desc=0&cy=F&atype=C&fregfrom=2015&kmto=200000&priceto=25000"
    try:
        as24_r = req.get(as24_url, headers=sc.AS24_HEADERS, timeout=12)
        soup = _BS(as24_r.text, "html.parser")
        nd_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if nd_tag and nd_tag.string:
            nd = _json.loads(nd_tag.string)
            pp = nd.get("props", {}).get("pageProps", {})
            nd_keys = list(pp.keys())
            # Cherche où sont les annonces
            listings_sample = None
            for k in ["listings", "ads", "searchResults", "initialState", "initialData"]:
                if k in pp:
                    val = pp[k]
                    if isinstance(val, dict):
                        listings_sample = {"key": k, "sub_keys": list(val.keys())[:10]}
                    elif isinstance(val, list):
                        first = val[0] if val else {}
                        listings_sample = {
                            "key": k,
                            "count": len(val),
                            "first_keys": list(first.keys())[:15] if first else [],
                            "raw_first": first,  # dump complet de la 1ère annonce
                        }
                    break
            as24_nd_info = {"pageProps_keys": nd_keys, "listings_found": listings_sample}
        else:
            as24_nd_info = {"error": "pas de __NEXT_DATA__", "html_snippet": as24_r.text[:200]}
    except Exception as e:
        as24_nd_info = {"error": str(e)}

    out = {"http_probes": http_status, "as24_next_data": as24_nd_info}
    for fn, name in [(sc.scrape_leboncoin, "leboncoin"), (sc.scrape_autoscout24, "autoscout24"), (sc.scrape_lacentrale, "lacentrale")]:
        try:
            r = fn(fake_alert)
            out[name] = {"count": len(r), "sample": r[0] if r else None}
        except Exception as e:
            out[name] = {"error": str(e)}
    return out

@app.post("/digest/now")
async def digest_now(user: dict = Depends(require_user)):
    unsent = get_unsent_vehicles(user["id"])
    if not unsent:
        return {"sent": 0, "message": "Aucun nouveau véhicule à envoyer"}
    try:
        send_digest_email(user["email"], user.get("garage_name", ""), unsent)
        mark_vehicles_sent(user["id"])
        return {"sent": len(unsent)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ── Notifications ──────────────────────────────────────────────────────────────

@app.get("/notifications")
def notifications_list(user: dict = Depends(require_user)):
    notifs = get_notifications(user["id"])
    return [{"id": n["id"], "message": n["message"], "read": n["read"], "created_at": str(n["created_at"])} for n in notifs]

@app.post("/notifications/read")
def notifications_read(user: dict = Depends(require_user)):
    mark_notifications_read(user["id"])
    return {"ok": True}

# ── Stripe ─────────────────────────────────────────────────────────────────────

@app.get("/subscribe")
async def subscribe(request: Request, plan: str = ""):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    if not plan:
        return html("subscribe.html")
    price_map = {
        "starter": os.getenv("STRIPE_PRICE_STARTER", ""),
        "pro":     os.getenv("STRIPE_PRICE_PRO", os.getenv("STRIPE_PRICE_ID", "")),
        "agence":  os.getenv("STRIPE_PRICE_AGENCE", ""),
    }
    price_id = price_map.get(plan, "")
    if not price_id:
        return JSONResponse({"error": "Plan ou Stripe non configuré"}, status_code=503)
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=user["email"],
            metadata={"user_id": user["id"], "plan": plan},
            success_url=APP_URL + "/dashboard?subscribed=1",
            cancel_url=APP_URL + "/subscribe",
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return RedirectResponse(session.url, status_code=303)

@app.get("/stripe-portal")
async def stripe_portal(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        return RedirectResponse("/dashboard")
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=APP_URL + "/profile",
        )
        return RedirectResponse(session.url, status_code=303)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, os.getenv("STRIPE_WEBHOOK_SECRET", ""))
    except Exception:
        return JSONResponse({"error": "invalid"}, status_code=400)
    if event["type"] == "checkout.session.completed":
        try:
            obj = event["data"]["object"]
            metadata = obj.get("metadata") or {}
            user_id = metadata.get("user_id") if isinstance(metadata, dict) else getattr(metadata, "user_id", None)
            plan = (metadata.get("plan") if isinstance(metadata, dict) else None) or "pro"
            customer_id = obj.get("customer")
        except Exception:
            user_id = None
            plan = "pro"
            customer_id = None
        if user_id:
            update_subscription(user_id, "active", customer_id)
            update_plan(user_id, plan)
            plan_label = {"starter": "Starter", "pro": "Pro", "agence": "Agence"}.get(plan, plan.capitalize())
            create_notification(user_id, f"🎉 Abonnement {plan_label} activé ! Bienvenue dans RateRadar.")
    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        try:
            customer_id = event["data"]["object"].get("customer")
        except Exception:
            customer_id = None
        if customer_id:
            update_subscription_by_customer(customer_id, "inactive")
    return {"ok": True}

# ── Static ─────────────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=True)
