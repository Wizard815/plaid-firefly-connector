"""Single image, many roles: ROLE=gateway|goblin|rh-agent|rh-live."""
import os


def build():
    from goblinbank import oplog
    oplog.setup()
    role = os.environ.get("ROLE", "goblin")
    secret = os.environ.get("INTERNAL_SECRET", "")
    if role == "gateway":
        from goblinbank.gateway import create_app
        return create_app(monitor=True)
    if role == "goblin":
        from goblinbank.goblin import create_app
        return create_app(autosync=os.environ.get("GOBLIN_AUTOSYNC", "1") != "0", ops_log=True)
    from goblinbank.connectors.base import make_connector_app
    if role == "rh-agent":
        from goblinbank.connectors.rh_agent import AgentConnector
        return make_connector_app(AgentConnector(config_url=os.environ.get("CONFIG_URL", ""), config_secret=secret), secret)
    if role == "rh-live":
        from goblinbank.connectors.rh_live import LiveConnector
        return make_connector_app(LiveConnector(), secret)
    raise SystemExit(f"unknown ROLE {role!r}")


def app():  # uvicorn --factory goblinbank.main:app
    return build()
