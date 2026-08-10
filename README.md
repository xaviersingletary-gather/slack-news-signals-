# Signal Ping

**Origin:** Rob's ask from the Aug 4 project review — "Show me in the next 24 hours
that you can build an exec mandate tool for one account... first run should be all the
stories that you've pulled that are in context, then your second run can be new ones
after that... push it to my Slack channel... if we're just testing, run once a day."

**Hard constraint:** standalone. Does NOT import from or depend on the SOS system
(`~/Hermes - Signals Watcher`). That project stays parked. Only shared asset is the
Slack bot (signals_os) and its token.

## What it does

Watches named accounts for supply-chain / logistics / warehousing / intralogistics
news (US-only). Posts a Slack digest of the qualifying stories — linked headline,
source, date, and a one-line outreach TLDR per story. Silent when nothing qualifies.

## Current rules (V1)

- **14-day rolling window** (`lookback_days: 14`). Stories older than the window
  never post.
- **Relevance:** the title must hit a supply-chain term (supply chain, logistics,
  warehouse, warehousing, intralogistics, distribution, fulfillment, inventory,
  distribution center) OR a capacity term (plant, factory, facility, manufacturing,
  site) plus an action (expand, invest, build, open, close, sell, shut, ...).
- **US-only geo filter.** A story is dropped if its title names a non-US place
  (case-sensitive list in config.yaml), carries a non-US currency symbol (£ € ₹ …),
  or its source domain ends in a non-US ccTLD (e.g. `nation.com.pk`). Asymmetric on
  purpose: US stories rarely say "US", so no foreign marker = pass.
- **Blocklists.** Stock-analysis / content-farm sources (`source_blacklist`) and
  soft-PR terms (scholarship, charity, foundation, donation, contest, giveaway) are
  dropped. Accounts can carry their own block terms (3M blocks its golf-tournament
  spam).
- **No TLDR, no post.** Each surviving story must match one of nine outreach-TLDR
  patterns (new leadership, capacity loss, partnership, rollout, divestiture,
  closure, tariffs, new capacity, resilience). No pattern match = the story is
  dropped — never posted with filler.
- **Dedup** by URL hash + normalized-title hash, per subscriber. Already-sent
  stories never repost; the same title from two outlets in one run keeps one copy.
- **15-story cap** per account per run. Overflow is recorded and deduped silently.

## Architecture (live now)

```
config.yaml ──► fetch ──► filter ──► TLDR ──► dedup ──► Block Kit ──► Slack
                 Google      alias     9        per
                 News RSS    geo       canned   subscriber
                 2 queries/  blocklists TLDRs
                 account     relevance
```

Fetch runs two shaped queries per account (supply-chain + exec), unioned by URL.
Alias match is case-sensitive only for short aliases (3M vs £3M). State lives in
SQLite locally, Postgres when `DATABASE_URL` is set.

**Two runners are deployed right now:**

| Runner | Schedule | State | Mode |
|---|---|---|---|
| Hermes cron `signal-ping-daily` (job 78248c75de10) | 7:00 AM ET, script-only via `~/.hermes/scripts/signal-ping.sh` | local `state.db` (SQLite) | **LIVE — the current posting path** → #account-signals |
| Railway cron service `signal-ping-worker` | `0 11 * * *` UTC | Postgres | Deployed in **mock mode** — prints what it would post, writes nothing, until `SLACK_BOT_TOKEN` is added as a Railway variable |

**Cutover rule:** the moment the Railway token lands, the Hermes cron job is
deleted at the same moment. Both live = double posts.

Watched accounts today: 3M, Procter & Gamble, Nestlé, MilliporeSigma, FedEx,
CEVA Logistics — all subscribed by the #account-signals channel. Adding an
account = config.yaml lines, no code.

## Production (target)

Reps DM the bot their account names; the tool does the rest.

- A rep DMs **@signals_os**: `Nestlé` → the events listener (Railway web service
  `signal-ping-listener`, listener.py in this repo) verifies the Slack request
  signature, normalizes the name + aliases with one LLM call, and confirms
  in-thread:
  `Watching: Nestlé (aliases: Nestlé, Nestle)`.
- Commands: bare names = add. `remove X` = stop watching. `list` = your watches.
- Railway project = three pieces:
  - **web service** — the Slack events listener
  - **cron worker** — `signal-ping-worker` (already deployed, runs the digest)
  - **Postgres** — two tables:
    - `watches`: subscriber_id (Slack user ID or channel), canonical_name,
      aliases, active
    - `sent`: (subscriber_id, url_hash) — per-subscriber dedup
- The worker already fans out: channel IDs get a direct post, user IDs get a DM
  via conversations.open. Per-rep digests need no worker changes — only watch rows.

Onboarding for the whole team is one pinned line in the channel:

> DM @signals_os your account names. That's it.

## Runbook

Files: `ping.py` (the whole engine, stdlib + certifi + PyYAML + psycopg),
`config.yaml` (accounts, queries, rules, gather_brief), `state.db` (local, created
on first run), `scripts/seed_state.py` + `scripts/seed_state.json` (Postgres
bootstrap), `railway.toml` (worker service def), `railway.listener.toml` (listener
web service def — deploys via `railway up --service signal-ping-listener` after
swapping it onto railway.toml; `railway up` has no --config flag and this repo
isn't git), `.env` (local secrets, gitignored).

Local dry run — audit trail, no post, no state writes:

```
cd ~/signal-ping
python3 ping.py --dry-run                  # all watched accounts
python3 ping.py --account "3M" --dry-run   # one account
python3 ping.py --lookback 30 --dry-run    # wider window
```

Environment variables (names only; values live in `.env` locally and in Railway
variables on the worker):

```
SLACK_BOT_TOKEN       signals_os bot token. Absent = mock mode (print, no post).
OPENROUTER_API_KEY    needed only when llm_filter: true in config.yaml
DATABASE_URL          set on Railway → Postgres state; absent → local state.db
SLACK_SIGNING_SECRET  needed when the events listener ships
```

Slack app admin (one-time, for DM subscriptions) — order matters:

1. Basic Information → copy the Signing Secret → set `SLACK_SIGNING_SECRET` on the
   `signal-ping-listener` Railway service (alongside `SLACK_BOT_TOKEN` and
   `OPENROUTER_API_KEY`). Setting a variable auto-redeploys the listener.
2. OAuth & Permissions → add bot scopes `im:history` + `im:write`.
3. Event Subscriptions → Enable → Request URL:
   `https://signal-ping-listener-production.up.railway.app/slack/events`
   Slack verifies it on save — this only goes green once step 1's secret is live.
   Then Subscribe to bot events → `message.im` → Save.
4. Reinstall the app to the workspace (banner appears after the scope change).

Railway:

```
Project:     https://railway.com/project/47351088-78c1-4c5d-939b-7fdd1ed892ac
Worker logs: railway logs --service signal-ping-worker
```

Hermes cron (until cutover):

```
Job:     signal-ping-daily, 7:00 AM ET, script-only (job 78248c75de10)
Wrapper: ~/.hermes/scripts/signal-ping.sh
Output:  ~/.hermes/cron/output/
```

`scripts/seed_state.py` runs first in the worker startCommand and self-seeds
Postgres: `sent` history from `seed_state.json` (exported from the local
state.db, so the hosted run doesn't repost old stories) plus `watches` from
config.yaml accounts. It only writes when the tables are empty — a no-op on
every run after the first. Delete it and `seed_state.json` once the hosted run
is proven.

## Status

| Component | State |
|---|---|
| Digest engine (ping.py: fetch → filter → TLDR → dedup → post) | LIVE |
| Hermes cron `signal-ping-daily` (7am ET, SQLite) | LIVE — current posting path |
| Railway worker `signal-ping-worker` (`0 11 * * *` UTC) | DEPLOYED — mock mode until token lands |
| Postgres state (watches + sent) | ONLINE — self-seeded |
| Slack events listener (DM subscriptions) | DEPLOYED — healthz ok at signal-ping-listener-production.up.railway.app; 401s events until Slack admin + 3 vars land |
| LLM pass (`llm_filter`) | CODE BUILT — pending OPENROUTER_API_KEY |
| homebase pull page | PENDING |
| Per-rep DM digests | PENDING — worker fan-out ready, needs user watch rows |

## Follow-ups (not built)

- **`llm_filter: true`** — one batched OpenRouter call per account, pinned to the
  `gather_brief` in config.yaml. Story-specific TLDRs (instead of the nine canned
  ones), trigger/context/drop decisions with region judgment, same-event
  clustering beyond exact-title match, and an angle block on triggers: who (buyer
  role), hook (story → pain we solve), opener (≤60-word LinkedIn first line).
  Falls back to keyword mode on any failure.
- **"Draft email?" button** per story → outreach draft in-thread (needs the
  signing secret, a public endpoint, and the OpenRouter key).
- **The events listener itself** — see Production.
- **Thumbs feedback loop** — reactions on stories feed back into filtering.
- **Perigon API swap** for fetch. Free Google News RSS limits: ~100-item cap per
  query, `when:>7d` silently ignored (the client-side date filter enforces the
  window instead), and Google redirect links are JS-gated. The fetcher sits
  behind a one-function interface so Perigon drops in.
- **Salesforce account-notes write** (needs an SFDC auth decision).
