import os
import smtplib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

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
        get_all_active_alerts, save_vehicle, get_vehicles,
        get_unsent_vehicles, mark_vehicles_sent, get_all_users_with_alerts,
        toggle_vehicle_favorite, hide_vehicle,
        get_vehicle_stats_daily, get_best_vehicle_today,
        create_notification, get_notifications, mark_notifications_read, get_unread_count,
        update_profile,
    )
except ModuleNotFoundError:
    from backend.db import (
        init_db, create_user, get_user_by_email, get_user_by_id,
        update_subscription, update_subscription_by_customer,
        set_reset_token, get_user_by_reset_token, update_password,
        create_alert, get_alerts, delete_alert, toggle_alert, update_alert, update_alert_last_scan,
        get_all_active_alerts, save_vehicle, get_vehicles,
        get_unsent_vehicles, mark_vehicles_sent, get_all_users_with_alerts,
        toggle_vehicle_favorite, hide_vehicle,
        get_vehicle_stats_daily, get_best_vehicle_today,
        create_notification, get_notifications, mark_notifications_read, get_unread_count,
        update_profile,
    )

load_dotenv()

BASE_DIR     = Path(__file__).parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"
SECRET_KEY   = os.getenv("SECRET_KEY", "change-me")
APP_URL      = os.getenv("APP_URL", "http://localhost:8000")
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
scheduler = AsyncIOScheduler()


async def run_daily_scrape():
    from scripts.scraper import run_alert
    import sys
    sys.path.insert(0, str(BASE_DIR.parent))

    alerts = get_all_active_alerts()
    print(f"[Scraper] Running {len(alerts)} alerts...")
    new_by_user = {}
    for alert in alerts:
        try:
            vehicles = run_alert(alert)
            new = 0
            for v in vehicles:
                if save_vehicle(v):
                    new += 1
                    uid = v['user_id']
                    new_by_user[uid] = new_by_user.get(uid, 0) + 1
            update_alert_last_scan(alert['id'])
            print(f"  Alert {alert['id'][:8]}: {new} new vehicles")
        except Exception as e:
            print(f"  Error alert {alert['id'][:8]}: {e}")

    for uid, count in new_by_user.items():
        try:
            create_notification(uid, f"🚗 {count} nouveau(x) véhicule(s) trouvé(s) aujourd'hui")
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

    lines = [f"Bonjour {garage_name or ''},\n",
             f"RateRadar a trouvé {len(vehicles)} opportunité(s) pour vous aujourd'hui :\n"]
    for v in vehicles[:10]:
        lines.append(f"• {v['title']} — {v['price']:,}€ — {v['km']:,} km — {v['location']}")
        lines.append(f"  → {v['url']}\n")
    lines.append(f"\nBonne chasse !\nL'équipe RateRadar\n{APP_URL}")

    msg = MIMEMultipart()
    msg["From"] = f"RateRadar <{gmail_user}>"
    msg["To"] = to_email
    msg["Subject"] = f"🚗 {len(vehicles)} opportunité(s) trouvée(s) aujourd'hui — RateRadar"
    msg.attach(MIMEText("\n".join(lines), "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(gmail_user, gmail_pass)
        smtp.sendmail(gmail_user, to_email, msg.as_string())


@asynccontextmanager
async def lifespan(app):
    init_db()
    scheduler.add_job(run_daily_scrape, 'cron', hour=8, minute=0, id='daily_scrape')
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
    response = RedirectResponse("/dashboard", status_code=303)
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
        created = datetime.fromisoformat((user["created_at"] or "").replace("Z", "+00:00"))
        days_left = max(0, 14 - (datetime.now(timezone.utc) - created).days)
    except Exception:
        days_left = 14
    unread = get_unread_count(user["id"])
    return {
        "id": user["id"],
        "email": user["email"],
        "garage_name": user.get("garage_name", ""),
        "subscription_status": user.get("subscription_status", "trial"),
        "trial_days_left": days_left,
        "can_use": user_can_use(user),
        "unread_notifications": unread,
        "created_at": str(user.get("created_at", "")),
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
async def subscribe(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    price_id = os.getenv("STRIPE_PRICE_ID", "")
    if not price_id:
        return JSONResponse({"error": "Stripe non configuré"}, status_code=503)
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=user["email"],
            metadata={"user_id": user["id"]},
            success_url=APP_URL + "/dashboard?subscribed=1",
            cancel_url=APP_URL + "/dashboard",
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
            customer_id = obj.get("customer")
        except Exception:
            user_id = None
            customer_id = None
        if user_id:
            update_subscription(user_id, "active", customer_id)
            create_notification(user_id, "🎉 Abonnement Pro activé ! Bienvenue dans RateRadar Pro.")
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
