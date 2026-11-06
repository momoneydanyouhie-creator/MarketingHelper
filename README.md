# TitlePen
FastAPI app: AI newsletters and marketing for title insurance companies, with Stripe subscriptions.

## Deploy on Render
1. Push this folder to a GitHub repo.
2. Render: New > Blueprint > pick the repo (uses render.yaml; creates the web service and Postgres).
3. Set env vars: BASE_URL (your Render URL), ANTHROPIC_API_KEY, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRICE_STARTER / _PRO / _UNLIMITED.
4. Stripe webhook endpoint: BASE_URL/stripe/webhook, events: checkout.session.completed, customer.subscription.updated, customer.subscription.deleted.
5. Link your bank under Stripe Settings > Payouts. Never put bank details in code.

## Notes
- Plan names, limits, and display prices are in PLANS (main.py); real prices live in Stripe.
- Add templates in templates_data.py.
- Test locally: pip install -r requirements.txt, set env vars, run `uvicorn main:app --reload`.
