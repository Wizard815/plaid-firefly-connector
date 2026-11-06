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

## What each goblin does
* **Plaid** (Huntington, Discover, Axos, Capital One, IRA): link via the UI, tokens Fernet-encrypted at rest (`FIELD_KEY`),
  `transactions/sync`, balances, auto-detected recurring bills, webhook JWT verification, relink mode for the Discover migration.
* **Subscriptions** (Wallos-style): manual + Plaid-detected, monthly/yearly totals, pause, `.ics` calendar feed, and push to CalDAV.
* **Money movement feed**: Atom feed of every transaction and balance change at `/feed/<name>/<feed-token>/money.atom`.
* **PayPal / Venmo / Cash App**: CSV import (no personal-account APIs exist). PayPal Credit may link via Plaid; check its directory.
* **UI** (`ui/index.html`, served by the gateway at `/`): sign in with goblin name + login token. Falls back to sample data when no gateway answers.

## Run
```bash
cp goblins.example.yaml goblins.yaml        # edit names / share / rh_agent / rh_live
python3 scripts/gen_compose.py               # prints login tokens, feed tokens, Orb key ONCE
# fill .secrets/plaid.env, <name>.integrations.env (CalDAV), <name>.rh-agent.env
docker compose -f docker-compose.generated.yml up --build
```
Gateway listens on `127.0.0.1:8080`; put Tailscale / a Cloudflare Tunnel in front, never the open internet.
Back up `.secrets/<name>.env`: losing `FIELD_KEY` means re-linking banks. Plaid webhooks need a public HTTPS URL
(`PLAID_WEBHOOK_URL` -> `/webhooks/plaid`); without it, use Sync now.

## Not done yet
* Plaid is untested against the real API (tests use mocks); try `PLAID_ENV=sandbox` first.
* rh-agent tool names/auth are unverified (Robinhood docs don't publish them); configure via env.
* CalDAV *reading* (event -> money amount) and investments holdings via Plaid.

`python3 -m pytest` runs the tests.
