# Goblin-Bank

Self-hosted family finance. **One gateway, one private container per person.**

```
Pondering Orb ──(read-only key)──► CoinGateway ──► goblin-alice ─► alice-rh-csv / rh-agent / rh-live
                                        │──────► goblin-bob   ─► ...
```

* **CoinGateway** (`ROLE=gateway`): login + routing, Plaid webhook relay (item_id → goblin map only),
  and `/api/v1/summary` for Pondering Orb. Stores no tokens, balances or transactions.
* **Goblin** (`ROLE=goblin`): one person's data. Shares with the gateway/Orb only what `share:` allows.
* **Connectors** (one container each, read-only, GET-only, secret-protected):
  * `rh-agent` – official Robinhood MCP (agent account). Fails closed: only tools in `RH_MCP_READ_TOOLS` are callable.
  * `rh-csv` – parses Robinhood activity CSVs dropped in `data/<name>/robinhood-csv/`. No account access. Deduped.
  * `rh-live` – **unofficial** `robin_stocks`. Off by default, opt-in, own container + session volume, min 3h between upstream calls. Violates Robinhood ToS.
* Main portfolio view = live if enabled and answering, otherwise CSV (with `as_of`).

One image, many roles (`ROLE=`), so the repo stays small.

## Run
```bash
cp goblins.example.yaml goblins.yaml        # edit names / share / rh_agent / rh_live
python3 scripts/gen_compose.py               # prints login tokens + Orb key ONCE
# fill .secrets/<name>.rh-agent.env (and .rh-live.env if enabled)
docker compose -f docker-compose.generated.yml up --build
```
Gateway listens on `127.0.0.1:8080`; put Tailscale / a Cloudflare Tunnel in front, never the open internet.
`curl -H "Authorization: Bearer <token>" localhost:8080/g/alice/robinhood/main`

## Not done yet
* Plaid connector (Huntington / Discover / Axos / Capital One) and token encryption at rest.
* Plaid webhook JWT verification — the webhook endpoint returns 501 until it exists.
* rh-agent tool names/auth are unverified (Robinhood docs don't publish them); configure via env.
* CalDAV → money tracker and the Wallos-style subscription tracker; the dashboard UI.

`python3 -m pytest` runs the tests.
