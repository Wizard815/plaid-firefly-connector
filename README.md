# plaid-firefly-connector

plaid-firefly-connector holds your Plaid bank connections and forwards the accounts and
transactions you approve into Firefly III. Firefly III is where you look at
your money; plaid-firefly-connector only has an **admin page** (`/admin`). The old
per-person app lives on the `full-ui` branch.

**Coming from Goblin Bank?** See [Moving from Goblin Bank](#moving-from-goblin-bank): one command moves everything
(people, passwords, tokens, Plaid links, Firefly III links, data) into a fresh folder, and you can delete the old one afterwards.

## How it works

1. **Admin** adds a person, enters the Plaid keys, sets API cost limits.
2. A bank is linked with Plaid (by you in Admin, or by the person with a
   temporary link code at `/link`).
3. Every new bank account starts as **Needs approval**: nothing is sent.
   In Admin, per account, pick **Create in Firefly III**, **Link to** an
   existing Firefly account, or **Do not send**. One Firefly account can
   receive only one bank account.
4. plaid-firefly-connector pushes transactions to Firefly III after each sync, safely
   repeatable (no duplicates, edits and deletions follow the bank).

## Joint accounts

People share a bank account between themselves; the admin is not needed. The person who linked the account **asks**, the other
person **accepts** or declines. The gateway only relays the messages between the two workers and keeps nothing.

1. **Ask.** The owner picks the account and the person (`POST /api/joint/<account>/ask` with `{"peer": "<name>"}`, or Coinpile:
   Settings, Banks, Joint accounts). Only the account's name, bank, kind and last digits are sent. Nothing else moves yet.
2. **Answer.** The other person sees it under Incoming (`GET /api/joint`) and answers
   (`POST /api/joint/from/<owner>/<account>/accept|decline`). Accepting means choosing how it goes into *their own* Firefly III:
   * `{"mode": "create"}`: a new asset account, "Shared by <owner>".
   * `{"mode": "link", "ff_id": <id>}`: an asset account they already have. It must exist in their Firefly III and must not already
     receive another account (their own or another share).
   * `{"mode": "skip"}`: accepted but not put into Firefly III.
3. **Shared.** From then on the account's balance and transactions go to them after each sync, safely repeatable (no duplicates, edits
   and deletions follow the bank). Only the owner's copy talks to the bank; the other side never sees anything else of the owner's.
4. **Stop.** Either side can stop at any time: the owner with Stop (`DELETE /api/joint/<account>`), the other with Leave
   (`POST /api/joint/from/<owner>/<account>/leave`). Nothing more is sent. The account stays in the receiver's Firefly III with the
   history it already had (Firefly III is never emptied by the connector); they can delete it there.

What happens at the edges:

* **Asking again** after a stop, decline, leave or cancel is a new question. The other side is always told about the stop first, even
  when the new question follows straight away, and a new acceptance applies the new choice.
* **Changing your mind** before your answer is delivered: only the latest answer is sent.
* **The account goes away** (unlinked from Plaid, or closed at the bank): the share ends and the other side is told.
* **A person is removed** from the server: shares with them end on everyone's side. A removed person's name is never given to someone
  new, so nobody can inherit their shares.
* **Too many questions:** one person can have at most 20 questions waiting in someone's inbox.
* **Two people on the same Firefly III:** don't share into it; it would show the account twice. Link it once there.

The admin can still set a share directly (Admin, Manage that person, **Joint with**). The admin never sees the account's data, only
counts. A share the admin set waits for the admin's approval on the receiver's side (create, link or skip), like a bank the receiver
linked themselves, and the receiver can Leave it like any other. An account cannot be shared with its own owner.

To count only your part of a joint account and settle up who put in what, tie the two copies together in Coinpile (Settings, Sharing,
Joint accounts); see Coinpile's README.

Goals and subscriptions are not part of this app any more (they live on the
`full-ui` branch). Use Firefly III's own piggy banks and bills.

## Firefly III on Unraid

In the Unraid template (`fireflyiii/core:latest`): `APP_KEY` is any 32
characters (make one with `head /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 32`);
`DB_CONNECTION` is `mysql` or `pgsql` and needs a MariaDB or PostgreSQL container
you install first (create an empty database and a user for it); `DB_HOST` is that
container's address, `DB_PORT` 3306 (MariaDB) or 5432 (PostgreSQL), plus the
database name, user and password; `TRUSTED_PROXIES` stays `**`; set a host port
for the WebUI. Open `http://<unraid address>:<port>`, register the first user,
create a Personal Access Token, and enter `http://<unraid address>:<port>` and
the token in Admin for that person.

## Quick how-to

- **New person:** Admin, *Add person*. Press *Manage*.
- **Link Huntington or another bank:** Manage, *Link a bank*, search the bank
  in Plaid's window and sign in there. plaid-firefly-connector never sees the login. Which
  banks Plaid supports (PayPal included) depends on your Plaid plan; check in
  the Link window.
- **Let the person link their own:** Manage, choose a time, *Make a temporary
  link code*. Give them the code and `https://<your address>/link`. It works
  only for that person, only for linking, and expires or can be revoked.
- **Connect Firefly III:** in Firefly III go to Options, Profile, OAuth,
  Personal Access Tokens, create one. In Admin, under *Firefly III for each
  person*, enter its address and token, Save. Then Manage: *Test the
  connection*, approve accounts, *Send now*.

The rest of this file is the original install guide (some sections talk about
the per-person app, which is now on the `full-ui` branch, along with goals, sharing between people and the AI chat; those are not in `main`).


Self-hosted family finance. **One gateway, one private container per person.**

```
Pondering Orb ──(read-only key)──► CoinGateway ──► worker-alice ─► alice-rh-agent / rh-live
                                        │──────► worker-bob   ─► ...
```

* **CoinGateway** (`ROLE=gateway`): login + routing, Plaid webhook relay (item_id → worker map only),
  and `/api/v1/summary` for Pondering Orb. Stores no tokens, balances or transactions.
* **Worker** (`ROLE=worker`): one person's data. Shares with the gateway/Orb only what that person ticks under Settings, Sharing (or `share:` in `people.yaml` as a starting value).
  Shareable: `net_worth`, `subscriptions`, `bills` (totals), `bill_list` (each bill for the next 30 days), `alerts` (counts),
  `alert_list` (each alert), `savings`, `robinhood_main`, `robinhood_agent`. The lists are separate opt-ins from the totals.
  Parses Robinhood activity CSVs dropped in `data/<name>/robinhood-csv/` itself, in-process (no account access, deduped) — not a separate container, since there's no credential to isolate.
* **Connectors** (one container each, read-only, GET-only, secret-protected):
  * `rh-agent` – official Robinhood MCP (agent account). Fails closed: only tools in `RH_MCP_READ_TOOLS` are callable.
  * `rh-live` – **unofficial** `robin_stocks`. Off by default, opt-in, own container + session volume, min 3h between upstream calls. Violates Robinhood ToS.
* Main portfolio view = live if enabled and answering, otherwise CSV (with `as_of`).

One image, many roles (`ROLE=`), so the repo stays small.

## What each worker does
* **Plaid** (Huntington, Discover, Axos, Capital One, IRA): link in Settings, tokens encrypted at rest, transactions sync, holdings, recurring-bill detection, webhook signature checks, relink mode.
* **Money movement**: search + filter (account, category, direction, amount range, dates, sort), totals for whatever you filtered, pagination, CSV export, per-payment categories with "remember for similar" rules and keyword auto-categorizing.
* **Accounts**: drill into any account (history, 30-day in/out, its bills, its transactions), add accounts by hand (cash, anything Plaid can't reach), minimum-balance warnings.
* **What each account is for**: under Settings, Connect & import, every linked bank lists its accounts. Give the bank a category and each account its own (Checking, Bills, Spending, Savings, Emergency fund, Credit card, Retirement, or a name of your own), plus a nickname. If Huntington has three checkings and a savings, say which is which. What you call an account beats what the bank calls it: a "checking" labelled Emergency fund counts as savings, and a bank whose accounts are all savings is only asked weekly.
* **Find missed subscriptions**: on the Subscriptions tab, **Scan now** looks through all your transactions, including CSVs you imported, for repeating payments that are not subscriptions yet. Pick one account or all, Thorough (finds ones seen only twice or on a looser schedule, with a confidence) or Normal, and optionally include ones you said no to. Add or dismiss each result. An account's page also has **Check for subscriptions**.
* **Subscriptions**: every interval (daily to every 2 years, or custom "every N days/weeks/months/years"), paid-by-me or someone else (with their name), bound to any account (Plaid or manual) or a free-text payment method, end dates, reminders. Detects steady payments in your history and offers them with your name/interval/account. Flags bound bills that stop showing up (counts consecutive misses).
* **Phone notifications**: paste Mattermost incoming-webhook URLs (Settings, Alerts): a separate channel and severity for CDs and bonds (default: everything; empty means they use the main one), and a main channel for everything else (default: warnings and critical). Each new alert at or above your chosen severity is sent once, criticals repeat after 24 hours while still active, an alert that clears and returns is announced again, and a failed send is retried. Test button and "check now" included. Slack-style webhooks work too.
* **Alerts** (switchable in Settings; a CD or bond that matured and is still sitting there gets its own warning until you mark it handled): repeating payments not yet in Subscriptions, overdrawn / below-minimum accounts, accounts projected to run short, missed payments, CDs maturing, bills due soon.
* **Overview**: net worth, adjustable "bills in the next N days" window (1 day to 10 years, repeats expanded), spending by category, alerts.
* **Agent**: saved multi-chat (rename, delete, context meter, automatic + manual compaction, per-chat tools switch and model switch), over your data with tools (accounts, transactions, bills, recurring detection, savings, projections, budgets, calendar, notes, exact calculator). Any OpenAI-compatible endpoint (Ollama, LM Studio, llama.cpp, OpenRouter...) or Anthropic; add as many as you like. Cloud models are blocked until you allow each one. It can write notes and add a subscription when you ask in chat (with Undo); it cannot move money. Models without tool calling get a data snapshot instead.
* **Settings** (tabs): General (light/dark, accent colour, arrange mode), Alerts, Family links, Sharing with the Orb, Models, Categories, Calendar (CalDAV), Robinhood, Connect & import.
* **Your layout**: every tab has "Arrange panels": drag panels anywhere on a 12-column grid, resize them, hide the ones you do not use.
* **Family links**: link two workers through the gateway (it only relays, stores nothing). Linking shares nothing by itself; each person then grants specific items (an account balance, a goal with a combined total and shared notes), revocable any time. Joint accounts count only your share. Plaid usage + cost guard: call counts, a monthly estimate at Plaid's pay-as-you-go list prices (Transactions $0.30, Recurring $0.15, Investments Holdings $0.18, per linked item per month), a budget warning, a daily call cap, and a switch to skip Plaid's paid recurring detection (the app finds repeating payments itself).
* **Sharing**: the Pondering Orb sees only the totals you tick (never transactions). API tokens (named, scoped, revocable, rate limited, read-only) let Home Assistant or scripts read `GET /api/v1/me/{summary|ha|bills|alerts|savings|transactions}`; the UI generates the Home Assistant YAML.
* **Fresh data**: automatic sync every 15 minutes (Settings, Connect & import; 0 turns it off). A token with the `refresh` scope can add `?refresh=1` to ask for a fresh sync on demand (at most once per 5 minutes); Home Assistant polls every 15 minutes and can call a `rest_command` for an immediate refresh.
* **Projected spending**: Overview shows spending so far, a month-end projection built from your history (per category, with lumpy payments like rent handled separately) and both against your average monthly income. Shareable with the Orb as `spending`; the itemised `bill_list` and `alert_list` are separate opt-ins.
* **Models**: pick the server type from a list, press Refresh to list its models, no naming needed.
* **Drop folder**: CSVs in `data/<name>/imports/` (Cash App, PayPal, Venmo) are imported on sync.
* **UI** (`ui/index.html`, served by the gateway at `/`): sign in with your name and password (see the install guide).

Does the agent need embeddings? No. Your data is structured (amounts, dates, accounts), so exact queries through tools are more accurate than similarity search. Embeddings only help with free text; add them later for notes if you want.

## Install guide

Everything runs in Docker. You do not need Python or anything else on the machine. A small home server, a NAS or a Raspberry Pi 4/5 (64-bit) is plenty.

### 1. What you need
* A machine that stays on, with **Docker Engine and the compose plugin** ([install Docker](https://docs.docker.com/engine/install/)). On Windows use WSL2.
* About 1 GB of free disk space and 500 MB of RAM per person.
* A free [Plaid](https://dashboard.plaid.com) account for the Client ID and Secret (to link real banks). Start in `sandbox` (fake banks).

### 2. Install
```bash
git clone https://github.com/Wizard815/plaid-firefly-connector.git
cd plaid-firefly-connector
./scripts/install.sh
docker compose -f docker-compose.generated.yml up -d --build
```
**There are no files to edit.** The script asks for your name and a **password you choose** (12 or more characters; a short phrase of a few words works well), builds the image, makes a separate encryption key and secrets for each person, creates the Docker network it needs (`plaid-proxy`, or set `NETWORK=`), and prints the **admin password**. Check with `docker compose -f docker-compose.generated.yml ps`: everything should say "healthy".

How sign-in works:
* **People choose their own password.** It is kept only as a salted scrypt hash (never as plain text, and the script never writes it to a file). Press Enter at the prompt instead and a random login token is generated and printed **once**; that token is the password then. Change a password later under Settings, General, or `./scripts/install.sh --set-password NAME`. You can also put `password:` on a person in `people.yaml` (12+ characters, validated), see `people.example.yaml`; the plaintext stays only in that file.
* **The admin password is random hex**, saved in `.secrets/admin.password` (owner-only) and printed every time you run `./scripts/install.sh`; the gateway only keeps its hash. Lose it? `cat .secrets/admin.password`.
* **Guessing is slowed down**: after 5 wrong tries for a name (or for the admin) further tries are refused for a while, doubling up to 15 minutes. A weak password is still weak, so make it long.
* Unattended installs: `NONINTERACTIVE=1 NAME=alice PROXY_PASSWORD_ALICE='a long phrase here' ./scripts/install.sh`.

### 3. Set everything up in the app
1. **Admin page** at `http://127.0.0.1:6421/admin` (the admin password): add the other people, enter your **Plaid keys** once for everyone (Client ID and secret from dashboard.plaid.com, Developers, Keys, and Sandbox or Production), set the **public address** for webhooks, and set the budget and limits. Give each new person a password (or let it make a random token, shown once) and the one command to start their container is shown there.
   Or skip the admin page: each person can enter their own keys under Settings, Connect & import, **Plaid keys** (stored encrypted in their own container).
2. Open `http://127.0.0.1:6421`, sign in with your name and password, and in Settings, Connect & import, press **Test the keys**, then **Link an account**.
3. The Pondering Orb key, CalDAV, Robinhood, models and notifications are all set up in the app too (admin page for the Orb key, Settings for the rest). `.secrets/plaid.env` and `.secrets/<name>.integrations.env` still work if you prefer files, but nothing needs them.

### Connecting your real bank
1. In the app, Settings, Connect & import, **Plaid keys**: choose **Production**, enter your Client ID and Production secret (or let the admin do it for everyone), Save, and press **Test the keys**. Plaid may need to approve production access first; until then use Sandbox with Plaid's test login (`user_good` / `pass_good`).
2. Press **Link an account** and sign in to the bank in the window that opens.
3. Banks that send you to **their own website** to sign in (OAuth: Capital One and many other large banks) need an HTTPS address. Set the **Public address** on the admin page (it is also used for webhooks) and add `https://your-address/` under Developers, API, **Allowed redirect URIs** in the Plaid dashboard. After you sign in at the bank it sends you back and the app finishes the link by itself. Plaid may also ask you to complete an OAuth questionnaire in the dashboard before some banks appear in production.
4. Link one bank first and check the numbers, then add the rest. The cost panel in the same tab shows what Plaid will bill.

### Pondering Orb
1. On the gateway's admin page, under **Pondering Orb**, press **Make a key**. It is shown once; only its hash is kept. (`orb_key: true` in `people.yaml` still works if you prefer a file.)
2. Each person chooses what the Orb may see under Settings, **Sharing** (totals like net worth, bills, savings; the itemised lists are separate opt-ins; never transactions).
3. In Pondering Orb, set `GOBLIN_ORB_KEY` to that key and point its plaid-firefly-connector widget at the gateway: `http://gateway:8000` if the Orb runs in the same Docker network, otherwise this server's address (the one from step 4 below, or `http://<server>:6421` on your network). The Orb calls `GET /api/v1/summary` with `Authorization: Bearer <key>`. Revoke the key on the admin page any time.
Nothing about this changed with the new setup: the key, the endpoint and the shape of the summary are the same.

### Calendar (CalDAV)
Settings, **Calendar**: enter your CalDAV calendar address, user and password (stored encrypted in that person's container; `.secrets/<name>.integrations.env` is only an optional fallback) and press **Test connection**. Then, on the Subscriptions tab, **Push to CalDAV now** puts every bill into the calendar and **Download .ics** gives the same bills for any calendar app. Reading calendar events as money amounts is not built yet.

### One address for everyone
There is **one gateway and one address** (`http://127.0.0.1:6421`, or your Tailscale / domain address). Everybody uses that same address, and so does the admin:
* `/` is the sign-in page. Each person types their own name and password; the gateway then sends everything they do only to their own worker container (`/g/<name>/...` behind the scenes). Alice and Bob never get different ports or links, and they never reach each other's containers.
* `/admin` on the same address is the admin page, with its own password (the random hex one in `.secrets/admin.password`).
* The worker containers have no ports open to you at all. Only the gateway is published (port 6421, this machine only); the workers sit behind it on the Docker network and trust only the gateway's secret.

### Which Plaid site is which
* **dashboard.plaid.com** is the developer dashboard: your Client ID, secrets, allowed redirect URIs, API version and usage. This is what Plaid Firefly Connector is set up with.
* **my.plaid.com** is the consumer Plaid Portal, where a person sees and revokes the connections they made to apps through Plaid. It is not a connection point for developers: banks shown there belong to other apps' connections, and Plaid Firefly Connector cannot read them. To bring a bank into Plaid Firefly Connector, link it in Plaid Firefly Connector (Settings, Connect & import, Link an account), which creates a connection under your own Client ID.
* **API version:** every request pins `Plaid-Version: 2020-09-14` (override with `PLAID_VERSION`), so a change of the default version in the dashboard cannot change what the app receives.

### Webhooks (Plaid tells the app when there is news)
Without webhooks the app has to ask each bank on a schedule. With them, Plaid calls the gateway the moment new transactions, holdings or recurring payments exist, the app pulls only that bank, and banks on Automatic are only polled weekly as a safety net. Webhooks are free; the bill is the same, the data is fresher and there are fewer calls.
1. Make one address reachable by Plaid over HTTPS: a domain with Caddy, a Cloudflare Tunnel or Tailscale Funnel. To expose only the webhook and nothing else, use `examples/Caddyfile.webhooks-only`.
2. On the gateway's `/admin` page, Plaid limits, type it into **Public address** and Save. That is the only setting: every worker then sends `https://your-address/webhooks/plaid` with each new link, tells banks that are already linked (one free call each, once), and uses `https://your-address/` as the OAuth return address unless you set `PLAID_REDIRECT_URI` yourself.
3. Each bank shows a **live updates** badge once its first webhook has arrived, and the admin table shows how many banks per person have gone live. Webhooks are checked with Plaid's signature; an unsigned call is refused.
4. **Test it in the Sandbox:** with a sandbox bank linked and the public address set, Settings, Connect & import has a **Send a test webhook** button. Plaid then sends a real signed webhook to your address and the app tells you whether it arrived and verified. Plaid wants an answer within 10 seconds and retries for a day, so the app answers at once and does the pull in the background; webhooks that fail verification get a 401, which Plaid does not retry.
5. If a bank's login breaks (Plaid says so by webhook), the app raises an alert to relink it; the other banks keep updating.
The gateway remembers which bank belongs to which worker in memory only, so each worker re-announces its banks every few minutes and after any restart.

### Plaid free-plan link limit
Plaid's free Trial plan limits how many real (Production) banks you can ever link, and per [Plaid's billing docs](https://plaid.com/docs/account/billing/) **removing one does not give the slot back**: every Production Item ever created counts. The docs say 10 for the Trial plan; check what your dashboard shows for your own account. So: use **Relink** (not Unlink then Link) when a bank's login needs repairing, because Relink reuses the same connection; practise in the Sandbox, which has no such limit; and link a bank once you are sure you want it.

### Keeping the Plaid bill small
Plaid bills per linked bank login per month, not per call: about $0.45 each at list price (Transactions $0.30 + Recurring $0.15), plus $0.18 if it has an investment account. The calls themselves are free, but the app still keeps them low:
* **Every bank has its own schedule** (Settings, Connect & import, under each linked bank): every 15 minutes, 3 hours, daily, weekly, every 2 weeks, monthly, or only when you press Sync. **Automatic** picks by what the bank holds: daily for a checking account or card, weekly for a savings-only bank, monthly for an investment-only one. Pressing **Sync now**, a Plaid webhook and a first link always pull straight away. A Plaid webhook (see "Plaid webhooks" above) tells the app the moment new data exists, so it never has to poll.
* **Recurring detection is per bank too** ($0.15 per bank per month, the one switch that saves money). Automatic turns it on only for banks with a checking account or a card, and asks at most weekly. Balances now come with the transactions call, so a pull that has news is one call. Typical five-bank setup: about **7 calls a day** on schedules alone, about **4.5** with webhooks (the old 3-hour polling was 86).
* **A hard monthly budget, default $5 per person.** The app refuses to link another bank that would push the estimated bill over it (it even refuses to open the sign-in window), and it will not make any per-use call (balance, refresh) that would break it. Default daily call cap: 300.
* **The gateway admin sets the limits.** On the gateway's `/admin` page, "Plaid limits" sets the budget, the daily call cap, how often a bank is asked and whether to use Plaid's paid recurring detection, for everyone, with optional per-person overrides. Each worker picks them up within minutes and shows them read-only; a person cannot raise them from inside the app. The table beside it shows, per person, banks, estimated cost and calls today (counts only). Plaid bills you the combined total of everyone.
* There is **one gateway for the whole server** and one worker container per person; the workers all report to the same gateway.
* **Turn off Plaid's recurring detection** (Settings, Connect & import): saves $0.15 per item per month. The app finds repeating payments itself.
* **Link only the accounts that change a lot.** A savings account or IRA that you look at once a month does not need a live feed: add it by hand, or drop its CSV in `data/<name>/imports/` (on a hand-added account, open it and use **Import a CSV from your bank**: it finds the date, description and amount, or Debit/Credit, columns by name, picks up a running Balance column, and never double counts an overlapping download; Cash App, PayPal and Venmo files have their own importer).
* **Every person who links the same bank is billed again**, including a joint account linked by two people.
* Banks generally do not email scheduled CSV reports: Huntington, Discover, Capital One and Axos offer manual downloads (CSV or QFX) and PDF statements, plus balance and transaction alerts. There is no setting that mails a recurring CSV that I could confirm, so a monthly manual download for the quiet accounts is the realistic no-Plaid route.

### 4. Reach it from your phone and other devices
The gateway listens only on `127.0.0.1:6421` on purpose, so from another device `http://<server-ip>:6421` is refused until you allow it. Change the port with `port:` and who can connect with `listen:` in `people.yaml` (`listen: 0.0.0.0`, or your LAN address, opens it to your home network over plain http; fine on a trusted network, since your password then crosses it unencrypted). After a change, run `./scripts/install.sh` and `docker compose -f docker-compose.generated.yml up -d`. Pick one:
* **Tailscale (simplest, recommended):** install it on the server and your devices, then `tailscale serve --bg 6421` gives you an `https://your-machine.your-tailnet.ts.net` address that only your devices can open.
* **Your own domain with automatic HTTPS:** edit `examples/Caddyfile` (your domain), point the domain at the server, open ports 80 and 443, then
  `docker compose -f docker-compose.generated.yml -f examples/docker-compose.caddy.yml up -d`.
* **Cloudflare Tunnel** also works. Whatever you use, it must be HTTPS: your password travels at sign-in and a session token with every request.
Never expose port 6421 to the internet as plain HTTP.

### 5. Add people, link them, share
* **Add a person:** open `/admin` (use the admin password), type a name and, if you like, a password for them under People, press Add. Without a password it prints a random login token once. It also shows the small compose file for their container, plus the one command to start it (a web page cannot start Docker). Their encryption key is made in your browser, so the gateway never sees it.
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
| `people.yaml` | who is on this server | yes |
| `.secrets/` | encryption keys, secrets, the admin password (`admin.password`), password hashes. `<name>.env` is the generator's own record; `<name>.worker.env` and `<name>.connector.env` are what the containers load (no password hashes in them) | **yes, private** |
| `data/<name>/db/` | one person's database (`proxy.db`) | yes |
| `data/<name>/imports/` | drop CSVs here (Cash App, PayPal, Venmo) | optional |
| `gateway.yaml`, `gateway-state/` | the gateway's list of people (hashes only) | yes |

**Keep live data outside this folder:** add `data_dir: /mnt/user/appdata/plaid-proxy-data` to `people.yaml` (or set
`PLAID_PROXY_DATA_DIR`), then run `./scripts/install.sh`. `.secrets/`, `data/`, `gateway-state/` and `gateway.yaml` then live there, the
generated compose file points at them, and regenerating never moves them back. No spaces in that path.

**Values with a `$` in the files you fill in by hand** (`.secrets/plaid.env`, `<name>.integrations.env`, `<name>.rh-live.env`): Docker Compose
reads `$word` as a variable and cuts the value short. Write each `$` as `$$`, or put the whole value in single quotes (`RH_PASSWORD='pa$s'`).
`install.sh` warns when it sees a lone `$` there. Files the generator writes are escaped for you.

### 8. Troubleshooting
* **No accounts after linking Plaid?** Run `./scripts/doctor.sh`. It checks, without printing any secret, that the Client ID and the secret are both set, which environment (Sandbox or Production) the app is using, whether Plaid accepts the keys, and which banks are linked, and it reminds you of the usual causes (linking in my.plaid.com instead of in the app, production not approved yet).
* `docker compose ... ps` shows a service restarting: `docker compose -f docker-compose.generated.yml logs <service>`.
* **Gateway "Permission denied: /config/gateway.yaml"** or "cannot write state": the containers run as user 10001. `sudo chown 10001:10001 gateway.yaml && sudo chown -R 10001:10001 gateway-state data`.
* **Forgot a password:** `./scripts/install.sh --set-password bob` asks for a new one (nothing else changes); `./scripts/install.sh --reset-token bob` instead makes a new random token. Then `docker compose -f docker-compose.generated.yml restart gateway`.
* **"Plaid is not configured":** enter the keys in Settings, Connect & import, Plaid keys (or on the admin page), then press Test the keys.
* **"Can't establish a connection" from another device:** by default the app only listens on the server itself. Add `listen: 0.0.0.0` (or the server's LAN address) to `people.yaml`, run `./scripts/install.sh`, then `docker compose -f docker-compose.generated.yml up -d`. Also check the server's firewall allows the port. **Logs:** `docker compose -f docker-compose.generated.yml logs -f gateway worker-<name>`. They say whether things are running and connected, never anything personal: started (and whether Plaid is configured), a worker going down or coming back, a heartbeat every 10 minutes with counts (requests, errors, failed sign-ins, workers up), sync rounds as counts with Plaid error codes, webhooks received or refused, a sign-in lockout. Requests, names of banks, amounts, tokens, passwords, search terms and addresses are never logged. `docker compose ... ps` shows each container as "healthy" when its health check passes.

### 9. Safety checklist before real bank data
* HTTPS in front of it, or Tailscale only (step 4).
* `.secrets/` readable only by you, and backed up off the machine.
* Use local models only for the agent unless you have deliberately allowed a cloud one (Settings, Models).
* Keep the Plaid daily call cap on (Settings, Connect & import).

## Moving from Goblin Bank

plaid-firefly-connector is what Goblin Bank became: names changed (`goblins.yaml` is now `people.yaml`, containers are `worker-<name>`, and so on;
the full list is below), so an old folder cannot simply be pulled forward. Instead, clone this repository into a **new folder** and let one script
move everything across. Your old folder is only read (it is mounted read-only), so you can always go back until you delete it.

```bash
cd /mnt/user/appdata                      # wherever your old goblin-bank folder is
git clone https://github.com/Wizard815/plaid-firefly-connector.git plaid-proxy
cd plaid-proxy
./scripts/migrate_from_goblin_bank.sh /mnt/user/appdata/goblin-bank
```

What it does, in order:
1. Builds the image, then **checks** the old install without changing anything: for each person it finds the database, the login, and the
   encryption key the container really ran with, and proves that key opens every saved Plaid link. Data folders you moved by hand are found
   from the old compose file. People added on the admin page (with their own `docker-compose.<name>.yml`) are found too.
2. Stops the old containers (their files are not touched).
3. Copies: secrets and keys, `goblins.yaml` as `people.yaml` (your comments kept, the same address and port), each database (renamed to
   `proxy.db`, nothing in it changed), Robinhood files and imports, and the gateway's saved state (passwords changed in the app, Plaid keys,
   Firefly III tokens, limits, Orb keys).
4. Writes the new compose file, then **verifies** that every login, internal secret, encryption key, the admin password and the Orb key are
   exactly what they were.
5. Starts plaid-proxy and waits until every person's container answers.

If any step after the old containers were stopped fails, it removes the half-made copy and starts the old containers again by itself.

Afterwards everything signs in as before: the same names, passwords and login tokens, the same admin password, the same Home Assistant tokens
(`gb_...`) and Pondering Orb key, and every bank stays linked. When you are happy:

```bash
./scripts/migrate_from_goblin_bank.sh --remove-old
```
That makes a private backup of the old folder next to the new one (`goblin-bank-final-backup-<date>.tar.gz`), removes the old images, and
deletes the old folder. Delete the backup too once you no longer want a way back.

Add `--data-dir /mnt/user/appdata/plaid-proxy-data` to the first command to keep the live data outside the new folder (see "Where things live").

### What was renamed (for anyone who set things by hand)

| Before (Goblin Bank) | Now |
| --- | --- |
| `goblins.yaml`, key `goblins:` | `people.yaml`, key `people:` (`gateway.yaml` too) |
| `goblins.example.yaml` | `people.example.yaml` |
| Python package `goblinbank`, module `goblin.py` | `plaidproxy`, `worker.py` |
| `ROLE=goblin` | `ROLE=worker` |
| `GOBLIN_NAME` | `PERSON_NAME` |
| `GOBLIN_DB` (default `/data/goblin.db`) | `WORKER_DB` (default `/data/proxy.db`) |
| `GOBLIN_AUTOSYNC` | `WORKER_AUTOSYNC` |
| `GOBLIN_NEW_PASSWORD`, `GOBLIN_PASSWORD_<NAME>` (install script) | `PROXY_NEW_PASSWORD`, `PROXY_PASSWORD_<NAME>` |
| services / hosts `goblin-<name>` | `worker-<name>` |
| image `goblin-bank`, compose project = folder name | image `plaid-proxy`, compose project `plaid-proxy` |
| header `X-Goblin` (between containers) | `X-Person` |
| `.secrets/<name>.env` loaded into the container | `.secrets/<name>.worker.env` (no password hashes; `$` escaped) |
| `CONNECTOR_SECRET` and the Robinhood connectors' `INTERNAL_SECRET` written in the compose file | `.secrets/<name>.worker.env` and `.secrets/<name>.connector.env` |
| new | `data_dir:` in `people.yaml` / `PLAID_PROXY_DATA_DIR` |

**Kept on purpose**, because they are stored elsewhere or read by other apps: the `goblin-bank` tag and internal reference on transactions
already in Firefly III (so nothing old looks new), the IDs of calendar events (`goblin-sub-...`), the `gb_` prefix of API tokens, the
`goblins`/`goblin` keys in `/api/v1/summary` (Pondering Orb reads them), and Pondering Orb's own `GOBLIN_ORB_KEY` setting. Your Docker
network name (`network:` in the yaml) is also kept, since Firefly III may be on it.

## Run without the install script
```bash
cp people.example.yaml people.yaml && python3 scripts/gen_compose.py     # needs python3 with pyyaml + cryptography
docker compose -f docker-compose.generated.yml up -d --build
```

## Test without a Plaid account
```bash
python3 -m pytest                     # unit tests
python3 scripts/synthetic_e2e.py      # real processes + a synthetic Plaid server (devtools/fake_plaid.py), 31 checks
python3 scripts/dev_stack.py          # gateway + worker with random data + a fake model server, to click around (token: login-alice)
```
The synthetic server follows Plaid's documented response shapes. It proves our services work together; it cannot prove real Plaid agrees. Get free sandbox keys from Plaid before trusting it with real banks.

## Not done yet
* Plaid is tested against the real Sandbox, never production. Banks that sign you in on their own website (OAuth) need `PLAID_REDIRECT_URI` set and registered with Plaid; the return step is built but has not been tried against a real bank.
* The agent is tested with scripted fake models. Try your real local model and see which tools it handles well.
* rh-agent tool names/auth are unverified (Robinhood docs don't publish them); configure via env.
* CalDAV *reading* (event -> money amount) and investments holdings via Plaid.

`python3 -m pytest` runs the tests.
