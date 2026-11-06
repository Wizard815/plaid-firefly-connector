"""Single image, many roles: ROLE=gateway|goblin|rh-agent|rh-csv|rh-live."""
import os


def build():
    role = os.environ.get("ROLE", "goblin")
    secret = os.environ.get("INTERNAL_SECRET", "")
    if role == "gateway":
        from goblinbank.gateway import create_app
        return create_app()
    if role == "goblin":
        from goblinbank.goblin import create_app
        return create_app()
    from goblinbank.connectors.base import make_connector_app
    if role == "rh-agent":
        from goblinbank.connectors.rh_agent import AgentConnector
        return make_connector_app(AgentConnector(), secret)
    if role == "rh-csv":
        from goblinbank.connectors.rh_csv import CsvConnector
        return make_connector_app(CsvConnector(os.environ.get("CSV_DIR", "/csv")), secret)
    if role == "rh-live":
        from goblinbank.connectors.rh_live import LiveConnector
        return make_connector_app(LiveConnector(), secret)
    raise SystemExit(f"unknown ROLE {role!r}")


def app():  # uvicorn --factory goblinbank.main:app
    return build()
