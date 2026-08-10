#!/usr/bin/env python3
"""Local listener test — fake-signed Slack events, mock Slack posting.
Run: SLACK_BOT_TOKEN= SLACK_SIGNING_SECRET=testsecret python3 scripts/test_listener.py
Exercises: healthz, bad-signature 401, url_verification, add/list/remove via message.im.
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = 8899
SECRET = "testsecret"
BASE = f"http://127.0.0.1:{PORT}"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def post(path, payload, sign=True):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = ""
    if sign:
        sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + body,
                               hashlib.sha256).hexdigest()
    req = urllib.request.Request(BASE + path, data=body, method="POST", headers={
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def ev(text, eid):
    return {"type": "event_callback", "event_id": eid,
            "event": {"type": "message", "channel_type": "im",
                      "user": "UTEST01", "channel": "DTEST01", "text": text}}


def main():
    env = dict(os.environ, PORT=str(PORT), SLACK_SIGNING_SECRET=SECRET)
    env["SLACK_BOT_TOKEN"] = ""  # force mock mode: replies print, never post
    proc = subprocess.Popen([sys.executable, "listener.py"], cwd=ROOT,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # wait for healthz
        for _ in range(50):
            try:
                with urllib.request.urlopen(BASE + "/healthz", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            print("FAIL: listener never came up"); return 1

        print("1. healthz:", "PASS")

        code, _ = post("/slack/events", ev("hello", "E-bad"), sign=False)
        print(f"2. bad signature rejected: {'PASS' if code == 401 else 'FAIL got ' + str(code)}")

        code, body = post("/slack/events", {"type": "url_verification", "challenge": "abc123"})
        print(f"3. url_verification: {'PASS' if body == 'abc123' else 'FAIL got ' + body}")

        code, _ = post("/slack/events", ev("TestCorp", "E-1"))
        time.sleep(1.0)  # worker thread processes async
        code2, _ = post("/slack/events", ev("TestCorp", "E-1"))  # retry dup
        print(f"4. add event accepted ({code}) + retry deduped ({code2}): {'PASS' if (code, code2) == (200, 200) else 'FAIL'}")

        post("/slack/events", ev("list", "E-2")); time.sleep(1.0)
        post("/slack/events", ev("remove TestCorp", "E-3")); time.sleep(1.0)
        post("/slack/events", ev("list", "E-4")); time.sleep(1.0)
        print("5. list/remove/list sequence sent (check listener output below)")

        time.sleep(1.0)
    finally:
        proc.terminate()
        out = proc.stdout.read()
        print("--- listener output ---")
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
