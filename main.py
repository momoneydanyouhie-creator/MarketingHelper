import os, hashlib, secrets, datetime as dt
import stripe, anthropic
from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
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
    "free": {"name": "Free", "limit": 2, "price": "$0", "note": "2 generations, one time"},
    "starter": {"name": "Starter", "limit": 30, "price": "$29", "note": "30 per month"},
    "pro": {"name": "Pro", "limit": 150, "price": "$59", "note": "150 per month"},
    "unlimited": {"name": "Unlimited", "limit": None, "price": "$99", "note": "No cap"},
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
        return {"html": out, "left": None if u.plan == "unlimited" else remaining(u)}

@app.post("/checkout/{plan}")
def checkout(request: Request, plan: str):
    with Session() as db:
        u = current(request, db)
        if not u or plan not in PRICE_IDS or not PRICE_IDS[plan]:
            return RedirectResponse("/", 303)
        args = {"customer": u.customer} if u.customer else {"customer_email": u.email}
        s = stripe.checkout.Session.create(mode="subscription", line_items=[{"price": PRICE_IDS[plan], "quantity": 1}],
            client_reference_id=str(u.id), metadata={"plan": plan},
            success_url=f"{BASE_URL}/generate", cancel_url=f"{BASE_URL}/", **args)
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
