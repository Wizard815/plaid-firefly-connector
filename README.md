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
* **Plaid** (Huntington, Discover, Axos, Capital One, IRA): link in Settings, tokens encrypted at rest, transactions sync, holdings, recurring-bill detection, webhook signature checks, relink mode.
* **Money movement**: search + filter (account, category, direction, amount range, dates, sort), totals for whatever you filtered, pagination, CSV export, per-payment categories with "remember for similar" rules and keyword auto-categorizing.
* **Accounts**: drill into any account (history, 30-day in/out, its bills, its transactions), add accounts by hand (cash, anything Plaid can't reach), minimum-balance warnings.
* **Subscriptions**: every interval (daily to every 2 years, or custom "every N days/weeks/months/years"), paid-by-me or someone else (with their name), bound to any account (Plaid or manual) or a free-text payment method, end dates, reminders. Detects steady payments in your history and offers them with your name/interval/account. Flags bound bills that stop showing up (counts consecutive misses).
* **Alerts** (switchable in Settings): repeating payments not yet in Subscriptions, overdrawn / below-minimum accounts, accounts projected to run short, missed payments, CDs maturing, bills due soon.
* **Overview**: net worth, adjustable "bills in the next N days" window (1 day to 10 years, repeats expanded), spending by category, alerts.
* **Savings**: where the money sits (bank savings/CD accounts appear by themselves, plus anything you add by hand) and what it is for. Goals earmark part of the savings, so *free* money = saved minus earmarked. A new goal takes whatever is free; with none free it starts unfunded and shows how long saving takes. Move earmarks between goals, auto-fill by priority, mark bought.
* **Budgets & notes**: a monthly plan (expected income - bills - budgets - goal contributions = left to assign; a budget covers its category's bills, never double counted), income sources (steady deposits are detected), your notes, goals, and an agent that drafts notes from your real data. Link a goal or note to an agent chat.
* **Agent**: saved multi-chat (rename, delete, context meter, automatic + manual compaction, per-chat tools switch and model switch), over your data with tools (accounts, transactions, bills, recurring detection, savings, projections, budgets, calendar, notes, exact calculator). Any OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp, OpenRouter...) or Anthropic; add as many as you like. Cloud models are blocked until you allow each one. It can write notes and add a subscription when you ask in chat (with Undo); it cannot move money. Models without tool calling get a data snapshot instead.
* **Settings** (tabs): Connect & import, Robinhood, Calendar (CalDAV), Models, Categories, Sharing with the Orb. Plaid usage + cost guard (call counts, budget warning, daily cap).
* **Sharing**: the Pondering Orb sees only the totals you tick (never transactions). API tokens (named, scoped, revocable, rate limited, read-only) let Home Assistant or scripts read `GET /api/v1/me/{summary|ha|bills|alerts|savings|goals|budgets|transactions}`; the UI generates the Home Assistant YAML.
* **Models**: pick the server type from a list, press Refresh to list its models, no naming needed.
* **Drop folder**: CSVs in `data/<name>/imports/` (Cash App, PayPal, Venmo) are imported on sync.
* **UI** (`ui/index.html`, served by the gateway at `/`): sign in with goblin name + login token.

Does the agent need embeddings? No. Your data is structured (amounts, dates, accounts), so exact queries through tools are more accurate than similarity search. Embeddings only help with free text; add them later for notes if you want.

## Run
```bash
cp goblins.example.yaml goblins.yaml        # edit names / share / rh_agent / rh_live
python3 scripts/gen_compose.py               # prints login tokens and the Orb key ONCE
# fill .secrets/plaid.env, <name>.integrations.env (CalDAV), <name>.rh-agent.env
docker compose -f docker-compose.generated.yml up --build
```
Gateway listens on `127.0.0.1:8080`; put Tailscale / a Cloudflare Tunnel in front, never the open internet.
Back up `.secrets/<name>.env`: losing `FIELD_KEY` means re-linking banks. Plaid webhooks need a public HTTPS URL
(`PLAID_WEBHOOK_URL` -> `/webhooks/plaid`); without it, use Sync now.

## Test without a Plaid account
```bash
python3 -m pytest                     # unit tests
python3 scripts/synthetic_e2e.py      # real processes + a synthetic Plaid server (devtools/fake_plaid.py), 31 checks
python3 scripts/dev_stack.py          # gateway + goblin with random data + a fake model server, to click around (token: login-alice)
```
The synthetic server follows Plaid's documented response shapes. It proves our services work together; it cannot prove real Plaid agrees. Get free sandbox keys from Plaid before trusting it with real banks.

## Not done yet
* Plaid is untested against the real API (tests use mocks); try `PLAID_ENV=sandbox` first.
* The agent is tested with scripted fake models. Try your real local model and see which tools it handles well.
* rh-agent tool names/auth are unverified (Robinhood docs don't publish them); configure via env.
* CalDAV *reading* (event -> money amount) and investments holdings via Plaid.

`python3 -m pytest` runs the tests.
