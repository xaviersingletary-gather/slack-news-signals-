#!/usr/bin/env python3
"""signal-ping — account news monitoring → Slack.

fetch (Google News RSS) → filter (alias/currency/blacklist/relevance) →
classify (trigger/context, optional LLM pass) → dedup per subscriber (state.db) →
format (Block Kit) → fan-out post (channel direct; DM via conversations.open).

Stdlib + certifi + PyYAML only. Query shapes live in config.yaml; WHO watches
what lives in the watches table (seeded from config accounts → channel on first
run). Adding an account = config.yaml lines, no code.

Usage:
  python3 ping.py                  # all watched accounts
  python3 ping.py --account "3M"   # one account (canonical name)
  python3 ping.py --dry-run        # audit trail, no post, no sent-writes
  python3 ping.py --lookback 30    # override config lookback_days

Cron/no_agent contract: stdout stays EMPTY when there is no news and no error
(silent). One line per posted (account, subscriber) otherwise.
Any failure → stderr + exit 1.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # macOS python.org builds need certifi; linux usually fine without
    _CTX = ssl.create_default_context()

try:
    import yaml
except ImportError:
    sys.exit("pyyaml required: python3 -m pip install pyyaml")

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) signal-ping/1.0"}
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Default query shapes for watches with no config block (e.g. DM-added accounts).
DEFAULT_QUERY_SC = ('supply chain OR logistics OR warehouse OR warehousing OR '
                    'intralogistics OR distribution OR fulfillment OR inventory '
                    'OR "distribution center"')
DEFAULT_QUERY_EXEC = ('restructuring OR expansion OR appoints OR names OR chief OR plant '
                      'OR factory OR closes OR opens OR layoffs OR tariff OR '
                      '"supply chain officer"')

# ---------------------------------------------------------------------------
# env / config
# ---------------------------------------------------------------------------

def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Values are never printed."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    cfg.setdefault("lookback_days", 7)
    cfg.setdefault("max_stories", 15)
    cfg.setdefault("llm_filter", False)
    cfg.setdefault("llm_model", "openai/gpt-4o-mini")
    cfg.setdefault("source_blacklist", [])
    return cfg

# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def fetch_feed(query: str) -> list[dict]:
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
        root = ET.fromstring(r.read())
    import email.utils
    out = []
    for it in root.findall(".//item"):
        src = it.find("source")
        out.append({
            "title": it.findtext("title", ""),
            "link": it.findtext("link", ""),
            "source": src.text if src is not None else "?",
            "pub": email.utils.parsedate_to_datetime(it.findtext("pubDate")),
        })
    return out


def fetch_account(acct: dict, lookback: int) -> tuple[list[dict], int]:
    """Two shaped queries per account (supply-chain + exec), unioned by URL.
    Query window: when:7d is honored by Google and shapes recall for daily runs.
    Longer lookbacks: omit when: entirely (Google silently ignores >7d values)
    and let the client-side date filter enforce the window."""
    alias_q = " OR ".join(f'intitle:"{a}"' for a in acct["aliases"][:2])
    win = " when:7d" if lookback <= 7 else ""
    raw, seen, fetched = [], set(), 0
    for q in (f'({alias_q}) AND ({acct["query_sc"]}){win}',
              f'({alias_q}) AND ({acct["query_exec"]}){win}'):
        for item in fetch_feed(q):
            fetched += 1
            if item["link"] not in seen:
                seen.add(item["link"])
                raw.append(item)
    return raw, fetched

# ---------------------------------------------------------------------------
# filter — deterministic, every decision carries a reason
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Relevance: title must hit a supply-chain term, or a capacity term + action.
# Everything else is dropped. No story posts without a specific outreach TLDR.
# ---------------------------------------------------------------------------
SC_TERMS = ["supply chain", "logistics", "warehouse", "warehousing", "intralogistics",
            "distribution", "fulfillment", "inventory", "distribution center"]
CAPACITY_TERMS = ["plant", "factory", "facility", "manufacturing", "site"]
ACTION_TERMS = ["expand", "invest", "build", "open", "close", "sell", "shut",
                "inaugurate", "suspend", "halt"]
TRIGGER_TERMS = ["appoints", "names", "chairman", "chief", "ceo", "coo", "expand",
                 "inaugurate", "expansion", "opens", "closes", "restructuring",
                 "spin-off", "spinoff", "layoff", "job cuts", "selects", "partner",
                 "rollout", "invest", "tariff", "suspend", "shut", "halt",
                 "elevates", "promotes"]
CURRENCY = re.compile(r"[£$€\d]")


def _hit(term: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(term) + r"\w*", text) is not None


def alias_ok(title: str, aliases: list[str]) -> bool:
    """Alias present in company (non-amount) context. Case-SENSITIVE only for
    short aliases (<=4 chars) where case disambiguates (3M vs £3M/3m women);
    longer aliases match case-insensitively ('Ceva Logistics' == 'CEVA Logistics')."""
    for a in aliases:
        flags = 0 if len(a) <= 4 else re.IGNORECASE
        occ = [m.start() for m in re.finditer(re.escape(a), title, flags)]
        if occ and not all(p > 0 and CURRENCY.match(title[p - 1]) for p in occ):
            return True
    return False


def filter_items(items: list[dict], acct: dict, blacklist: set[str],
                 block_terms: list[str] | None = None,
                 non_us: dict | None = None) -> tuple[list[dict], dict]:
    kept, kills = [], {}
    blocks = [b.lower() for b in (block_terms or [])]
    geo_pats = []
    cctld = None
    if non_us:
        geo_pats = [re.compile(r"\b" + re.escape(p) + r"\b")          # places: case-sensitive
                    for p in non_us.get("places", [])]
        geo_pats += [re.compile(re.escape(c))                          # currency symbols: literal
                     for c in non_us.get("currencies", [])]
        # non-US ccTLDs in domain-like source names (e.g. "nation.com.pk")
        cctld = re.compile(r"\.(pk|in|ng|ie|uk|th|br|cl|hu|ae|za|mx|ca|de|fr|it|es|nl|pl|tr|"
                           r"ua|ru|jp|kr|au|sg|my|id|vn|ph|bd|ke|gh|eg|ma|ar|co|pe|ve|gt|jm|"
                           r"tt|cn|ch|se|no|dk|fi|pt|gr|cz|ro|il|sa|qa|kw|tn|dz)\b")
    for i in items:
        t = i["title"]
        if not alias_ok(t, acct["aliases"]):
            kills.setdefault("alias amount/absent", []).append(t)
            continue
        core = t.rsplit(" - ", 1)[0]
        low = core.lower()
        if any(b in low for b in blocks):
            kills.setdefault("blocklisted term", []).append(t)
            continue
        if geo_pats and (any(p.search((core + " " + i["source"]).replace("US$", ""))
                             for p in geo_pats)
                         or (cctld and cctld.search(i["source"]))):
            kills.setdefault("non-US", []).append(t)
            continue
        if i["source"] in blacklist:
            kills.setdefault("blacklisted source", []).append(t)
            continue
        sc_hit = any(_hit(k, low) for k in SC_TERMS)
        cap_hit = (any(_hit(k, low) for k in CAPACITY_TERMS)
                   and any(_hit(k, low) for k in ACTION_TERMS))
        if not (sc_hit or cap_hit):
            kills.setdefault("not supply-chain news", []).append(t)
            continue
        i["core"] = core
        i["trigger"] = any(_hit(w, low) for w in TRIGGER_TERMS)
        kept.append(i)
    return kept, kills

# ---------------------------------------------------------------------------
# why-lines (keyword mode) — word-boundary patterns, first match wins
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# TLDR lines (keyword mode) — why this matters for a salesperson's outreach.
# No pattern match = story is dropped, not posted with filler.
# ---------------------------------------------------------------------------

WHY = [
    (r"\bchairman\b|\bappoints\b|\bnames\b|\belevates\b|\bpromotes\b|\bchief\b",
     "TLDR: new leadership touchpoint — mandates get set in the first 90 days; ask what their visibility baseline is."),
    (r"\bsuspend\w*\b|\bshut\w*\b|\bhalt\w*\b|\bflooding\b|\bstrike\b",
     "TLDR: unplanned capacity loss — urgency window; ask how they hold inventory accuracy while sites are down."),
    (r"\bselects\b|\bautomation partner\b|\bpartnership\b",
     "TLDR: new partnership/vendor changes who runs the floor — ask how inventory truth travels across partners."),
    (r"\brollout\b",
     "TLDR: pilot-to-scale — adjacent tools get evaluated now; ask what data feeds the platform."),
    (r"\bsells?\b|\bdivest",
     "TLDR: divestiture — the buyer inherits and re-tools the site; the opening is at the buyer, not this account."),
    (r"\bcloses?\b|\bshutter\w*\b",
     "TLDR: site closure = network consolidation — volume shifts to surviving sites; ask how they absorb it without accuracy slipping."),
    (r"\btariffs?\b",
     "TLDR: tariffs redraw the network map — ask who owns inventory accuracy across the new nodes."),
    (r"\bexpand\w*\b|\binaugurat\w*\b|\binvest\w*\b|\bopens?\b|\bbuilds?\b|\bnew\b",
     "TLDR: new capacity — the build window is when ground-truth tooling gets specced; ask who owns visibility for the new site."),
    (r"\bresilien\w*\b|\bsustainab\w*\b",
     "TLDR: publicly investing in supply-chain resilience — mirror the theme in outreach; door-opener, not a trigger."),
]


def why_line(title: str) -> str | None:
    low = title.lower()
    for pat, v in WHY:
        if re.search(pat, low):
            return v
    return None

# ---------------------------------------------------------------------------
# LLM pass (optional) — one batched OpenRouter call, pinned to gather_brief
# ---------------------------------------------------------------------------

def llm_pass(stories: list[dict], brief: dict, model: str) -> bool:
    """Annotate stories in place: decision (trigger/context/drop), why, angle.
    Returns False on any failure (caller falls back to keyword annotations)."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("llm_filter: OPENROUTER_API_KEY not set — keyword mode", file=sys.stderr)
        return False
    payload_stories = [{"id": n, "headline": s["core"], "source": s["source"],
                        "date": s["pub"].strftime("%b %d")} for n, s in enumerate(stories)]
    prompt = (
        "You are the relevance-and-outreach engine for a sales signal tool at Gather AI.\n"
        "GROUND TRUTH BRIEF (yaml):\n" + yaml.safe_dump(brief, sort_keys=False) + "\n"
        "STORIES (json):\n" + json.dumps(payload_stories, ensure_ascii=False) + "\n\n"
        "For EACH story return one object: {\"id\", \"decision\", \"tldr\", \"angle\"}.\n"
        "decision rules:\n"
        "- \"drop\": the story is not about supply chain, logistics, warehousing, or\n"
        "  intralogistics; or it is financial-background, pure PR/awards; or the\n"
        "  story's region is outside the brief's regions. When in doubt, drop —\n"
        "  never force a marginal story to seem relevant.\n"
        "- \"trigger\": the story opens an outreach window (new capacity, disruption,\n"
        "  closure/consolidation, vendor selection, tariff-driven network change,\n"
        "  supply-chain leadership move).\n"
        "- \"context\": genuinely supply-chain relevant, but no outreach window.\n"
        "tldr: one sentence, story-specific, plain language — why this matters for a\n"
        "salesperson's outreach at this account. Start with 'TLDR:'.\n"
        "angle (triggers only, else null): {\"who\": one buyer_role from the brief,\n"
        "  \"hook\": story connected to a pains_we_solve entry, one sentence,\n"
        "  \"opener\": LinkedIn DM first line, <=60 words, references the story specifically}.\n"
        "HARD RULES: use only facts in the story and the brief; never invent names or\n"
        "metrics; obey never_say; if no pain intersection, return decision \"drop\".\n"
        "Respond with ONLY json: {\"stories\": [...]}."
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, method="POST", headers={
        **UA, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
            resp = json.loads(r.read().decode())
        text = resp["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0))
        by_id = {int(o["id"]): o for o in data["stories"]}
        for n, s in enumerate(stories):
            o = by_id.get(n)
            if not o:
                s["decision"] = "context"
                continue
            s["decision"] = o.get("decision", "context")
            if o.get("tldr"):
                s["why"] = o["tldr"]
            if o.get("angle") and s["decision"] == "trigger":
                s["angle"] = o["angle"]
        return True
    except Exception as exc:
        print(f"llm_pass failed ({type(exc).__name__}: {exc}) — keyword mode", file=sys.stderr)
        return False

# ---------------------------------------------------------------------------
# state (dedup)
# ---------------------------------------------------------------------------

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


class State:
    """Dedup + watchlist state. Backend: Postgres when DATABASE_URL is set
    (Railway), else local SQLite (dev). Schema auto-creates on connect.

    sent:     (subscriber_id, url_hash) PK — dedup is PER SUBSCRIBER.
    watches:  subscriber_id, canonical_name, aliases (JSON), active.
    """

    def __init__(self, path: Path):
        self.pg = None
        url = os.environ.get("DATABASE_URL")
        if url:
            import psycopg
            self.pg = psycopg.connect(url, autocommit=True)
            self.pg.execute("""CREATE TABLE IF NOT EXISTS sent (
                subscriber_id TEXT NOT NULL, url_hash TEXT NOT NULL, title_hash TEXT,
                account TEXT, title TEXT, first_sent_at TEXT,
                PRIMARY KEY (subscriber_id, url_hash))""")
            self.pg.execute("""CREATE TABLE IF NOT EXISTS watches (
                id SERIAL PRIMARY KEY, subscriber_id TEXT NOT NULL,
                canonical_name TEXT NOT NULL, aliases TEXT NOT NULL,
                active INTEGER DEFAULT 1, created_at TEXT)""")
            return
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS sent (
            url_hash TEXT PRIMARY KEY, title_hash TEXT, account TEXT,
            title TEXT, first_sent_at TEXT)""")
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(sent)")]
        if "subscriber_id" not in cols:
            self.db.execute("ALTER TABLE sent ADD COLUMN subscriber_id TEXT")
            self.db.commit()
        self.db.execute("""CREATE TABLE IF NOT EXISTS watches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, subscriber_id TEXT NOT NULL,
            canonical_name TEXT NOT NULL, aliases TEXT NOT NULL,
            active INTEGER DEFAULT 1, created_at TEXT)""")
        self.db.commit()

    def seen(self, story: dict, subscriber: str | None = None) -> bool:
        uh = _hash(story["link"])
        th = _hash(re.sub(r"[^a-z0-9]", "", story["core"].lower()))
        if self.pg:
            return self.pg.execute(
                "SELECT 1 FROM sent WHERE subscriber_id=%s AND (url_hash=%s OR title_hash=%s)",
                (subscriber, uh, th)).fetchone() is not None
        return self.db.execute(
            "SELECT 1 FROM sent WHERE url_hash=? OR title_hash=?", (uh, th)).fetchone() is not None

    def mark(self, story: dict, account: str, subscriber: str | None = None) -> None:
        uh = _hash(story["link"])
        th = _hash(re.sub(r"[^a-z0-9]", "", story["core"].lower()))
        ts = dt.datetime.now(dt.timezone.utc).isoformat()
        if self.pg:
            self.pg.execute(
                "INSERT INTO sent (subscriber_id,url_hash,title_hash,account,title,first_sent_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (subscriber_id,url_hash) DO NOTHING",
                (subscriber, uh, th, account, story["core"], ts))
            return
        self.db.execute("INSERT OR IGNORE INTO sent VALUES (?,?,?,?,?,?)",
                        (uh, th, account, story["core"], ts, subscriber))
        self.db.commit()

    def first_run(self, account: str, subscriber: str | None = None) -> bool:
        if self.pg:
            return self.pg.execute(
                "SELECT 1 FROM sent WHERE account=%s AND subscriber_id=%s LIMIT 1",
                (account, subscriber)).fetchone() is None
        return self.db.execute(
            "SELECT 1 FROM sent WHERE account=? LIMIT 1", (account,)).fetchone() is None

    # -- watches -----------------------------------------------------------
    def add_watch(self, subscriber: str, canonical: str, aliases: list[str]) -> None:
        ts = dt.datetime.now(dt.timezone.utc).isoformat()
        if self.pg:
            self.pg.execute(
                "INSERT INTO watches (subscriber_id,canonical_name,aliases,active,created_at) "
                "VALUES (%s,%s,%s,1,%s)", (subscriber, canonical, json.dumps(aliases), ts))
            return
        self.db.execute(
            "INSERT INTO watches (subscriber_id,canonical_name,aliases,active,created_at) "
            "VALUES (?,?,?,1,?)", (subscriber, canonical, json.dumps(aliases), ts))
        self.db.commit()

    def active_watches(self) -> list[dict]:
        q = "SELECT subscriber_id, canonical_name, aliases FROM watches WHERE active=1"
        rows = (self.pg.execute(q).fetchall() if self.pg
                else self.db.execute(q).fetchall())
        return [{"subscriber_id": r[0], "canonical_name": r[1],
                 "aliases": json.loads(r[2])} for r in rows]

    def watches_for(self, subscriber: str) -> list[dict]:
        q = ("SELECT canonical_name, aliases FROM watches "
             "WHERE active=1 AND subscriber_id=?")
        if self.pg:
            rows = self.pg.execute(q.replace("?", "%s"), (subscriber,)).fetchall()
        else:
            rows = self.db.execute(q, (subscriber,)).fetchall()
        return [{"canonical_name": r[0], "aliases": json.loads(r[1])} for r in rows]

    def remove_watch(self, subscriber: str, name: str) -> bool:
        """Deactivate a watch matching canonical name OR any alias (case-insensitive)."""
        q = ("SELECT id, canonical_name, aliases FROM watches "
             "WHERE active=1 AND subscriber_id=?")
        if self.pg:
            rows = self.pg.execute(q.replace("?", "%s"), (subscriber,)).fetchall()
        else:
            rows = self.db.execute(q, (subscriber,)).fetchall()
        want = name.strip().lower()
        for wid, canon, aliases_json in rows:
            names = [canon.lower()] + [a.lower() for a in json.loads(aliases_json)]
            if want in names:
                if self.pg:
                    self.pg.execute("UPDATE watches SET active=0 WHERE id=%s", (wid,))
                else:
                    self.db.execute("UPDATE watches SET active=0 WHERE id=?", (wid,))
                    self.db.commit()
                return True
        return False

    def watch_count(self) -> int:
        """Total watch rows (active or not) — startup seeding keys on this."""
        q = "SELECT COUNT(*) FROM watches"
        row = (self.pg.execute(q).fetchone() if self.pg
               else self.db.execute(q).fetchone())
        return row[0]

# ---------------------------------------------------------------------------
# format + post
# ---------------------------------------------------------------------------

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def story_text(s: dict) -> str:
    txt = (f"*<{s['link']}|{esc(s['core'])}>*\n"
           f"{esc(s['source'])} · {s['pub'].strftime('%b %d')}\n"
           f"_{esc(s.get('why', ''))}_")
    a = s.get("angle")
    if a:
        txt += (f"\n→ *Angle* ({esc(a.get('who', ''))}): {esc(a.get('hook', ''))}"
                f"\n_\"{esc(a.get('opener', ''))}\"_")
    return txt


def build_messages(account: str, stories: list[dict], lookback: int,
                   first_run: bool, max_stories: int) -> list[dict]:
    today = dt.datetime.now(dt.timezone.utc).strftime("%b %d, %Y")
    shown, overflow = stories[:max_stories], stories[max_stories:]
    sub = f"{len(stories)} new {'story' if len(stories)==1 else 'stories'} · lookback {lookback}d"
    if first_run:
        sub += " · first run = full window"
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"Signal Ping — {account} — {today}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": sub}]},
    ]
    for s in shown:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": story_text(s)}})
    if overflow:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                       "text": f"+{len(overflow)} more recorded (already deduped — won't repost)"}]})
    # chunk under Slack's 50-block ceiling (never expected, but cheap insurance)
    msgs, i = [], 0
    while i < max(1, len(blocks)):
        chunk = blocks[i:i + 45] if blocks else blocks
        msgs.append({"channel": None, "text": f"Signal Ping: {account} — {len(stories)} new",
                     "blocks": chunk})
        i += 45
        if not blocks:
            break
    return msgs


def open_dm(user_id: str, token: str) -> str:
    """conversations.open → DM channel id for a U… user."""
    req = urllib.request.Request("https://slack.com/api/conversations.open",
                                 data=json.dumps({"users": [user_id]}).encode(),
                                 method="POST",
                                 headers={**UA, "Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
        body = json.loads(r.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"slack conversations.open error: {body.get('error')}")
    return body["channel"]["id"]


def post(payload: dict, subscriber: str, token: str | None) -> bool:
    """Post one Block Kit message to a subscriber. C… = channel (direct post,
    today's behavior); U… = user (DM via conversations.open first).
    Mock mode (no token): print + return False, no state writes (caller keys
    on the return value)."""
    if not token:
        print(f"  [mock] would post to {subscriber}: {payload['text']}")
        return False
    payload["channel"] = (open_dm(subscriber, token) if subscriber.startswith("U")
                          else subscriber)
    req = urllib.request.Request("https://slack.com/api/chat.postMessage",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={**UA, "Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
        body = json.loads(r.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"slack error: {body.get('error')}")
    return True

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def gather_account(acct: dict, cfg: dict, lookback: int) -> dict:
    """Fetch → filter → TLDR → in-run dedup → optional LLM, ONCE per account.
    Returns {fetched, in_window, kept, kills}. Per-subscriber dedup happens in
    run_account — this is the expensive part and must not repeat per subscriber."""
    now = dt.datetime.now(dt.timezone.utc)
    raw, fetched = fetch_account(acct, lookback)
    in_win = [i for i in raw if -1 <= (now - i["pub"]).days <= lookback]
    block_terms = list(cfg.get("block_terms", [])) + list(acct.get("block_terms", []))
    non_us = cfg.get("non_us_geo") if cfg.get("us_only") else None
    kept, kills = filter_items(in_win, acct, set(cfg["source_blacklist"]), block_terms, non_us)

    no_tldr = []
    survivors = []
    for s in kept:
        w = why_line(s["core"])
        if w:
            s["why"] = w
            s["decision"] = "trigger" if s["trigger"] else "context"
            survivors.append(s)
        else:
            no_tldr.append(s["title"])
    if no_tldr:
        kills["no outreach TLDR"] = no_tldr
    kept = survivors

    # within-run same-title dedup (state.db only catches cross-run)
    seen_th = set()
    deduped = []
    for s in sorted(kept, key=lambda x: x["pub"], reverse=True):
        th = _hash(re.sub(r"[^a-z0-9]", "", s["core"].lower()))
        if th in seen_th:
            kills.setdefault("same event, other outlet (in-run)", []).append(s["title"])
            continue
        seen_th.add(th)
        deduped.append(s)
    kept = deduped
    if cfg["llm_filter"] and kept:
        if llm_pass(kept, cfg.get("gather_brief", {}), cfg["llm_model"]):
            kept = [s for s in kept if s["decision"] != "drop"]

    return {"fetched": fetched, "in_window": len(in_win), "kept": kept, "kills": kills}


def run_account(acct: dict, subscribers: list[str], cfg: dict, state: State,
                lookback: int, dry: bool) -> int:
    """Fan-out: gather once, then dedup + post per subscriber.
    Returns the largest per-subscriber new-story count (informational)."""
    g = gather_account(acct, cfg, lookback)
    kept, kills = g["kept"], g["kills"]
    ordered = sorted(kept, key=lambda x: x["pub"], reverse=True)
    # per-subscriber dedup — state.seen is per-subscriber on Postgres and
    # account-level on SQLite; either way the channel's behavior is unchanged
    per_sub = [(sub, [s for s in ordered if not state.seen(s, sub)])
               for sub in subscribers]

    if dry:
        print(f"=== DRY RUN: {acct['name']} — fetched {g['fetched']}, "
              f"in-window {g['in_window']}, kept {len(kept)}")
        for reason, titles in kills.items():
            print(f"  killed {len(titles):2} ({reason})")
        for sub, new in per_sub:
            print(f"  --- subscriber {sub} — new {len(new)}")
            for s in new:
                print(f"\n  [{s['decision'].upper()}] {s['core']}")
                print(f"      {s['source']} · {s['pub'].strftime('%b %d')} · {s['link'][:80]}…")
                print(f"      why: {s['why']}")
                if s.get("angle"):
                    a = s["angle"]
                    print(f"      angle({a.get('who','')}): {a.get('hook','')}")
                    print(f"      opener: \"{a.get('opener','')}\"")
        print()
        return max((len(new) for _, new in per_sub), default=0)

    token = os.environ.get("SLACK_BOT_TOKEN")
    best = 0
    for sub, new in per_sub:
        if not new:
            continue  # silent — per-subscriber silence contract
        msgs = build_messages(acct["name"], new, lookback,
                              state.first_run(acct["name"], sub), cfg["max_stories"])
        posted = False
        for m in msgs:
            posted = post(m, sub, token) or posted
        if posted:  # state only after a real successful post
            for s in new:
                state.mark(s, acct["name"], sub)
            print(f"signal-ping: posted {len(new)} new for {acct['name']} → {sub}")
        else:
            print(f"signal-ping: {len(new)} new for {acct['name']} → {sub} "
                  f"(mock — set SLACK_BOT_TOKEN)")
        best = max(best, len(new))
    return best


def seed_watches(state: State, cfg: dict) -> int:
    """Bootstrap the watchlist: one watch per config account, subscriber = the
    config channel. Only when the watches table is completely empty → idempotent.
    Returns rows inserted."""
    accounts = cfg.get("accounts") or []
    channel = cfg.get("channel_id")
    if state.watch_count() or not accounts or not channel:
        return 0
    for a in accounts:
        state.add_watch(channel, a["name"], a["aliases"])
    return len(accounts)


def account_for(name: str, watch_aliases: list[str], cfg: dict) -> dict:
    """Config block (query shape + aliases) for a canonical account name.
    A watch with no matching config block (DM-added account) gets default
    queries and the aliases stored on its watch rows."""
    for a in cfg.get("accounts", []):
        if a["name"] == name or a["name"].lower() == name.lower():
            return a
    return {"name": name, "aliases": watch_aliases or [name],
            "query_sc": DEFAULT_QUERY_SC, "query_exec": DEFAULT_QUERY_EXEC}


def main() -> int:
    ap = argparse.ArgumentParser(description="signal-ping")
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--account")
    ap.add_argument("--lookback", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    cfg = load_config(Path(args.config))
    lookback = args.lookback or cfg["lookback_days"]
    state = State(HERE / "state.db")

    seed_watches(state, cfg)
    watches = state.active_watches()
    if not watches:  # defensive: derive the watchlist from config if seeding couldn't
        channel = cfg.get("channel_id")
        watches = [{"subscriber_id": channel, "canonical_name": a["name"],
                    "aliases": a["aliases"]}
                   for a in cfg.get("accounts", [])] if channel else []

    # group watches by canonical account: gather once, fan out to subscribers
    by_account: dict[str, dict] = {}
    for w in watches:
        e = by_account.setdefault(w["canonical_name"], {"subscribers": [], "aliases": []})
        if w["subscriber_id"] not in e["subscribers"]:
            e["subscribers"].append(w["subscriber_id"])
        for al in w["aliases"]:
            if al not in e["aliases"]:
                e["aliases"].append(al)

    cfg_accounts = cfg.get("accounts", [])

    def _order(name: str) -> tuple:
        for i, a in enumerate(cfg_accounts):  # config order first
            if a["name"].lower() == name.lower():
                return (0, i)
        return (1, name.lower())  # then watches-only accounts, alphabetical

    names = sorted(by_account, key=_order)
    if args.account:
        names = [n for n in names if n.lower() == args.account.lower()]
        if not names:
            print(f"no such account in watches/config: {args.account}", file=sys.stderr)
            return 1

    failed = False
    for name in names:
        acct = account_for(name, by_account[name]["aliases"], cfg)
        try:
            run_account(acct, by_account[name]["subscribers"], cfg, state,
                        lookback, args.dry_run)
        except Exception as exc:
            print(f"signal-ping: {name} failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            failed = True

    # heartbeat: listener liveness (only once it's deployed and configured)
    listener_url = cfg.get("listener_url", "")
    if listener_url and not args.dry_run:
        try:
            req = urllib.request.Request(listener_url.rstrip("/") + "/healthz", headers=UA)
            with urllib.request.urlopen(req, timeout=10, context=_CTX) as r:
                if r.status != 200:
                    raise RuntimeError(f"healthz returned {r.status}")
        except Exception as exc:
            target = cfg.get("admin_slack_user") or cfg["channel_id"]
            msg = (f":warning: signal-ping: listener unreachable ({type(exc).__name__}) — "
                   f"DM subscriptions are down. Digest unaffected.")
            try:
                post({"text": msg, "blocks": []}, target, os.environ.get("SLACK_BOT_TOKEN"))
                print(f"signal-ping: heartbeat alert posted → {target}")
            except Exception as e2:
                print(f"heartbeat alert failed: {e2}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
