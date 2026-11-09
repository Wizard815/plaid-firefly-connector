"""Minimal Plaid client over httpx (no SDK) + webhook verification.

Env: PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV=sandbox|production, PLAID_WEBHOOK_URL (optional).
"""
import base64
import hashlib
import json
import os
import time

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

HOSTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}


class PlaidError(Exception):
    pass


class PlaidLimit(PlaidError):
    """Our own guard said no (daily call cap, monthly budget). Not a problem with the bank."""


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Plaid:
    def __init__(self, transport: httpx.BaseTransport | None = None, usage=None):
        self.usage = usage   # object with before(path) -> may raise PlaidError, and record(path)
        self.client_id = os.environ.get("PLAID_CLIENT_ID", "")
        self.secret = os.environ.get("PLAID_SECRET", "")
        self.header_auth = os.environ.get("PLAID_AUTH") == "header"       # send credentials as PLAID-CLIENT-ID / PLAID-SECRET headers instead of in the body
        self.base = os.environ.get("PLAID_BASE_URL") or HOSTS.get(os.environ.get("PLAID_ENV", "sandbox"), HOSTS["sandbox"])   # PLAID_BASE_URL: test servers only
        self.webhook_url = os.environ.get("PLAID_WEBHOOK_URL", "")
        self.version = os.environ.get("PLAID_VERSION", "2020-09-14")        # pinned, so a change of the default version in the Plaid dashboard cannot change what this app receives
        self.redirect_uri = os.environ.get("PLAID_REDIRECT_URI", "")      # needed for banks that sign you in on their own site (OAuth); must be HTTPS and registered in the Plaid dashboard
        self.http = httpx.Client(timeout=30, transport=transport)
        self._keys: dict[str, dict] = {}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.secret)

    def post(self, path: str, body: dict) -> dict:
        if not self.configured:
            raise PlaidError("Plaid not configured (PLAID_CLIENT_ID / PLAID_SECRET)")
        if self.usage:
            self.usage.before(path)
            self.usage.record(path)
        if self.header_auth:
            r = self.http.post(self.base + path, json=body, headers={"PLAID-CLIENT-ID": self.client_id, "PLAID-SECRET": self.secret, "Plaid-Version": self.version})
        else:
            r = self.http.post(self.base + path, json={"client_id": self.client_id, "secret": self.secret, **body}, headers={"Plaid-Version": self.version})
        if r.status_code >= 400:
            try:
                e = r.json()
                raise PlaidError(f"{e.get('error_code')}: {e.get('error_message')}")
            except ValueError:
                raise PlaidError(f"HTTP {r.status_code}")
        return r.json()

    def link_token(self, user_id: str, update_item_token: str | None = None) -> str:
        body = {"user": {"client_user_id": user_id}, "client_name": "Goblin-Bank", "country_codes": ["US"], "language": "en"}
        if update_item_token:           # relink mode (e.g. Discover -> Capital One migration)
            body["access_token"] = update_item_token
        else:
            body["products"] = ["transactions"]
            body["optional_products"] = ["investments", "liabilities"]
        if self.webhook_url:
            body["webhook"] = self.webhook_url
        if self.redirect_uri:
            body["redirect_uri"] = self.redirect_uri
        return self.post("/link/token/create", body)["link_token"]

    def exchange(self, public_token: str) -> dict:
        return self.post("/item/public_token/exchange", {"public_token": public_token})

    def accounts(self, access_token: str) -> dict:
        return self.post("/accounts/get", {"access_token": access_token})

    def sync(self, access_token: str, cursor: str | None):
        body = {"access_token": access_token, "count": 500}
        if cursor:
            body["cursor"] = cursor
        return self.post("/transactions/sync", body)

    def recurring(self, access_token: str, account_ids: list[str]) -> dict:
        return self.post("/transactions/recurring/get", {"access_token": access_token, "account_ids": account_ids})

    def holdings(self, access_token: str) -> dict:
        return self.post("/investments/holdings/get", {"access_token": access_token})

    # ---- webhook verification (https://plaid.com/docs/api/webhooks/webhook-verification/) ----
    def verify_webhook(self, body: bytes, token: str | None, now: float | None = None) -> bool:
        """True only if the JWT is ES256, from Plaid's key, fresh (<5min) and matches the body hash."""
        try:
            h_b64, p_b64, s_b64 = (token or "").split(".")
            header, payload = json.loads(_b64d(h_b64)), json.loads(_b64d(p_b64))
            if header.get("alg") != "ES256" or not header.get("kid"):
                return False
            jwk = self._keys.get(header["kid"]) or self.post("/webhook_verification_key/get", {"key_id": header["kid"]})["key"]
            self._keys[header["kid"]] = jwk
            if jwk.get("expired_at"):
                return False
            pub = ec.EllipticCurvePublicNumbers(
                int.from_bytes(_b64d(jwk["x"]), "big"), int.from_bytes(_b64d(jwk["y"]), "big"), ec.SECP256R1()).public_key()
            sig = _b64d(s_b64)
            der = utils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
            pub.verify(der, f"{h_b64}.{p_b64}".encode(), ec.ECDSA(hashes.SHA256()))
            if (now or time.time()) - payload.get("iat", 0) > 300:
                return False
            return payload.get("request_body_sha256") == hashlib.sha256(body).hexdigest()
        except Exception:
            return False
