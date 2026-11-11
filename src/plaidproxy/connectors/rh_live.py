"""rh-main-live: UNOFFICIAL robin_stocks access. OFF by default.

Safety rules enforced here:
  * explicit opt-in (RH_LIVE_ENABLED=1) or every call raises PermissionError
  * only read functions are imported/called; no order functions are referenced
  * hard minimum interval between upstream calls (default 3h); in between we
    serve the cached snapshot, so the dashboard cannot hammer Robinhood
This violates Robinhood's ToS; run it only in its own container/volume.
"""
import os
import time


class LiveConnector:
    name = "rh-main-live"

    def __init__(self, client=None, min_interval: float | None = None, clock=time.monotonic):
        self.enabled = os.environ.get("RH_LIVE_ENABLED") == "1"
        self.min_interval = min_interval if min_interval is not None else float(os.environ.get("RH_LIVE_MIN_INTERVAL", 3 * 3600))
        self._client = client
        self._clock = clock
        self._last = None
        self._cache: dict = {}

    def _login(self):
        if self._client is None:
            import robin_stocks.robinhood as rh  # lazy: optional dependency
            rh.login(os.environ["RH_USERNAME"], os.environ["RH_PASSWORD"],
                     mfa_code=None, store_session=True, pickle_path=os.environ.get("RH_SESSION_DIR", "/session"))
            self._client = _ReadOnlyRH(rh)
        return self._client

    def _snapshot(self) -> dict:
        if not self.enabled:
            raise PermissionError("rh-main-live is disabled (set RH_LIVE_ENABLED=1 to opt in)")
        now = self._clock()
        if self._last is not None and now - self._last < self.min_interval:
            return self._cache
        c = self._login()
        self._cache = {"balances": c.balances(), "positions": c.positions(), "history": c.history()}
        self._last = now
        return self._cache

    def balances(self): return self._snapshot()["balances"]
    def positions(self): return self._snapshot()["positions"]
    def history(self): return self._snapshot()["history"]


class _ReadOnlyRH:
    """Whitelist adapter: the only robin_stocks calls this project makes."""

    def __init__(self, rh):
        self._rh = rh

    def balances(self):
        p = self._rh.profiles.load_portfolio_profile() or {}
        return {"equity": p.get("equity"), "market_value": p.get("market_value"),
                "withdrawable_amount": p.get("withdrawable_amount")}

    def positions(self):
        h = self._rh.account.build_holdings() or {}
        return [{"symbol": s, "quantity": float(v.get("quantity", 0)), "equity": v.get("equity")} for s, v in h.items()]

    def history(self):
        return [{"id": o.get("id"), "symbol": None, "state": o.get("state"), "created_at": o.get("created_at")}
                for o in (self._rh.orders.get_all_stock_orders() or [])]
