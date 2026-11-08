import os, re, csv, io, time, hashlib, secrets, datetime as dt
from urllib.parse import quote_plus
import stripe, anthropic
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine, Column, Integer, String, LargeBinary, DateTime, func, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from templates_data import TEMPLATES
from telemarketing_data import SCRIPTS

E = os.environ.get
stripe.api_key = E("STRIPE_SECRET_KEY")
ai = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
MODEL = E("MODEL", "claude-sonnet-5-5")
BASE_URL = E("BASE_URL", "http://localhost:8000")

# VIP accounts: free unlimited forever, plus the Gators theme and welcome screen.
# Add more with the VIP_EMAILS env var (comma separated) if you ever want to.
ADMIN_EMAILS = {e.strip().lower() for e in E("ADMIN_EMAILS", "").split(",") if e.strip()}  # can delete any chat message
VIP_EMAILS = {e.strip().lower() for e in E("VIP_EMAILS", "pernadawn@yahoo.com").split(",") if e.strip()}
# CEO account: unlimited, "WELCOME BOSS" splash, and the /ceo control room.
CEO_EMAILS = {e.strip().lower() for e in E("CEO_EMAILS", "peytonmoore81@gmail.com").split(",") if e.strip()}
# Optional but recommended: set CEO_PIN on Render and /ceo asks for it once per login.
CEO_PIN = E("CEO_PIN", "")
SPECIAL = VIP_EMAILS | CEO_EMAILS

# Display prices are labels only; real prices live in Stripe.
PLANS = {
    "free": {"name": "Free", "limit": 2, "price": "$0", "period": "", "note": "2 generations, one time"},
    "starter": {"name": "Starter", "limit": 30, "price": "$29", "period": "per month", "note": "30 generations per month"},
    "pro": {"name": "Pro", "limit": 75, "price": "$59", "period": "every 3 months", "note": "75 generations per month"},
    "unlimited": {"name": "Unlimited", "limit": 150, "price": "$99", "period": "per year", "note": "150 generations per month"},
    "ceo": {"name": "CEO", "limit": None, "price": "$0", "period": "", "note": "Unlimited, boss mode"},  # hidden from pricing page
    "vip": {"name": "Unlimited VIP", "limit": None, "price": "$0", "period": "", "note": "Free unlimited, forever"},  # hidden from pricing page
    "admin": {"name": "Admin", "limit": None, "price": "$0", "period": "", "note": "Unlimited, telemarketing team"},  # hidden from pricing page; given from the CEO room
}
PRICE_IDS = {p: E(f"STRIPE_PRICE_{p.upper()}") for p in ("starter", "pro", "unlimited")}

url = E("DATABASE_URL") or "sqlite:///local.db"
url = url.replace("postgres://", "postgresql://", 1).replace("postgresql://", "postgresql+psycopg2://", 1)
engine = create_engine(url)
Session = sessionmaker(engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, index=True)
    pw = Column(String)
    plan = Column(String, default="free")
    customer = Column(String, index=True)
    free_used = Column(Integer, default=0)
    month = Column(String, default="")
    month_used = Column(Integer, default=0)
    created = Column(DateTime)
    last_login = Column(DateTime)
    staff = Column(Integer, default=0)  # 1 = Admin / telemarketer (set from the CEO room)

class Profile(Base):  # public chat nickname (never show emails in chat)
    __tablename__ = "profiles"
    user_id = Column(Integer, primary_key=True)
    nick = Column(String, unique=True, index=True)

class Msg(Base):
    __tablename__ = "chat_msgs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True)
    nick = Column(String)
    body = Column(String, default="")
    img = Column(LargeBinary, nullable=True)
    img_type = Column(String, nullable=True)
    reports = Column(Integer, default=0)
    created = Column(DateTime, default=dt.datetime.utcnow)

class Report(Base):
    __tablename__ = "chat_reports"
    id = Column(Integer, primary_key=True)
    msg_id = Column(Integer, index=True)
    user_id = Column(Integer, index=True)

class Event(Base):  # activity log: signups, purchases, plan changes, CEO actions
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=dt.datetime.utcnow, index=True)
    kind = Column(String)
    email = Column(String)
    detail = Column(String, default="")

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(String, default="")

Base.metadata.create_all(engine)

def migrate():
    # create_all never adds columns to an existing table, so add the new ones by hand.
    have = {c["name"] for c in inspect(engine).get_columns("users")}
    with engine.begin() as c:
        for col in ("created", "last_login"):
            if col not in have:
                c.execute(text(f"ALTER TABLE users ADD COLUMN {col} TIMESTAMP"))
        if "staff" not in have:
            c.execute(text("ALTER TABLE users ADD COLUMN staff INTEGER DEFAULT 0"))
migrate()

SETTINGS = {}
with Session() as _db:
    for _s in _db.query(Setting):
        SETTINGS[_s.key] = _s.value or ""

def log_event(db, kind, email, detail=""):
    db.add(Event(kind=kind, email=email, detail=detail))

app = FastAPI()
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(SessionMiddleware, secret_key=E("SECRET_KEY", "dev-only"), max_age=60 * 60 * 24 * 30)
tpl = Jinja2Templates(directory=".")
tpl.env.filters["dt"] = lambda d: d.strftime("%b %d %Y, %H:%M UTC") if d else "before logging"

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()

def check_pw(pw, stored):
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)

def current(request, db):
    uid = request.session.get("uid")
    u = db.get(User, uid) if uid else None
    want = "ceo" if (u and u.email in CEO_EMAILS) else "vip" if (u and u.email in VIP_EMAILS) else "admin" if (u and u.staff) else None
    if want and u.plan != want:
        u.plan = want  # auto-upgrade CEO / VIP accounts
        db.commit()
    elif u and not want and u.plan in ("ceo", "admin"):
        u.plan = "free"  # the CEO email changed or admin was removed; the account drops back to free
        db.commit()
    return u

def remaining(u):
    if u.plan == "free":
        return PLANS["free"]["limit"] - u.free_used
    lim = PLANS[u.plan]["limit"]
    if lim is None:
        return 10**9
    now = dt.date.today().strftime("%Y-%m")
    return lim - (u.month_used if u.month == now else 0)

def spend(u):
    if u.plan == "free":
        u.free_used += 1
    else:
        now = dt.date.today().strftime("%Y-%m")
        if u.month != now:
            u.month, u.month_used = now, 0
        u.month_used += 1

def is_staff(u):  # Admin / telemarketer (CEO and VIP accounts are handled separately)
    return bool(u and u.staff and u.email not in SPECIAL)

def is_special(u):  # accounts that never pay and that Stripe must never change
    return bool(u and (u.email in SPECIAL or u.staff))

def can_admin(u):  # may open the Telemarketing tab
    return bool(u and (u.email in CEO_EMAILS or is_staff(u)))

def page(request, name, u, **ctx):
    left = None if not u else remaining(u)
    boss = bool(u and u.email in CEO_EMAILS)
    vip = bool(u and u.email in VIP_EMAILS) and not boss
    adm = is_staff(u)
    welcome = (vip or boss or adm) and bool(request.session.pop("welcome", None))
    return tpl.TemplateResponse(request, name, {"user": u, "plans": PLANS, "left": left, "templates": TEMPLATES, "vip": vip, "boss": boss, "adm": adm, "tele": adm or boss, "welcome": welcome, "banner": SETTINGS.get("banner", ""), **ctx})

@app.get("/")
def home(request: Request, error: str = ""):
    with Session() as db:
        return page(request, "index.html", current(request, db), error=error)

@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    with Session() as db:
        if len(password) < 8 or db.query(User).filter_by(email=email).first():
            return RedirectResponse("/?error=Use+a+new+email+and+a+password+of+8%2B+characters", 303)
        u = User(email=email, pw=hash_pw(password), created=dt.datetime.utcnow())
        db.add(u); log_event(db, "signup", email, "free plan"); db.commit()
        request.session["uid"] = u.id
        if email in SPECIAL:
            request.session["welcome"] = True
    return RedirectResponse("/generate", 303)

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    with Session() as db:
        u = db.query(User).filter_by(email=email.strip().lower()).first()
        if not u or not check_pw(password, u.pw):
            return RedirectResponse("/?error=Email+or+password+is+incorrect", 303)
        request.session["uid"] = u.id
        u.last_login = dt.datetime.utcnow(); db.commit()
        if u.email in SPECIAL or u.staff:
            request.session["welcome"] = True
    return RedirectResponse("/generate", 303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)

@app.get("/generate")
def generate_page(request: Request):
    with Session() as db:
        u = current(request, db)
        return page(request, "generate.html", u) if u else RedirectResponse("/", 303)

@app.get("/templates")
def templates_page(request: Request):
    with Session() as db:
        return page(request, "templates.html", current(request, db))

SYSTEM = """You are a writing assistant that creates newsletters, emails, social posts, flyers, postcards, website copy, and other marketing materials.
You are especially knowledgeable about title insurance, escrow, and real estate closings (title search, owner's vs lender's policy, closing).
When writing for title or real estate professionals, never promise specific legal outcomes, rates, or gifts/referral incentives (RESPA/state rules).
Use [bracketed placeholders] for company names, contact details, and local facts.
Output clean, ready-to-edit HTML (h2/h3/p/ul/strong only, no html/head/body tags) for the requested piece.
Help with whatever writing or marketing request the user asks for."""

def text_of(r):
    return "".join(b.text for b in r.content if b.type == "text").strip()

@app.post("/api/generate")
async def api_generate(request: Request):
    data = await request.json()
    prompt = (data.get("prompt") or "").strip()[:2000]
    kind = (data.get("kind") or "newsletter")[:40]
    with Session() as db:
        u = current(request, db)
        if not u:
            return JSONResponse({"error": "Please log in."}, 401)
        if SETTINGS.get("maintenance") and u.email not in CEO_EMAILS:
            return JSONResponse({"error": "TitlePen is down for a quick update. Please try again in a few minutes."}, 503)
        if remaining(u) <= 0:
            return JSONResponse({"error": "You're out of generations. Upgrade your plan to keep going.", "upgrade": True}, 402)
        if not prompt:
            return JSONResponse({"error": "Type what you want written first."}, 400)
        r = ai.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM,
                               messages=[{"role": "user", "content": f"Format: {kind}\n\nRequest: {prompt}"}])
        out = text_of(r)
        spend(u); db.commit()
        return {"html": out, "left": None if PLANS[u.plan]["limit"] is None else remaining(u)}

@app.post("/checkout/{plan}")
def checkout(request: Request, plan: str):
    with Session() as db:
        u = current(request, db)
        if not u or is_special(u) or plan not in PRICE_IDS or not PRICE_IDS[plan]:
            return RedirectResponse("/", 303)
        args = {"customer": u.customer} if u.customer else {"customer_email": u.email}
        s = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": PRICE_IDS[plan], "quantity": 1}],
            client_reference_id=str(u.id),
            metadata={"plan": plan},
            managed_payments={"enabled": False},  # skip Managed Payments (no product tax code needed)
            success_url=f"{BASE_URL}/generate",
            cancel_url=f"{BASE_URL}/",
            **args,
        )
        return RedirectResponse(s.url, 303)

@app.post("/portal")
def portal(request: Request):
    with Session() as db:
        u = current(request, db)
        if not u or not u.customer:
            return RedirectResponse("/", 303)
        s = stripe.billing_portal.Session.create(customer=u.customer, return_url=f"{BASE_URL}/generate")
        return RedirectResponse(s.url, 303)

@app.post("/stripe/webhook")
async def webhook(request: Request):
    try:
        ev = stripe.Webhook.construct_event(await request.body(), request.headers.get("stripe-signature"), E("STRIPE_WEBHOOK_SECRET"))
    except Exception:
        return JSONResponse({"error": "bad signature"}, 400)
    obj = ev["data"]["object"]
    with Session() as db:
        if ev["type"] == "checkout.session.completed":
            u = db.get(User, int(obj["client_reference_id"]))
            if u and not is_special(u):
                new = obj["metadata"]["plan"]
                u.plan, u.customer = new, obj["customer"]
                log_event(db, "purchase", u.email, f"subscribed to {PLANS.get(new, {}).get('name', new)}")
        elif ev["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
            u = db.query(User).filter_by(customer=obj["customer"]).first()
            if u and not is_special(u):
                price = obj["items"]["data"][0]["price"]["id"]
                active = ev["type"] != "customer.subscription.deleted" and obj["status"] in ("active", "trialing")
                old = u.plan
                u.plan = next((p for p, i in PRICE_IDS.items() if i == price), "free") if active else "free"
                if u.plan != old:
                    kind = "canceled" if not active else "plan_change"
                    log_event(db, kind, u.email, f"{old} -> {u.plan}")
        db.commit()
    return {"ok": True}

# ---------- Help, About, Settings ----------

@app.get("/help")
def help_page(request: Request):
    with Session() as db:
        return page(request, "help.html", current(request, db))

@app.get("/about")
def about_page(request: Request):
    with Session() as db:
        return page(request, "about.html", current(request, db))

@app.get("/settings")
def settings_page(request: Request, msg: str = "", err: str = ""):
    with Session() as db:
        u = current(request, db)
        return page(request, "settings.html", u, msg=msg, err=err) if u else RedirectResponse("/", 303)

@app.post("/settings/password")
def change_password(request: Request, current_pw: str = Form(...), new_pw: str = Form(...)):
    with Session() as db:
        u = current(request, db)
        if not u:
            return RedirectResponse("/", 303)
        if not check_pw(current_pw, u.pw) or len(new_pw) < 8:
            return RedirectResponse("/settings?err=Current+password+is+wrong+or+the+new+one+is+under+8+characters", 303)
        u.pw = hash_pw(new_pw)
        db.commit()
    return RedirectResponse("/settings?msg=Password+updated", 303)

@app.post("/settings/delete")
def delete_account(request: Request, confirm: str = Form(...)):
    with Session() as db:
        u = current(request, db)
        if not u:
            return RedirectResponse("/", 303)
        if u.plan not in ("free", "vip", "ceo", "admin"):
            return RedirectResponse("/settings?err=Cancel+your+subscription+under+Manage+billing+first", 303)
        if confirm.strip().lower() != u.email:
            return RedirectResponse("/settings?err=That+email+does+not+match+your+account", 303)
        log_event(db, "deleted", u.email, "account deleted by user")
        db.query(Msg).filter_by(user_id=u.id).delete()
        db.query(Profile).filter_by(user_id=u.id).delete()
        db.delete(u)
        db.commit()
    request.session.clear()
    return RedirectResponse("/", 303)

# ---------- AI assistants (chat bubble) ----------

BOT_MODEL = E("BOT_MODEL", "claude-haiku-5-5")
SUPPORT = E("SUPPORT_EMAIL", "")
BOT_HITS = {}
BOTS = {
    "maya": ("Maya", "You specialize in plans, pricing, billing, and account questions."),
    "jordan": ("Jordan", "You specialize in helping people write good requests for the generator and get better drafts: who it is for, the topic, the goal, and the tone."),
    "sam": ("Sam", "You specialize in troubleshooting: things not loading, login problems, generations not working, and editing, copying, or downloading drafts."),
}

def bot_system(key):
    name, focus = BOTS[key]
    paid = ", ".join(f"{p['name']} {p['price']} {p['period']} ({p['note']})" for k, p in PLANS.items() if k not in ("free", "vip", "ceo", "admin"))
    help_line = f" suggest contacting {SUPPORT}" if SUPPORT else " suggest reaching out to the site owner"
    return (f"You are {name}, an AI assistant on the TitlePen website. You are an AI, not a human. If anyone asks whether you are a real person or a bot, "
        f"say plainly that you are an AI assistant. Never claim to be a real person or to have a job, family, or personal experiences. {focus}\n\n"
        "TitlePen lets title insurance and real estate professionals generate newsletters, emails, social posts, flyers, and other marketing pieces, then edit, copy, or download them. "
        f"It was created by Peyton Moore. Plans: Free (2 generations total), {paid}. Each click of Generate uses one generation; editing, copying, and downloading are free. "
        "The three-line menu at the top right has Generator, Community chat (members share pictures and what worked for them), Templates, Plans and pricing, Help and tips, About us, Settings, Manage billing (paid plans), and Log out. "
        "Settings shows plan and usage, and lets people change their password or delete their account (paid subscribers cancel in Manage billing first). "
        "Billing is handled by Stripe: in Manage billing people can change plan, update their card, see invoices, and cancel. "
        "Drafts contain [bracketed placeholders] to fill in. You cannot see anyone's account or change anything for them. "
        f"Do not invent features, prices, or policies. If you do not know something, say so and{help_line}. "
        "Title insurance marketing is regulated, so remind people to review drafts for compliance before sending, and do not give legal advice. "
        "Politely steer unrelated requests back to TitlePen. Reply in 2 to 5 short sentences of plain text, no markdown.")

@app.post("/api/chatbot")
async def chatbot(request: Request):
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "x")).split(",")[0].strip()
    now = time.time()
    recent = [t for t in BOT_HITS.get(ip, []) if now - t < 3600]
    if len(recent) >= 30:
        return JSONResponse({"reply": "I've answered a lot of questions this hour. Please try again a little later."}, 429)
    BOT_HITS[ip] = recent + [now]
    data = await request.json()
    key = data.get("bot") if data.get("bot") in BOTS else "maya"
    msgs = [{"role": "user" if m.get("role") == "user" else "assistant", "content": str(m.get("content", ""))[:600]}
            for m in (data.get("messages") or [])[-8:] if str(m.get("content", "")).strip()]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs:
        return {"reply": "What would you like help with?"}
    try:
        r = await run_in_threadpool(lambda: ai.messages.create(model=BOT_MODEL, max_tokens=600, system=bot_system(key), messages=msgs))
        return {"reply": text_of(r) or "Sorry, I couldn't come up with an answer. Please try again."}
    except Exception:
        return JSONResponse({"reply": "I'm having trouble right now. Please try again in a moment."}, 500)


# ---------- Community chat ----------

MAX_IMG = 2 * 1024 * 1024
CHAT_HITS = {}

def is_admin(u):
    return bool(u and (u.email in ADMIN_EMAILS or u.email in CEO_EMAILS))

def sniff(b):
    if b[:3] == b"\xff\xd8\xff": return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    return None

@app.get("/chat")
def chat_page(request: Request):
    with Session() as db:
        u = current(request, db)
        if not u:
            return RedirectResponse("/", 303)
        p = db.get(Profile, u.id)
        return page(request, "chat.html", u, nick=p.nick if p else "", is_admin=is_admin(u))

@app.post("/api/chat/nick")
async def chat_nick(request: Request):
    data = await request.json()
    nick = " ".join(str(data.get("nick", "")).split())
    if not re.fullmatch(r"[A-Za-z0-9 _.-]{2,20}", nick):
        return JSONResponse({"error": "Use 2 to 20 letters, numbers, spaces, dots, dashes, or underscores."}, 400)
    with Session() as db:
        u = current(request, db)
        if not u:
            return JSONResponse({"error": "Please log in."}, 401)
        if not is_admin(u) and re.search(r"admin|owner|titlepen|moderator", nick, re.I):
            return JSONResponse({"error": "Please pick a different name."}, 400)
        taken = db.query(Profile).filter(func.lower(Profile.nick) == nick.lower()).first()
        if taken and taken.user_id != u.id:
            return JSONResponse({"error": "That name is taken. Try another."}, 409)
        p = db.get(Profile, u.id) or Profile(user_id=u.id)
        p.nick = nick
        db.add(p); db.commit()
    return {"nick": nick}

@app.get("/api/chat/messages")
def chat_messages(request: Request):
    with Session() as db:
        u = current(request, db)
        if not u:
            return JSONResponse({"error": "Please log in."}, 401)
        rows = db.query(Msg).filter(Msg.reports < 3).order_by(Msg.id.desc()).limit(80).all()[::-1]
        adm = {x.id for x in db.query(User).filter(User.email.in_(list(ADMIN_EMAILS | CEO_EMAILS)))}
        return {"messages": [{
            "id": m.id, "nick": m.nick, "body": m.body or "", "img": bool(m.img_type),
            "mine": m.user_id == u.id, "owner": m.user_id in adm,
            "canDelete": m.user_id == u.id or is_admin(u),
            "ts": m.created.isoformat() + "Z",
        } for m in rows]}

@app.post("/api/chat/send")
async def chat_send(request: Request, body: str = Form(""), image: UploadFile = File(None)):
    body = body.strip()[:500]
    data = await image.read(MAX_IMG + 1) if image is not None and image.filename else b""
    if len(data) > MAX_IMG:
        return JSONResponse({"error": "That picture is over 2 MB. Try a smaller one."}, 413)
    kind = sniff(data) if data else None
    if data and not kind:
        return JSONResponse({"error": "Pictures must be JPG, PNG, GIF, or WebP."}, 400)
    if not body and not data:
        return JSONResponse({"error": "Type a message or add a picture."}, 400)
    with Session() as db:
        u = current(request, db)
        if not u:
            return JSONResponse({"error": "Please log in."}, 401)
        p = db.get(Profile, u.id)
        if not p:
            return JSONResponse({"error": "Pick a chat name first."}, 400)
        now = time.time()
        recent = [t for t in CHAT_HITS.get(u.id, []) if now - t < 60]
        if len(recent) >= 8:
            return JSONResponse({"error": "Slow down a little. Try again in a minute."}, 429)
        CHAT_HITS[u.id] = recent + [now]
        db.add(Msg(user_id=u.id, nick=p.nick, body=body, img=data or None, img_type=kind))
        db.commit()
        old = [r.id for r in db.query(Msg.id).order_by(Msg.id.desc()).offset(500).all()]
        if old:
            db.query(Msg).filter(Msg.id.in_(old)).delete(synchronize_session=False)
            db.commit()
    return {"ok": True}

@app.get("/chat/img/{mid}")
def chat_img(request: Request, mid: int):
    with Session() as db:
        if not current(request, db):
            return Response(status_code=401)
        m = db.get(Msg, mid)
        if not m or not m.img or m.reports >= 3:
            return Response(status_code=404)
        return Response(m.img, media_type=m.img_type, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})

@app.post("/api/chat/delete/{mid}")
def chat_delete(request: Request, mid: int):
    with Session() as db:
        u = current(request, db)
        m = db.get(Msg, mid) if u else None
        if not m or not (m.user_id == u.id or is_admin(u)):
            return JSONResponse({"error": "Not allowed."}, 403)
        db.delete(m); db.commit()
    return {"ok": True}

@app.post("/api/chat/report/{mid}")
def chat_report(request: Request, mid: int):
    with Session() as db:
        u = current(request, db)
        m = db.get(Msg, mid) if u else None
        if not m or m.user_id == u.id:
            return JSONResponse({"error": "Can't report that."}, 400)
        if not db.query(Report).filter_by(msg_id=mid, user_id=u.id).first():
            db.add(Report(msg_id=mid, user_id=u.id))
            m.reports = (m.reports or 0) + 1  # hidden automatically at 3 reports
            db.commit()
    return {"ok": True}

# ---------- CEO control room ----------

# Rough monthly value per plan, based on the display prices in PLANS (real billing lives in Stripe).
MONTHLY = {"starter": 29, "pro": 59 / 3, "unlimited": 99 / 12}
PAID = ("starter", "pro", "unlimited")
CEO_TRIES = {}

def ceo_ok(request, u):
    return bool(u and u.email in CEO_EMAILS and (not CEO_PIN or request.session.get("ceo_ok")))

def ceo_guard(request, db):
    u = current(request, db)
    return u if ceo_ok(request, u) else None

def ceo_back(msg="", err=""):
    q = ("msg=" + quote_plus(msg)) if msg else ("err=" + quote_plus(err))
    return RedirectResponse("/ceo?" + q, 303)

def save_setting(db, key, value):
    s = db.get(Setting, key) or Setting(key=key)
    s.value = value
    db.add(s); db.commit()
    SETTINGS[key] = value

@app.get("/ceo")
def ceo_page(request: Request, q: str = "", plan: str = "", msg: str = "", err: str = ""):
    with Session() as db:
        u = current(request, db)
        if not u or u.email not in CEO_EMAILS:
            return RedirectResponse("/", 303)
        if not ceo_ok(request, u):
            return page(request, "ceo.html", u, locked=True, err=err, msg="")
        now = dt.datetime.utcnow()
        month = now.strftime("%Y-%m")
        users = db.query(User).order_by(User.id.desc()).all()
        counts = {k: 0 for k in PLANS}
        for x in users:
            counts[x.plan if x.plan in counts else "free"] += 1
        since = lambda d: sum(1 for x in users if x.created and x.created >= now - dt.timedelta(days=d))
        stats = {
            "total": len(users), "today": since(1), "week": since(7), "month": since(30),
            "paying": sum(counts[k] for k in PAID), "free": counts["free"],
            "mrr": round(sum(MONTHLY[k] * counts[k] for k in PAID)),
            "free_gens": sum(x.free_used or 0 for x in users),
            "paid_gens": sum(x.month_used or 0 for x in users if x.month == month and x.plan in PAID),
        }
        ql = q.strip().lower()
        rows = [{
            "id": x.id, "email": x.email, "plan": x.plan, "created": x.created, "last_login": x.last_login,
            "customer": x.customer, "special": x.email in SPECIAL, "staff": bool(x.staff) and x.email not in SPECIAL,
            "used": (x.free_used or 0) if x.plan == "free" else ((x.month_used or 0) if x.month == month else 0),
            "limit": (PLANS.get(x.plan) or {}).get("limit"),
        } for x in users if (not ql or ql in x.email) and (not plan or x.plan == plan)]
        events = [{"ts": e.ts, "kind": e.kind, "email": e.email, "detail": e.detail}
                  for e in db.query(Event).order_by(Event.id.desc()).limit(100)]
        reported = [{"id": r.id, "nick": r.nick, "body": r.body, "reports": r.reports, "img": bool(r.img_type)}
                    for r in db.query(Msg.id, Msg.nick, Msg.body, Msg.reports, Msg.img_type).filter(Msg.reports > 0).order_by(Msg.id.desc()).limit(30)]
        return page(request, "ceo.html", u, locked=False, msg=msg, err=err, stats=stats, counts=counts,
                    rows=rows[:200], shown=len(rows), events=events, reported=reported, q=q, fplan=plan,
                    maintenance=bool(SETTINGS.get("maintenance")), pin_set=bool(CEO_PIN),
                    admins=[x.email for x in users if x.staff and x.email not in SPECIAL])

@app.post("/ceo/unlock")
def ceo_unlock(request: Request, pin: str = Form(...)):
    with Session() as db:
        u = current(request, db)
        if not u or u.email not in CEO_EMAILS:
            return RedirectResponse("/", 303)
        now = time.time()
        tries = [t for t in CEO_TRIES.get(u.id, []) if now - t < 900]
        if len(tries) >= 5:
            return ceo_back(err="Too many tries. Wait 15 minutes.")
        if CEO_PIN and secrets.compare_digest(pin.encode(), CEO_PIN.encode()):
            request.session["ceo_ok"] = True
            return RedirectResponse("/ceo", 303)
        CEO_TRIES[u.id] = tries + [now]
        return ceo_back(err="Wrong PIN.")

@app.post("/ceo/plan")
def ceo_set_plan(request: Request, user_id: int = Form(...), plan: str = Form(...)):
    with Session() as db:
        me = ceo_guard(request, db)
        if not me:
            return RedirectResponse("/", 303)
        t = db.get(User, user_id)
        if not t or plan not in PLANS or plan in ("ceo", "admin") or is_special(t):
            return ceo_back(err="Pick a valid account and plan.")
        email = t.email  # read before commit; commit expires the object
        old, t.plan = t.plan, plan
        log_event(db, "ceo_plan", email, f"{old} -> {plan} (set by CEO)")
        db.commit()
    return ceo_back(msg=f"Set {email} to {PLANS[plan]['name']}. If they have a Stripe subscription, Stripe may change it back on the next billing event.")

@app.post("/ceo/reset")
def ceo_reset(request: Request, user_id: int = Form(...)):
    with Session() as db:
        if not ceo_guard(request, db):
            return RedirectResponse("/", 303)
        t = db.get(User, user_id)
        if not t:
            return ceo_back(err="Account not found.")
        t.free_used, t.month_used = 0, 0
        log_event(db, "ceo_reset", t.email, "usage reset")
        db.commit()
    return ceo_back(msg="Usage reset.")

@app.post("/ceo/delete")
def ceo_delete(request: Request, user_id: int = Form(...)):
    with Session() as db:
        if not ceo_guard(request, db):
            return RedirectResponse("/", 303)
        t = db.get(User, user_id)
        if not t or t.email in CEO_EMAILS or t.staff:
            return ceo_back(err="Can't delete that account. If they are an Admin, remove their admin access first.")
        if t.plan in PAID:
            return ceo_back(err="They have a paid plan. Cancel their subscription in Stripe first.")
        log_event(db, "ceo_delete", t.email, "account deleted by CEO")
        db.query(Msg).filter_by(user_id=t.id).delete()
        db.query(Profile).filter_by(user_id=t.id).delete()
        db.delete(t); db.commit()
    return ceo_back(msg="Account deleted.")

@app.post("/ceo/banner")
def ceo_banner(request: Request, text_: str = Form("")):
    with Session() as db:
        if not ceo_guard(request, db):
            return RedirectResponse("/", 303)
        save_setting(db, "banner", text_.strip()[:300])
    return ceo_back(msg="Banner updated." if text_.strip() else "Banner cleared.")

@app.post("/ceo/maintenance")
def ceo_maintenance(request: Request):
    with Session() as db:
        if not ceo_guard(request, db):
            return RedirectResponse("/", 303)
        on = not SETTINGS.get("maintenance")
        save_setting(db, "maintenance", "1" if on else "")
        log_event(db, "ceo_maintenance", "", "ON" if on else "OFF"); db.commit()
    return ceo_back(msg="Maintenance mode is ON. Generations are paused for everyone but you." if on else "Maintenance mode is OFF.")

@app.post("/ceo/chat/{mid}/{action}")
def ceo_chat(request: Request, mid: int, action: str):
    with Session() as db:
        if not ceo_guard(request, db):
            return RedirectResponse("/", 303)
        m = db.get(Msg, mid)
        if m and action == "restore":
            m.reports = 0
            db.query(Report).filter_by(msg_id=mid).delete()
        elif m and action == "delete":
            db.delete(m)
        db.commit()
    return ceo_back(msg="Done.")

@app.get("/ceo/export.csv")
def ceo_export(request: Request):
    with Session() as db:
        if not ceo_guard(request, db):
            return RedirectResponse("/", 303)
        safe = lambda v: ("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "email", "plan", "joined_utc", "last_login_utc", "stripe_customer"])
        for x in db.query(User).order_by(User.id):
            w.writerow([x.id, safe(x.email), x.plan, x.created or "", x.last_login or "", x.customer or ""])
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=titlepen-accounts.csv"})


# ---------- CEO: give or remove Admin (telemarketer) access ----------

@app.post("/ceo/admin/add")
def ceo_admin_add(request: Request, user_id: int = Form(0), email: str = Form("")):
    with Session() as db:
        if not ceo_guard(request, db):
            return RedirectResponse("/", 303)
        t = db.get(User, user_id) if user_id else db.query(User).filter_by(email=email.strip().lower()).first()
        if not t:
            return ceo_back(err="No account with that email. Ask them to sign up first, then add them here.")
        if t.email in SPECIAL:
            return ceo_back(err="That account already has CEO or VIP access.")
        if t.staff:
            return ceo_back(err="They are already an Admin.")
        if t.plan in PAID:
            return ceo_back(err="They have a paid plan. Cancel their subscription in Stripe first.")
        em = t.email  # read before commit; commit expires the object
        t.staff, t.plan = 1, "admin"
        log_event(db, "ceo_admin_add", em, "made Admin (telemarketer) by CEO")
        db.commit()
    return ceo_back(msg=f"{em} is now an Admin with free unlimited access and the Telemarketing tab.")

@app.post("/ceo/admin/remove")
def ceo_admin_remove(request: Request, user_id: int = Form(...)):
    with Session() as db:
        if not ceo_guard(request, db):
            return RedirectResponse("/", 303)
        t = db.get(User, user_id)
        if not t or not t.staff:
            return ceo_back(err="That account is not an Admin.")
        em = t.email
        t.staff = 0
        if t.plan == "admin":
            t.plan = "free"
        log_event(db, "ceo_admin_remove", em, "Admin access removed by CEO")
        db.commit()
    return ceo_back(msg=f"{em} is no longer an Admin and is back on the Free plan.")

# ---------- Admin / Telemarketing tab ----------

TELE_PLANS = ("free", "starter", "pro", "unlimited")  # plans an Admin may hand out

def admin_guard(request, db):
    u = current(request, db)
    if not can_admin(u):
        return None
    if u.email in CEO_EMAILS and not ceo_ok(request, u):
        return None  # the CEO must unlock with the PIN first
    return u

def admin_back(msg="", err=""):
    q = ("msg=" + quote_plus(msg)) if msg else ("err=" + quote_plus(err))
    return RedirectResponse("/admin?" + q, 303)

def price_line():
    return "; ".join(f"{PLANS[k]['name']} is {PLANS[k]['price']} {PLANS[k]['period']} ({PLANS[k]['note']})" for k in PAID)

@app.get("/admin")
def admin_page(request: Request, q: str = "", plan: str = "", msg: str = "", err: str = ""):
    with Session() as db:
        u = current(request, db)
        if not can_admin(u):
            return RedirectResponse("/", 303)
        if not admin_guard(request, db):
            return RedirectResponse("/ceo", 303)  # locked CEO: unlock on the control room first
        month = dt.datetime.utcnow().strftime("%Y-%m")
        users = db.query(User).order_by(User.id.desc()).all()
        ql = q.strip().lower()
        rows = [{
            "id": x.id, "email": x.email, "plan": x.plan, "created": x.created, "last_login": x.last_login,
            "protected": x.email in SPECIAL or bool(x.staff),
            "used": (x.free_used or 0) if x.plan == "free" else ((x.month_used or 0) if x.month == month else 0),
            "limit": (PLANS.get(x.plan) or {}).get("limit"),
        } for x in users if (not ql or ql in x.email) and (not plan or x.plan == plan)]
        stats = {"total": len(users), "free": sum(1 for x in users if x.plan == "free"), "paying": sum(1 for x in users if x.plan in PAID)}
        scripts = [{"title": s["title"], "when": s["when"], "text": s["text"].replace("{PRICES}", price_line())} for s in SCRIPTS]
        return page(request, "admin.html", u, msg=msg, err=err, rows=rows[:200], shown=len(rows), stats=stats,
                    scripts=scripts, q=q, fplan=plan, tele_plans=TELE_PLANS)

@app.post("/admin/plan")
def admin_set_plan(request: Request, user_id: int = Form(...), plan: str = Form(...)):
    with Session() as db:
        me = admin_guard(request, db)
        if not me:
            return RedirectResponse("/", 303)
        t = db.get(User, user_id)
        if not t or plan not in TELE_PLANS or is_special(t):
            return admin_back(err="Pick a valid account and plan.")
        email, who = t.email, me.email  # read before commit; commit expires the objects
        old, t.plan = t.plan, plan
        log_event(db, "admin_plan", email, f"{old} -> {plan} (set by {who})")
        db.commit()
    return admin_back(msg=f"Set {email} to {PLANS[plan]['name']}. If they have a Stripe subscription, Stripe may change it back on the next billing event.")
