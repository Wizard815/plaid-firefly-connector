"""Single image, many roles: ROLE=gateway|worker|rh-agent|rh-live."""
import os


def build():
    from plaidproxy import oplog
    oplog.setup()
    role = os.environ.get("ROLE", "worker")
    secret = os.environ.get("INTERNAL_SECRET", "")
    if role == "gateway":
        from plaidproxy.gateway import create_app
        return create_app(monitor=True)
    if role == "worker":
        from plaidproxy.worker import create_app
        return create_app(autosync=os.environ.get("WORKER_AUTOSYNC", "1") != "0", ops_log=True)
    from plaidproxy.connectors.base import make_connector_app
    if role == "rh-agent":
        from plaidproxy.connectors.rh_agent import AgentConnector
        return make_connector_app(AgentConnector(config_url=os.environ.get("CONFIG_URL", ""), config_secret=secret), secret)
    if role == "rh-live":
        from plaidproxy.connectors.rh_live import LiveConnector
        return make_connector_app(LiveConnector(), secret)
    raise SystemExit(f"unknown ROLE {role!r}")


def app():  # uvicorn --factory plaidproxy.main:app
    return build()
