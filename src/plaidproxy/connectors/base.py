"""Read-only connector contract.

Connectors expose ONLY these three reads. There is deliberately no method for
placing, cancelling or modifying orders anywhere in this codebase.
"""
from datetime import datetime, timezone
from typing import Protocol

from fastapi import FastAPI


class ReadOnlyConnector(Protocol):
    name: str

    def balances(self) -> dict: ...
    def positions(self) -> list[dict]: ...
    def history(self) -> list[dict]: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_connector_app(connector: ReadOnlyConnector, internal_secret: str) -> FastAPI:
    """Wrap a connector in an HTTP service exposing GET-only read endpoints."""
    from plaidproxy.security import require_internal
    from fastapi import Request

    app = FastAPI(title=f"connector:{connector.name}")

    @app.get("/health")
    def health():
        return {"ok": True, "connector": connector.name}

    def wrap(fn):
        def handler(request: Request):
            require_internal(request, internal_secret)
            try:
                return {"source": connector.name, "as_of": now_iso(), "data": fn()}
            except PermissionError as e:
                from fastapi import HTTPException
                raise HTTPException(403, str(e))
        return handler

    app.get("/balances")(wrap(connector.balances))
    app.get("/positions")(wrap(connector.positions))
    app.get("/history")(wrap(connector.history))
    return app
