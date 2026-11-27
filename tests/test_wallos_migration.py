"""Moving Wallos subscriptions into Firefly III bills: a dry run writes nothing, --apply adds each once, and odd cycles are explained, not guessed."""
import importlib.util
import json
from pathlib import Path

import httpx

from plaidproxy.firefly import Firefly

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("migrate_from_wallos", ROOT / "scripts" / "migrate_from_wallos.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SUBS = [
    {"name": "Netflix", "price": 15.49, "cycle": 3, "frequency": 1, "next_payment": "2026-12-05", "payment_method_name": "Visa", "category_name": "Entertainment"},
    {"name": "Domain", "price": 12, "cycle": 4, "frequency": 1, "next_payment": "2027-03-01"},
    {"name": "Car wash", "price": 30, "cycle": 3, "frequency": 3, "next_payment": "2026-12-20"},
    {"name": "Every 10 days", "price": 5, "cycle": 1, "frequency": 10, "next_payment": "2026-12-01"},
    {"name": "Old gym", "price": 40, "cycle": 3, "frequency": 1, "next_payment": "2026-12-01", "inactive": 1},
    {"name": "spotify", "price": 11.99, "cycle": 3, "frequency": 1, "next_payment": "2026-12-09"},
]


def fake():
    posted = []

    def handle(req: httpx.Request):
        if req.url.path.endswith("get_subscriptions.php"):
            if req.url.params.get("api_key") != "wk":
                return httpx.Response(401, json={"success": False, "title": "Invalid API key"})
            return httpx.Response(200, json={"success": True, "subscriptions": SUBS})
        assert req.headers["authorization"] == "Bearer ft"
        if req.method == "GET" and req.url.path == "/api/v1/bills":
            existing = [{"id": "1", "attributes": {"name": "Spotify"}}] + [{"id": str(i + 2), "attributes": {"name": b["name"]}} for i, b in enumerate(posted)]
            return httpx.Response(200, json={"data": existing, "meta": {"pagination": {"total_pages": 1}}})
        if req.method == "POST" and req.url.path == "/api/v1/bills":
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"data": {"id": "9"}})
        return httpx.Response(404, json={})

    return httpx.MockTransport(handle), posted


def test_wallos_dry_run_then_apply_once():
    m = load()
    t, posted = fake()
    subs = m.wallos_subscriptions("http://wallos", "wk", transport=t)
    ff = Firefly("http://ff", "ft", transport=t)
    dry = m.run(subs, ff, False, "USD", 0.05, "Subscriptions", False)
    assert posted == [] and len(dry["would_add"]) == 3
    assert dry["already"] == ["spotify"]
    assert {n for n, _ in dry["skipped"]} == {"Every 10 days", "Old gym"}
    out = m.run(subs, ff, True, "USD", 0.05, "Subscriptions", False)
    assert out["added"] == ["Netflix", "Domain", "Car wash"]
    netflix, domain, wash = posted
    assert (netflix["repeat_freq"], netflix["skip"], netflix["amount_min"], netflix["amount_max"]) == ("monthly", 0, "14.72", "16.26")
    assert "Paid with: Visa" in netflix["notes"] and netflix["object_group_title"] == "Subscriptions"
    assert (domain["repeat_freq"], wash["repeat_freq"]) == ("yearly", "quarterly")
    again = m.run(subs, ff, True, "USD", 0.05, "Subscriptions", False)
    assert again["added"] == [] and len(posted) == 3


def test_wallos_wrong_key_says_so():
    m = load()
    t, _ = fake()
    try:
        m.wallos_subscriptions("http://wallos", "nope", transport=t)
    except m.Stop as e:
        assert "WALLOS_API_KEY" in str(e)
    else:
        raise AssertionError("want Stop")
