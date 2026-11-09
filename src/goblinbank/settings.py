"""Per-goblin settings stored in the DB (secrets Fernet-encrypted). Env vars are the fallback."""
import os
import re

from goblinbank import crypto

SOURCES = ("auto", "plaid", "csv", "live")
SHAREABLE = ("net_worth", "subscriptions", "bills", "bill_list", "alerts", "alert_list", "savings", "goals", "spending", "robinhood_main", "robinhood_agent")
# key -> (kind, secret, env fallback)
SPEC = {
    "share": ("list", False, "SHARE"),
    "rh_main_source": ("choice", False, ""),
    "plaid_client_id": ("str", False, "PLAID_CLIENT_ID"), "plaid_secret": ("str", True, "PLAID_SECRET"), "plaid_env": ("str", False, "PLAID_ENV"),
    "caldav_url": ("str", False, "CALDAV_URL"), "caldav_user": ("str", False, "CALDAV_USER"),
    "caldav_password": ("str", True, "CALDAV_PASSWORD"),
    "rh_mcp_url": ("str", False, "RH_MCP_URL"), "rh_mcp_token": ("str", True, "RH_MCP_TOKEN"),
    "rh_mcp_read_tools": ("str", False, "RH_MCP_READ_TOOLS"),
    "rh_mcp_tool_balances": ("str", False, "RH_MCP_TOOL_BALANCES"),
    "rh_mcp_tool_positions": ("str", False, "RH_MCP_TOOL_POSITIONS"),
    "rh_mcp_tool_history": ("str", False, "RH_MCP_TOOL_HISTORY"),
    # Plaid cost guard. Rates are YOURS to enter: Plaid shows them in its dashboard, not in any public price list.
    "plaid_rate_item_month": ("num", False, ""), "plaid_use_recurring": ("onoff", False, ""), "plaid_min_minutes": ("int", False, ""), "plaid_budget_month": ("num", False, ""), "plaid_daily_cap": ("int", False, ""), "agent_provider": ("int", False, ""), "alert_missed": ("onoff", False, ""), "sync_minutes": ("int", False, ""), "alert_maturity_days": ("int", False, ""), "notify_url": ("str", True, ""), "notify_min_severity": ("sev", False, ""), "notify_url_cds": ("str", True, ""), "notify_min_severity_cds": ("sev", False, ""), "alert_untracked": ("onoff", False, ""),
}


# Out of the box Plaid is kept cheap: at most $5 a month and a modest daily call cap; each bank is asked on a schedule that suits what it holds (see plaidplan.py).
DEFAULTS = {"plaid_budget_month": "5", "plaid_min_minutes": "0", "plaid_daily_cap": "300"}


def get(db, key: str) -> str:
    kind, secret, env = SPEC[key]
    row = db.one("SELECT value FROM settings WHERE key=?", (key,))
    if row is not None:
        return crypto.decrypt(row["value"]) if secret and row["value"] else (row["value"] or "")
    if key in DEFAULTS:
        return DEFAULTS[key]
    return os.environ.get(env, "") if env else ("auto" if key == "rh_main_source" else "on" if kind == "onoff" else ("info" if key.endswith("_cds") else "warning") if kind == "sev" else "")


def get_list(db, key: str) -> list[str]:
    return [x.strip() for x in get(db, key).split(",") if x.strip()]


def validate(key: str, value) -> str:
    if key not in SPEC:
        raise ValueError(f"unknown setting {key!r}")
    kind = SPEC[key][0]
    if kind == "list":
        items = value if isinstance(value, list) else [x for x in str(value).split(",") if x]
        bad = [i for i in items if i not in SHAREABLE]
        if bad:
            raise ValueError(f"share may only contain {', '.join(SHAREABLE)}")
        return ",".join(items)
    v = str(value).strip()
    if kind == "sev" and v not in ("", "info", "warning", "critical"):
        raise ValueError(f"{key} must be info, warning or critical")
    if kind == "onoff" and v not in ("on", "off"):
        raise ValueError(f"{key} must be on or off")
    if kind == "choice" and v not in SOURCES:
        raise ValueError(f"{key} must be one of {', '.join(SOURCES)}")
    if kind in ("num", "int") and v:
        try:
            n = float(v)
            assert n >= 0 and (kind == "num" or n == int(n))
        except (ValueError, AssertionError):
            raise ValueError(f"{key} must be a non-negative {'number' if kind == 'num' else 'whole number'}")
    if key == "plaid_env" and v not in ("", "sandbox", "production"):
        raise ValueError("plaid_env must be sandbox or production")
    if key == "plaid_client_id" and v and not re.fullmatch(r"[A-Za-z0-9]{8,64}", v):
        raise ValueError("that does not look like a Plaid Client ID (letters and digits only)")
    if key == "plaid_secret" and v and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", v):
        raise ValueError("that does not look like a Plaid secret (letters and digits only)")
    if len(v) > 2000:
        raise ValueError(f"{key} too long")
    if ("_url" in key) and v and not v.startswith(("http://", "https://")):
        raise ValueError(f"{key} must start with http:// or https://")
    return v


def put(db, key: str, value) -> None:
    v = validate(key, value)
    secret = SPEC[key][1]
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?,?)", (key, crypto.encrypt(v) if secret and v else v, int(secret)))


def view(db) -> dict:
    """What the UI sees: secrets are never returned, only whether they are set."""
    out = {}
    for k, (kind, secret, _) in SPEC.items():
        v = get(db, k)
        out[k] = {"set": bool(v)} if secret else (get_list(db, k) if kind == "list" else v)
    return out
