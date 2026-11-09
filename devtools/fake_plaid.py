"""A synthetic Plaid server for offline end-to-end runs. NOT Plaid: it follows the response shapes in
Plaid's public docs as I understand them, so it proves our code is self-consistent, not that real Plaid agrees.
Run: uvicorn devtools.fake_plaid:app --port 9101
"""
import base64
import hashlib
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi import FastAPI, HTTPException, Request

KEY = ec.generate_private_key(ec.SECP256R1())
NUMS = KEY.public_key().public_numbers()
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
JWK = {"kty": "EC", "crv": "P-256", "kid": "synthetic-key", "expired_at": None,
       "x": b64(NUMS.x.to_bytes(32, "big")), "y": b64(NUMS.y.to_bytes(32, "big"))}

CLIENT, SECRET = "synthetic-client", "synthetic-secret"
T = lambda i, acct, date, name, amt, merchant=None: {"transaction_id": i, "account_id": acct, "date": date, "name": name,
                                                      "merchant_name": merchant, "amount": amt, "pending": False, "iso_currency_code": "USD"}
PAGES = [  # Plaid convention: positive amount = money OUT
    [T("t1", "chk", "2026-11-01", "NETFLIX.COM", 15.49, "Netflix"), T("t2", "chk", "2026-11-02", "ACME PAYROLL", -2400.00)],
    [T("t3", "crd", "2026-11-03", "WHOLEFDS", 86.12, "Whole Foods"), T("t4", "chk", "2026-11-04", "GYM CO", 40.00, "Gym Co")],
]
state = {"calls": {}}
app = FastAPI()


@app.middleware("http")
async def count(request: Request, call_next):
    state["calls"][request.url.path] = state["calls"].get(request.url.path, 0) + 1
    return await call_next(request)


async def authed(request: Request) -> dict:
    body = await request.json()
    if body.get("client_id") != CLIENT or body.get("secret") != SECRET:
        raise HTTPException(400, detail={"error_code": "INVALID_API_KEYS", "error_message": "invalid client_id or secret"})
    return body


@app.exception_handler(HTTPException)
async def err(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(exc.detail if isinstance(exc.detail, dict) else {"error_code": "ERR", "error_message": str(exc.detail)}, status_code=exc.status_code)


@app.post("/link/token/create")
async def link(request: Request):
    await authed(request)
    return {"link_token": "link-sandbox-synthetic", "expiration": "2026-11-08T00:00:00Z"}


@app.post("/item/public_token/exchange")
async def exchange(request: Request):
    b = await authed(request)
    if not str(b.get("public_token", "")).startswith("public-sandbox"):
        raise HTTPException(400, detail={"error_code": "INVALID_PUBLIC_TOKEN", "error_message": "bad public token"})
    return {"access_token": "access-sandbox-synthetic", "item_id": "item-synth-1"}


@app.post("/accounts/get")
async def accounts(request: Request):
    await authed(request)
    A = lambda i, n, t, cur: {"account_id": i, "name": n, "mask": str(abs(hash(i)) % 10000).zfill(4), "type": t, "balances": {"current": cur, "iso_currency_code": "USD"}}
    return {"accounts": [A("chk", "Plaid Checking", "depository", 1250.55), A("sav", "Plaid Saving", "depository", 8000.00),
                         A("crd", "Plaid Credit Card", "credit", 430.20), A("brk", "Plaid Brokerage", "investment", 5000.00)]}


@app.post("/transactions/sync")
async def sync(request: Request):
    b = await authed(request)
    k = int(b["cursor"][1:]) if b.get("cursor") else 0
    if k < len(PAGES):
        return {"added": PAGES[k], "modified": [], "removed": [], "next_cursor": f"c{k + 1}", "has_more": k + 1 < len(PAGES)}
    return {"added": [], "modified": [], "removed": [], "next_cursor": f"c{k}", "has_more": False}


@app.post("/transactions/recurring/get")
async def recurring(request: Request):
    await authed(request)
    S = lambda i, m, amt, nxt: {"stream_id": i, "merchant_name": m, "description": m.upper(), "frequency": "MONTHLY", "is_active": True,
                                "last_amount": {"amount": amt}, "predicted_next_date": nxt, "last_date": "2026-11-01"}
    return {"outflow_streams": [S("s1", "Netflix", 15.49, "2026-12-01"), S("s2", "Gym Co", 40.0, "2026-12-04")]}


@app.post("/investments/holdings/get")
async def holdings(request: Request):
    await authed(request)
    return {"securities": [{"security_id": "sec1", "ticker_symbol": "VTI", "name": "Vanguard Total Stock Market"}],
            "holdings": [{"account_id": "brk", "security_id": "sec1", "quantity": 20, "institution_price": 250.0, "institution_value": 5000.0}]}


@app.post("/item/remove")
async def remove(request: Request):
    await authed(request)
    return {"request_id": "r"}


@app.post("/webhook_verification_key/get")
async def key(request: Request):
    await authed(request)
    return {"key": JWK}


# ---- test controls (not part of Plaid) ----
@app.post("/_test/add_txn")
async def add_txn(request: Request):
    PAGES.append([T(f"t{len(PAGES) + 10}", "chk", "2026-11-06", "NEW COFFEE", 4.75, "Coffee Shop")])
    return {"pages": len(PAGES)}


@app.post("/_test/webhook")
async def webhook(request: Request):
    body = json.dumps({"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE", "item_id": "item-synth-1"}).encode()
    h = b64(json.dumps({"alg": "ES256", "kid": "synthetic-key"}).encode())
    p = b64(json.dumps({"iat": int(time.time()), "request_body_sha256": hashlib.sha256(body).hexdigest()}).encode())
    r, s = utils.decode_dss_signature(KEY.sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256())))
    return {"body": body.decode(), "token": f"{h}.{p}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"}


@app.get("/_test/calls")
async def calls():
    return state["calls"]
