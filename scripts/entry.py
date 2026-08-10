#!/usr/bin/env python3
"""Railway entry dispatcher — one repo, two services, one railway.toml.
SERVICE_ROLE env var decides what runs:
  listener → the Slack events web service
  worker   → seed once (no-op after first run) then the daily digest
Set per service via: railway variables --service <name> --set SERVICE_ROLE=<role>
The worker's cron schedule lives in the service's Cron Schedule setting
(0 11 * * * UTC), not in config-as-code — keeps the file role-neutral.
"""
import os
import subprocess
import sys

role = os.environ.get("SERVICE_ROLE", "worker")

if role == "listener":
    os.execvp(sys.executable, [sys.executable, "listener.py"])

subprocess.check_call([sys.executable, "scripts/seed_state.py"])
os.execvp(sys.executable, [sys.executable, "ping.py"])
