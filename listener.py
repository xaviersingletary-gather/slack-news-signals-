#!/usr/bin/env python3
"""signal-ping events listener — Slack DM subscriptions.

Reps DM @signals_os account names; this service verifies the request, resolves
the company (one LLM call when OPENROUTER_API_KEY is set), writes the watch row,
and confirms in-thread. That's the whole interface:

    bare name(s)  → add watch(es)   "Nestlé, FedEx" (comma/newline separated)
    remove X      → deactivate
    list          → your watches
    help          → the commands

Runs on Railway as a web service (binds 0.0.0.0:$PORT). Stdlib only beyond the
project deps. Signature verification per Slack docs (HMAC-SHA256, v0 basestring).
Ack-fast/process-async: 200 immediately, a single worker thread handles events
(serializes watch writes, avoids races). Mock mode when SLACK_BOT_TOKEN is unset:
replies are printed, not posted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _CTX = ssl.create_default_context()

import ping  # reuse State, config loader, dotenv loader

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "signal-ping-listener/1.0"}
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_EVENTS: queue.Queue = queue.Queue()
_SEEN_IDS: set[str] = set()
_SEEN_ORDER: list[str] = []


# ---------------------------------------------------------------------------
# Slack API
# ---------------------------------------------------------------------------

def slack_post(channel: str, text: str) -> None:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print(f"[mock] reply → {channel}: {text[:120]}")
        return
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps({"channel": channel, "text": text}).encode(),
        method="POST",
        headers={**UA, "Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
        body = json.loads(r.read().decode())
    if not body.get("ok"):
        print(f"slack post error: {body.get('error')}")


def verify_signature(body: bytes, ts: str, sig: str) -> bool:
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    if not secret:
        return False
    if not ts or abs(time.time() - int(ts)) > 300:
        return False
    base = f"v0:{ts}:{body.decode('utf-8')}"
    computed = "v0=" + hmac.new(secret.encode(), base.encode(),
                                hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, sig or "")


# ---------------------------------------------------------------------------
# alias normalization — one LLM call, defensive fallback
# ---------------------------------------------------------------------------

def normalize_company(raw: str, model: str) -> dict:
    """→ {canonical, aliases, note}. LLM when keyed; else passthrough."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {"canonical": raw, "aliases": [raw],
                "note": "aliases unverified (no LLM key)"}
    prompt = (
        "A salesperson typed a company name into a Slack bot to watch its news.\n"
        f"Input: {raw!r}\n"
        "Return ONLY json: {\"canonical\": official company name, \"aliases\": up to 4\n"
        "forms news headlines actually use (include accented/unaccented and legal-suffix\n"
        "variants; NO stock tickers; NO generic words that collide with other companies\n"
        "or with currency/quantities — e.g. bare 'CEVA' collides with a chip company,\n"
        "bare '3M' collides with £3M), \"note\": one short line if there is ambiguity\n"
        "risk in the alias set, else \"\"}."
    )
    body = json.dumps({"model": model, "temperature": 0.1, "max_tokens": 300,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, method="POST", headers={
        **UA, "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            text = json.loads(r.read().decode())["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", text, re.S)
        out = json.loads(m.group(0))
        if out.get("canonical") and out.get("aliases"):
            return {"canonical": out["canonical"], "aliases": out["aliases"][:4],
                    "note": out.get("note", "")}
    except Exception as exc:
        print(f"normalize failed ({type(exc).__name__}): {exc}")
    return {"canonical": raw, "aliases": [raw], "note": "aliases unverified"}


# ---------------------------------------------------------------------------
# intent handling
# ---------------------------------------------------------------------------

HELP = ("I watch your accounts for US supply-chain news (14-day window, daily digest).\n"
        "• DM me account names to watch: `Nestlé, FedEx`\n"
        "• `remove Nestlé` to stop • `list` to see your watches")


def handle_add(state: ping.State, user: str, channel: str, text: str, model: str) -> None:
    names = [n.strip() for n in re.split(r"[,\n;]|\band\b", text) if n.strip()]
    if not names:
        slack_post(channel, HELP)
        return
    existing = {w["canonical_name"].lower() for w in state.watches_for(user)}
    for name in names[:5]:  # one DM = max 5 adds
        info = normalize_company(name, model)
        canon = info["canonical"]
        if canon.lower() in existing:
            slack_post(channel, f"Already watching {canon}.")
            continue
        state.add_watch(user, canon, info["aliases"])
        existing.add(canon.lower())
        msg = (f"Watching: *{canon}* (aliases: {', '.join(info['aliases'])})\n"
               f"14-day US supply-chain window · digest at 7am ET when there's news · "
               f"`remove {canon}` to stop.")
        if info.get("note"):
            msg += f"\n_{info['note']}_"
        slack_post(channel, msg)
        print(f"watch added: {user} → {canon} {info['aliases']}")


def handle_event(state: ping.State, ev: dict, model: str) -> None:
    if ev.get("bot_id") or ev.get("subtype"):  # bots, edits, joins, etc.
        return
    user, channel = ev.get("user"), ev.get("channel")
    text = (ev.get("text") or "").strip()
    if not user or not channel or not text:
        return
    low = text.lower()
    if low in ("list", "my watches", "watching?", "ls"):
        ws = state.watches_for(user)
        msg = ("You're watching:\n" + "\n".join(f"• {w['canonical_name']}" for w in ws)
               if ws else "You're not watching any accounts yet. DM me a company name.")
        slack_post(channel, msg)
    elif low.startswith(("remove ", "unwatch ", "stop ")):
        target = re.sub(r"^(remove|unwatch|stop)\s+", "", text, flags=re.I).strip()
        if state.remove_watch(user, target):
            slack_post(channel, f"Stopped watching {target}.")
        else:
            slack_post(channel, f"No active watch matching “{target}”. `list` to see yours.")
    elif low in ("help", "hi", "hello", "hey", "?"):
        slack_post(channel, HELP)
    else:
        handle_add(state, user, channel, text, model)


def worker(cfg: dict) -> None:
    state = ping.State(HERE / "state.db")
    model = cfg.get("llm_model", "openai/gpt-4o-mini")
    while True:
        ev = _EVENTS.get()
        try:
            handle_event(state, ev, model)
        except Exception as exc:
            print(f"event handling failed: {type(exc).__name__}: {exc}")
            try:
                slack_post(ev.get("channel", ""), "Something broke on my end — try again in a minute.")
            except Exception:
                pass
        finally:
            _EVENTS.task_done()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet default logging; we print our own lines
        pass

    def do_GET(self):
        if self.path == "/healthz":
            self._reply(200, "ok")
        else:
            self._reply(404, "nope")

    def do_POST(self):
        if self.path != "/slack/events":
            self._reply(404, "nope")
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            self._reply(400, "bad json")
            return
        # url_verification is answered unverified — it's a harmless echo, and this
        # lets Slack's handshake succeed before SLACK_SIGNING_SECRET is configured.
        if payload.get("type") == "url_verification":
            self._reply(200, payload.get("challenge", ""), ctype="text/plain")
            return
        if not verify_signature(body, self.headers.get("X-Slack-Request-Timestamp", ""),
                                self.headers.get("X-Slack-Signature", "")):
            self._reply(401, "bad signature")
            return
        # dedupe retries
        eid = payload.get("event_id", "")
        if eid:
            if eid in _SEEN_IDS:
                self._reply(200, "dup")
                return
            _SEEN_IDS.add(eid)
            _SEEN_ORDER.append(eid)
            if len(_SEEN_ORDER) > 2000:
                _SEEN_IDS.discard(_SEEN_ORDER.pop(0))
        ev = payload.get("event", {})
        if payload.get("type") == "event_callback" and ev.get("type") == "message":
            _EVENTS.put(ev)
        self._reply(200, "ok")

    def _reply(self, code: int, text: str, ctype: str = "text/plain"):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    ping.load_dotenv(HERE / ".env")
    cfg = ping.load_config(HERE / "config.yaml")
    threading.Thread(target=worker, args=(cfg,), daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"listener up on :{port} — POST /slack/events, GET /healthz")
    srv.serve_forever()


if __name__ == "__main__":
    main()
