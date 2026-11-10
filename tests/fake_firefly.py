"""A small in-memory stand-in for the parts of the Firefly III API Goblin Bank uses, following Firefly III's own validation (v1 API)."""
import json

import httpx


class FakeFirefly:
    def __init__(self, token="good-token"):
        self.token, self.accounts, self.groups, self.calls = token, {}, {}, []
        self.next_acct, self.next_group = 1, 100

    # --- helpers
    def balance(self, aid):
        a = self.accounts[aid]
        bal = float(a.get("opening_balance") or 0)
        for g in self.groups.values():
            for t in g["transactions"]:
                if t["type"] == "withdrawal" and t.get("source_id") == aid: bal -= float(t["amount"])
                if t["type"] == "deposit" and t.get("destination_id") == aid: bal += float(t["amount"])
                if t["type"] == "transfer":
                    if t.get("source_id") == aid: bal -= float(t["amount"])
                    if t.get("destination_id") == aid: bal += float(t["amount"])
        return round(bal, 2)

    def add_account(self, name, role="defaultAsset", opening="0"):
        aid = self.next_acct; self.next_acct += 1
        self.accounts[aid] = {"name": name, "type": "asset", "account_role": role, "opening_balance": opening}
        return aid

    def external_ids(self):
        return [t.get("external_id") for g in self.groups.values() for t in g["transactions"]]

    def transport(self):
        return httpx.MockTransport(self.handle)

    # --- the API
    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        if req.headers.get("authorization") != "Bearer " + self.token:
            return httpx.Response(401, json={"message": "Unauthenticated."})
        p = req.url.path.removeprefix("/api/v1")
        body = json.loads(req.content) if req.content else {}
        if req.method == "GET" and p == "/about":
            return httpx.Response(200, json={"data": {"version": "6.7.9", "api_version": "2.2.0"}})
        if req.method == "GET" and p == "/accounts":
            data = [{"id": str(i), "attributes": {"name": a["name"], "account_role": a["account_role"], "current_balance": f"{self.balance(i):.2f}"}} for i, a in self.accounts.items()]
            return httpx.Response(200, json={"data": data, "meta": {"pagination": {"total_pages": 1}}})
        if req.method == "GET" and p.startswith("/accounts/") and p.count("/") == 2:
            i = int(p.split("/")[2])
            a = self.accounts.get(i)
            return httpx.Response(200, json={"data": {"id": str(i), "attributes": {**a, "current_balance": f"{self.balance(i):.2f}"}}}) if a else httpx.Response(404, json={"message": "Resource not found"})
        if req.method == "GET" and p.startswith("/accounts/") and p.endswith("/transactions"):
            i = int(p.split("/")[2])
            data = [{"id": str(g), "attributes": {"transactions": v["transactions"]}} for g, v in self.groups.items() if any(t.get("source_id") == i or t.get("destination_id") == i for t in v["transactions"])]
            return httpx.Response(200, json={"data": data, "meta": {"pagination": {"total_pages": 1}}})
        if req.method == "PUT" and p.startswith("/accounts/"):
            i = int(p.split("/")[2])
            self.accounts[i].update({k: v for k, v in body.items() if k in ("opening_balance", "opening_balance_date")})
            return httpx.Response(200, json={"data": {"id": str(i)}})
        if req.method == "POST" and p == "/accounts":
            if any(a["name"] == body.get("name") for a in self.accounts.values()):
                return httpx.Response(422, json={"message": "The given data was invalid.", "errors": {"name": ["This account name is already in use."]}})
            if body.get("type") == "asset" and body.get("account_role") not in ("defaultAsset", "sharedAsset", "savingAsset", "ccAsset", "cashWalletAsset"):
                return httpx.Response(422, json={"message": "The given data was invalid.", "errors": {"account_role": ["required"]}})
            if body.get("account_role") == "ccAsset" and not (body.get("credit_card_type") and body.get("monthly_payment_date")):
                return httpx.Response(422, json={"message": "The given data was invalid.", "errors": {"credit_card_type": ["required"]}})
            aid = self.next_acct; self.next_acct += 1
            self.accounts[aid] = {k: body.get(k) for k in ("name", "type", "account_role", "opening_balance", "opening_balance_date", "currency_code", "include_net_worth")}
            return httpx.Response(200, json={"data": {"id": str(aid), "attributes": self.accounts[aid]}})
        if req.method == "POST" and p == "/transactions":
            t = body["transactions"][0]
            for k in ("type", "date", "amount", "description"):
                if not t.get(k):
                    return httpx.Response(422, json={"message": "The given data was invalid.", "errors": {k: ["required"]}})
            for key in ("source_id", "destination_id"):
                if t.get(key) and int(t[key]) not in self.accounts:
                    return httpx.Response(422, json={"message": "The given data was invalid.", "errors": {key: ["does not exist"]}})
            if body.get("error_if_duplicate_hash") and t.get("external_id") in self.external_ids():
                return httpx.Response(422, json={"message": "Duplicate of transaction #" + str(next(g for g, v in self.groups.items() if v["transactions"][0].get("external_id") == t["external_id"])) + "."})
            gid = self.next_group; self.next_group += 1
            self.groups[gid] = {"transactions": [t], "apply_rules": body.get("apply_rules")}
            return httpx.Response(200, json={"data": {"id": str(gid), "attributes": {"transactions": [t]}}})
        if req.method == "PUT" and p.startswith("/transactions/"):
            gid = int(p.split("/")[2])
            if gid not in self.groups:
                return httpx.Response(404, json={"message": "Resource not found"})
            self.groups[gid]["transactions"] = [{**self.groups[gid]["transactions"][0], **body["transactions"][0]}]
            return httpx.Response(200, json={"data": {"id": str(gid)}})
        if req.method == "DELETE" and p.startswith("/transactions/"):
            gid = int(p.split("/")[2])
            if gid not in self.groups:
                return httpx.Response(404, json={"message": "Resource not found"})
            del self.groups[gid]
            return httpx.Response(204)
        if req.method == "GET" and p == "/search/transactions":
            q = req.url.params.get("query", "")
            ext = q.split('external_id_is:"')[1].rstrip('"') if "external_id_is" in q else ""
            hits = [{"id": str(g)} for g, v in self.groups.items() if v["transactions"][0].get("external_id") == ext]
            return httpx.Response(200, json={"data": hits})
        return httpx.Response(404, json={"message": "unknown route " + p})
