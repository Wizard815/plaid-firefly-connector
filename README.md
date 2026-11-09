# Goblin-Bank

Self-hosted family finance. **One gateway, one private container per person.**

```
Pondering Orb ──(read-only key)──► CoinGateway ──► goblin-alice ─► alice-rh-csv / rh-agent / rh-live
                                        │──────► goblin-bob   ─► ...
```

* **CoinGateway** (`ROLE=gateway`): login + routing, Plaid webhook relay (item_id → goblin map only),
  and `/api/v1/summary` for Pondering Orb. Stores no tokens, balances or transactions.
* **Goblin** (`ROLE=goblin`): one person's data. Shares with the gateway/Orb only what `share:` allows.
  Shareable: `net_worth`, `subscriptions`, `bills` (totals), `bill_list` (each bill for the next 30 days), `alerts` (counts),
  `alert_list` (each alert), `savings`, `goals`, `robinhood_main`, `robinhood_agent`. The lists are separate opt-ins from the totals.
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
* **Phone notifications**: paste Mattermost incoming-webhook URLs (Settings, Alerts): a separate channel and severity for CDs and bonds (default: everything; empty means they use the main one), and a main channel for everything else (default: warnings and critical). Each new alert at or above your chosen severity is sent once, criticals repeat after 24 hours while still active, an alert that clears and returns is announced again, and a failed send is retried. Test button and "check now" included. Slack-style webhooks work too.
* **Alerts** (switchable in Settings; a CD or bond that matured and is still sitting there gets its own warning until you mark it handled): repeating payments not yet in Subscriptions, overdrawn / below-minimum accounts, accounts projected to run short, missed payments, CDs maturing, bills due soon.
* **Overview**: net worth, adjustable "bills in the next N days" window (1 day to 10 years, repeats expanded), spending by category, alerts.
* **Savings**: where the money sits (bank savings/CD accounts appear by themselves, plus anything you add by hand) and what it is for. Goals earmark part of the savings, so *free* money = saved minus earmarked. Free money is shared by every goal until you assign it. A new goal starts unfunded (tick "use free savings" to take what is free) and shows how long saving would take. "When your CDs mature" shows what each CD will be worth, what is pinned to goals, and where the free money would go, by priority. Move earmarks between goals, auto-fill by priority, mark bought.
* **Budgets & notes**: a monthly plan (expected income - bills - budgets - goal contributions = left to assign; a budget covers its category's bills, never double counted), income sources (steady deposits are detected; the plan follows the real paydays, so a three-paycheck month shows three), your notes, goals, and an agent that drafts notes from your real data. Link a goal or note to an agent chat.
* **Agent**: saved multi-chat (rename, delete, context meter, automatic + manual compaction, per-chat tools switch and model switch), over your data with tools (accounts, transactions, bills, recurring detection, savings, projections, budgets, calendar, notes, exact calculator). Any OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp, OpenRouter...) or Anthropic; add as many as you like. Cloud models are blocked until you allow each one. It can write notes and add a subscription when you ask in chat (with Undo); it cannot move money. Models without tool calling get a data snapshot instead.
* **Settings** (tabs): General (light/dark, accent colour, arrange mode), Alerts, Family links, Sharing with the Orb, Models, Categories, Calendar (CalDAV), Robinhood, Connect & import.
* **Your layout**: every tab has "Arrange panels": drag panels anywhere on a 12-column grid, resize them, hide the ones you do not use.
* **Family links**: link two goblins through the gateway (it only relays, stores nothing). Linking shares nothing by itself; each person then grants specific items (an account balance, a goal with a combined total and shared notes), revocable any time. Joint accounts count only your share. Plaid usage + cost guard: call counts, a monthly estimate at Plaid's pay-as-you-go list prices (Transactions $0.30, Recurring $0.15, Investments Holdings $0.18, per linked item per month), a budget warning, a daily call cap, and a switch to skip Plaid's paid recurring detection (the app finds repeating payments itself).
* **Sharing**: the Pondering Orb sees only the totals you tick (never transactions). API tokens (named, scoped, revocable, rate limited, read-only) let Home Assistant or scripts read `GET /api/v1/me/{summary|ha|bills|alerts|savings|goals|budgets|transactions}`; the UI generates the Home Assistant YAML.
* **Fresh data**: automatic sync every 15 minutes (Settings, Connect & import; 0 turns it off). A token with the `refresh` scope can add `?refresh=1` to ask for a fresh sync on demand (at most once per 5 minutes); Home Assistant polls every 15 minutes and can call a `rest_command` for an immediate refresh.
* **Projected spending**: Overview shows spending so far, a month-end projection built from your history (per category, with lumpy payments like rent handled separately) and both against your average monthly income. Shareable with the Orb as `spending`; the itemised `bill_list` and `alert_list` are separate opt-ins.
* **Models**: pick the server type from a list, press Refresh to list its models, no naming needed.
* **Drop folder**: CSVs in `data/<name>/imports/` (Cash App, PayPal, Venmo) are imported on sync.
* **UI** (`ui/index.html`, served by the gateway at `/`): sign in with goblin name + login token.

Does the agent need embeddings? No. Your data is structured (amounts, dates, accounts), so exact queries through tools are more accurate than similarity search. Embeddings only help with free text; add them later for notes if you want.

## Install guide

Everything runs in Docker. You do not need Python or anything else on the machine. A small home server, a NAS or a Raspberry Pi 4/5 (64-bit) is plenty.

### 1. What you need
* A machine that stays on, with **Docker Engine and the compose plugin** ([install Docker](https://docs.docker.com/engine/install/)). On Windows use WSL2.
* About 1 GB of free disk space and 500 MB of RAM per person.
* A free [Plaid](https://dashboard.plaid.com) account for the Client ID and Secret (to link real banks). Start in `sandbox` (fake banks).

### 2. Install
```bash
git clone https://github.com/Wizard815/Goblin-Bank.git
cd Goblin-Bank
./scripts/install.sh
docker compose -f docker-compose.generated.yml up -d --build
```
**There are no files to edit.** The script builds the image, makes a separate encryption key and secrets for each person, and prints each login token and the admin token **once**: copy them somewhere safe now, only their hashes are stored. It starts with one person called `me` (run `NAME=alice ./scripts/install.sh` for a different name). Check with `docker compose -f docker-compose.generated.yml ps`: everything should say "healthy".

### 3. Set everything up in the app
1. **Admin page** at `http://127.0.0.1:6767/admin` (the admin token): add the other people, enter your **Plaid keys** once for everyone (Client ID and secret from dashboard.plaid.com, Developers, Keys, and Sandbox or Production), set the **public address** for webhooks, and set the budget and limits. Each person's login token and the one command to start their container are shown there.
   Or skip the admin page: each person can enter their own keys under Settings, Connect & import, **Plaid keys** (stored encrypted in their own container).
2. Open `http://127.0.0.1:6767`, sign in with your name and token, and in Settings, Connect & import, press **Test the keys**, then **Link an account**.
3. CalDAV, Robinhood, models and notifications are all on the Settings page too. `.secrets/plaid.env` and `.secrets/<name>.integrations.env` still work if you prefer files, but nothing needs them.

### Connecting your real bank
1. In the app, Settings, Connect & import, **Plaid keys**: choose **Production**, enter your Client ID and Production secret (or let the admin do it for everyone), Save, and press **Test the keys**. Plaid may need to approve production access first; until then use Sandbox with Plaid's test login (`user_good` / `pass_good`).
2. Press **Link an account** and sign in to the bank in the window that opens.
3. Banks that send you to **their own website** to sign in (OAuth: Capital One and many other large banks) need an HTTPS address. Set the **Public address** on the admin page (it is also used for webhooks) and add `https://your-address/` under Developers, API, **Allowed redirect URIs** in the Plaid dashboard. After you sign in at the bank it sends you back and the app finishes the link by itself. Plaid may also ask you to complete an OAuth questionnaire in the dashboard before some banks appear in production.
4. Link one bank first and check the numbers, then add the rest. The cost panel in the same tab shows what Plaid will bill.

### Which Plaid site is which
* **dashboard.plaid.com** is the developer dashboard: your Client ID, secrets, allowed redirect URIs, API version and usage. This is what Goblin Bank is set up with.
* **my.plaid.com** is the consumer Plaid Portal, where a person sees and revokes the connections they made to apps through Plaid. It is not a connection point for developers: banks shown there belong to other apps' connections, and Goblin Bank cannot read them. To bring a bank into Goblin Bank, link it in Goblin Bank (Settings, Connect & import, Link an account), which creates a connection under your own Client ID.
* **API version:** every request pins `Plaid-Version: 2020-09-14` (override with `PLAID_VERSION`), so a change of the default version in the dashboard cannot change what the app receives.

### Webhooks (Plaid tells the app when there is news)
Without webhooks the app has to ask each bank on a schedule. With them, Plaid calls the gateway the moment new transactions, holdings or recurring payments exist, the app pulls only that bank, and banks on Automatic are only polled weekly as a safety net. Webhooks are free; the bill is the same, the data is fresher and there are fewer calls.
1. Make one address reachable by Plaid over HTTPS: a domain with Caddy, a Cloudflare Tunnel or Tailscale Funnel. To expose only the webhook and nothing else, use `examples/Caddyfile.webhooks-only`.
2. On the gateway's `/admin` page, Plaid limits, type it into **Public address** and Save. That is the only setting: every goblin then sends `https://your-address/webhooks/plaid` with each new link, tells banks that are already linked (one free call each, once), and uses `https://your-address/` as the OAuth return address unless you set `PLAID_REDIRECT_URI` yourself.
3. Each bank shows a **live updates** badge once its first webhook has arrived, and the admin table shows how many banks per person have gone live. Webhooks are checked with Plaid's signature; an unsigned call is refused.
4. **Test it in the Sandbox:** with a sandbox bank linked and the public address set, Settings, Connect & import has a **Send a test webhook** button. Plaid then sends a real signed webhook to your address and the app tells you whether it arrived and verified. Plaid wants an answer within 10 seconds and retries for a day, so the app answers at once and does the pull in the background; webhooks that fail verification get a 401, which Plaid does not retry.
5. If a bank's login breaks (Plaid says so by webhook), the app raises an alert to relink it; the other banks keep updating.
The gateway remembers which bank belongs to which goblin in memory only, so each goblin re-announces its banks every few minutes and after any restart.

### Keeping the Plaid bill small
Plaid bills per linked bank login per month, not per call: about $0.45 each at list price (Transactions $0.30 + Recurring $0.15), plus $0.18 if it has an investment account. The calls themselves are free, but the app still keeps them low:
* **Every bank has its own schedule** (Settings, Connect & import, under each linked bank): every 15 minutes, 3 hours, daily, weekly, every 2 weeks, monthly, or only when you press Sync. **Automatic** picks by what the bank holds: daily for a checking account or card, weekly for a savings-only bank, monthly for an investment-only one. Pressing **Sync now**, a Plaid webhook and a first link always pull straight away. A Plaid webhook (see "Plaid webhooks" above) tells the app the moment new data exists, so it never has to poll.
* **Recurring detection is per bank too** ($0.15 per bank per month, the one switch that saves money). Automatic turns it on only for banks with a checking account or a card, and asks at most weekly. Balances now come with the transactions call, so a pull that has news is one call. Typical five-bank setup: about **7 calls a day** on schedules alone, about **4.5** with webhooks (the old 3-hour polling was 86).
* **A hard monthly budget, default $5 per person.** The app refuses to link another bank that would push the estimated bill over it (it even refuses to open the sign-in window), and it will not make any per-use call (balance, refresh) that would break it. Default daily call cap: 300.
* **The gateway admin sets the limits.** On the gateway's `/admin` page, "Plaid limits" sets the budget, the daily call cap, how often a bank is asked and whether to use Plaid's paid recurring detection, for everyone, with optional per-person overrides. Each goblin picks them up within minutes and shows them read-only; a person cannot raise them from inside the app. The table beside it shows, per person, banks, estimated cost and calls today (counts only). Plaid bills you the combined total of everyone.
* There is **one gateway for the whole server** and one goblin container per person; the goblins all report to the same gateway.
* **Turn off Plaid's recurring detection** (Settings, Connect & import): saves $0.15 per item per month. The app finds repeating payments itself.
* **Link only the accounts that change a lot.** A savings account or IRA that you look at once a month does not need a live feed: add it by hand, or drop its CSV in `data/<name>/imports/` (on a hand-added account, open it and use **Import a CSV from your bank**: it finds the date, description and amount, or Debit/Credit, columns by name, picks up a running Balance column, and never double counts an overlapping download; Cash App, PayPal and Venmo files have their own importer).
* **Every person who links the same bank is billed again**, including a joint account linked by two people.
* Banks generally do not email scheduled CSV reports: Huntington, Discover, Capital One and Axos offer manual downloads (CSV or QFX) and PDF statements, plus balance and transaction alerts. There is no setting that mails a recurring CSV that I could confirm, so a monthly manual download for the quiet accounts is the realistic no-Plaid route.

### 4. Reach it from your phone and other devices
The gateway listens only on `127.0.0.1:6767` on purpose (change it with `port:` in `goblins.yaml`). Pick one:
* **Tailscale (simplest, recommended):** install it on the server and your devices, then `tailscale serve --bg 6767` gives you an `https://your-machine.your-tailnet.ts.net` address that only your devices can open.
* **Your own domain with automatic HTTPS:** edit `examples/Caddyfile` (your domain), point the domain at the server, open ports 80 and 443, then
  `docker compose -f docker-compose.generated.yml -f examples/docker-compose.caddy.yml up -d`.
* **Cloudflare Tunnel** also works. Whatever you use, it must be HTTPS: your login token travels with every request.
Never expose port 6767 to the internet as plain HTTP.

### 5. Add people, link them, share
* **Add a person:** open `/admin` (use the admin token), type a name under People, press Add. It prints their login token once and the small compose file for their container, plus the one command to start it (a web page cannot start Docker). Their encryption key is made in your browser, so the gateway never sees it.
* **Link two people:** each signs in, Settings, **Family links**, picks the other person from the list and asks. The other person gets a pop-up they must answer. A link only sets up the connection; each person then ticks exactly what the other can see (a savings account's balance, a goal), and can take it back any time.
* **Plaid webhooks (optional):** set the Public address on the admin page (see "Webhooks" above). Without it, banks are still pulled on their schedules.

### 6. Back up, update, stop
```bash
./scripts/backup.sh        # a consistent copy of every database plus the secrets, in backups/ (owner-only). Keep it private and off this machine.
./scripts/update.sh        # backup, git pull, rebuild, restart. Your data is untouched.
docker compose -f docker-compose.generated.yml down        # stop (data stays in data/ and .secrets/)
```
**Back up `.secrets/` and `data/`.** Losing a person's `FIELD_KEY` (in `.secrets/<name>.env`) means re-linking their banks.

### 7. Where things live
| Path | What | Back up? |
| --- | --- | --- |
| `goblins.yaml` | who is on this server | yes |
| `.secrets/` | encryption keys, secrets, Plaid keys | **yes, private** |
| `data/<name>/db/` | one person's database | yes |
| `data/<name>/imports/` | drop CSVs here (Cash App, PayPal, Venmo) | optional |
| `gateway.yaml`, `gateway-state/` | the gateway's list of people (hashes only) | yes |

### 8. Troubleshooting
* `docker compose ... ps` shows a service restarting: `docker compose -f docker-compose.generated.yml logs <service>`.
* **Gateway "Permission denied: /config/gateway.yaml"** or "cannot write state": the containers run as user 10001. `sudo chown 10001:10001 gateway.yaml && sudo chown -R 10001:10001 gateway-state data`.
* **Lost a login token:** `./scripts/install.sh --reset-token bob` prints a new one (nothing else changes), then `docker compose -f docker-compose.generated.yml restart gateway`.
* **"Plaid is not configured":** enter the keys in Settings, Connect & import, Plaid keys (or on the admin page), then press Test the keys.
* **Phone cannot reach it:** see step 4; the gateway is not on the open network by default.

### 9. Safety checklist before real bank data
* HTTPS in front of it, or Tailscale only (step 4).
* `.secrets/` readable only by you, and backed up off the machine.
* Use local models only for the agent unless you have deliberately allowed a cloud one (Settings, Models).
* Keep the Plaid daily call cap on (Settings, Connect & import).

## Run without the install script
```bash
cp goblins.example.yaml goblins.yaml && python3 scripts/gen_compose.py     # needs python3 with pyyaml + cryptography
docker compose -f docker-compose.generated.yml up -d --build
```

## Test without a Plaid account
```bash
python3 -m pytest                     # unit tests
python3 scripts/synthetic_e2e.py      # real processes + a synthetic Plaid server (devtools/fake_plaid.py), 31 checks
python3 scripts/dev_stack.py          # gateway + goblin with random data + a fake model server, to click around (token: login-alice)
```
The synthetic server follows Plaid's documented response shapes. It proves our services work together; it cannot prove real Plaid agrees. Get free sandbox keys from Plaid before trusting it with real banks.

## Not done yet
* Plaid is tested against the real Sandbox, never production. Banks that sign you in on their own website (OAuth) need `PLAID_REDIRECT_URI` set and registered with Plaid; the return step is built but has not been tried against a real bank.
* The agent is tested with scripted fake models. Try your real local model and see which tools it handles well.
* rh-agent tool names/auth are unverified (Robinhood docs don't publish them); configure via env.
* CalDAV *reading* (event -> money amount) and investments holdings via Plaid.

`python3 -m pytest` runs the tests.
