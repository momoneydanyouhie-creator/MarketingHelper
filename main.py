import os, time, hashlib, secrets, datetime as dt
import stripe, anthropic
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.orm import declarative_base, sessionmaker
from templates_data import TEMPLATES

E = os.environ.get
stripe.api_key = E("STRIPE_SECRET_KEY")
ai = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
MODEL = E("MODEL", "claude-sonnet-5-5")
BASE_URL = E("BASE_URL", "http://localhost:8000")

# Display prices are labels only; real prices live in Stripe.
PLANS = {
    "free": {"name": "Free", "limit": 2, "price": "$0", "period": "", "note": "2 generations, one time"},
    "starter": {"name": "Starter", "limit": 30, "price": "$29", "period": "per month", "note": "30 generations per month"},
    "pro": {"name": "Pro", "limit": 75, "price": "$59", "period": "every 3 months", "note": "75 generations per month"},
    "unlimited": {"name": "Unlimited", "limit": 150, "price": "$99", "period": "per year", "note": "150 generations per month"},
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

Base.metadata.create_all(engine)

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=E("SECRET_KEY", "dev-only"), max_age=60 * 60 * 24 * 30)
tpl = Jinja2Templates(directory=".")

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()

def check_pw(pw, stored):
    return secrets.compare_digest(hash_pw(pw, stored.split("$")[0]), stored)

def current(request, db):
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None

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

def page(request, name, u, **ctx):
    left = None if not u else remaining(u)
    return tpl.TemplateResponse(request, name, {"user": u, "plans": PLANS, "left": left, "templates": TEMPLATES, **ctx})

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
        u = User(email=email, pw=hash_pw(password))
        db.add(u); db.commit()
        request.session["uid"] = u.id
    return RedirectResponse("/generate", 303)

@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    with Session() as db:
        u = db.query(User).filter_by(email=email.strip().lower()).first()
        if not u or not check_pw(password, u.pw):
            return RedirectResponse("/?error=Email+or+password+is+incorrect", 303)
        request.session["uid"] = u.id
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
        if remaining(u) <= 0:
            return JSONResponse({"error": "You're out of generations. Upgrade your plan to keep going.", "upgrade": True}, 402)
        if not prompt:
            return JSONResponse({"error": "Type what you want written first."}, 400)
        r = ai.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM,
                               messages=[{"role": "user", "content": f"Format: {kind}\n\nRequest: {prompt}"}])
        out = text_of(r)
        spend(u); db.commit()
        return {"html": out, "left": remaining(u)}

@app.post("/checkout/{plan}")
def checkout(request: Request, plan: str):
    with Session() as db:
        u = current(request, db)
        if not u or plan not in PRICE_IDS or not PRICE_IDS[plan]:
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
            if u:
                u.plan, u.customer = obj["metadata"]["plan"], obj["customer"]
        elif ev["type"] in ("customer.subscription.updated", "customer.subscription.deleted"):
            u = db.query(User).filter_by(customer=obj["customer"]).first()
            if u:
                price = obj["items"]["data"][0]["price"]["id"]
                active = ev["type"] != "customer.subscription.deleted" and obj["status"] in ("active", "trialing")
                u.plan = next((p for p, i in PRICE_IDS.items() if i == price), "free") if active else "free"
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
        if u.plan != "free":
            return RedirectResponse("/settings?err=Cancel+your+subscription+under+Manage+billing+first", 303)
        if confirm.strip().lower() != u.email:
            return RedirectResponse("/settings?err=That+email+does+not+match+your+account", 303)
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
    paid = ", ".join(f"{p['name']} {p['price']} {p['period']} ({p['note']})" for k, p in PLANS.items() if k != "free")
    help_line = f" suggest contacting {SUPPORT}" if SUPPORT else " suggest reaching out to the site owner"
    return (f"You are {name}, an AI assistant on the TitlePen website. You are an AI, not a human. If anyone asks whether you are a real person or a bot, "
        f"say plainly that you are an AI assistant. Never claim to be a real person or to have a job, family, or personal experiences. {focus}\n\n"
        "TitlePen lets title insurance and real estate professionals generate newsletters, emails, social posts, flyers, and other marketing pieces, then edit, copy, or download them. "
        f"It was created by Peyton Moore. Plans: Free (2 generations total), {paid}. Each click of Generate uses one generation; editing, copying, and downloading are free. "
        "The three-line menu at the top right has Generator, Templates, Plans and pricing, Help and tips, About us, Settings, Manage billing (paid plans), and Log out. "
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
