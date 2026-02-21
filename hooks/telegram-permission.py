#!/usr/bin/env python3
"""PreToolUse hook for Telegram bot integration.

When TELEBOT_SESSION_ID is set (bot-managed session), writes a permission
request file and polls for a response file from the bot. When not set
(normal interactive claude), auto-allows immediately.
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime

def _log(msg):
    """Append a timestamped checkpoint to the debug log."""
    logpath = os.path.join(os.environ.get("TMPDIR", "/tmp"), "telebot_hook_debug.log")
    try:
        with open(logpath, "a") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass

input_data = json.load(sys.stdin)
session_id = os.environ.get("TELEBOT_SESSION_ID")

# Not a bot-managed session — exit silently, Claude uses default behavior
if not session_id:
    sys.exit(0)

uid = uuid.uuid4().hex[:8]
_log(f"hook started, session={session_id}, uid={uid}")

tmpdir = os.environ.get("TMPDIR", "/tmp")
req_path = os.path.join(tmpdir, f"telebot_perm_req_{session_id}_{uid}.json")
resp_path = os.path.join(tmpdir, f"telebot_perm_resp_{session_id}_{uid}.json")

# Write request for bot to pick up
request = {
    "uid": uid,
    "tool_name": input_data.get("tool_name", "unknown"),
    "tool_input": input_data.get("tool_input", {}),
    "ts": int(time.time()),
}
with open(req_path, "w") as f:
    json.dump(request, f)
_log("req written")

# Resolve bot PID for liveness checks
bot_pid_str = os.environ.get("TELEBOT_PID")
bot_pid = int(bot_pid_str) if bot_pid_str else None
# Fallback: if no PID env var, use a 120s deadline
deadline = None if bot_pid is not None else time.time() + 120

_log(f"polling for resp at {resp_path}, bot_pid={bot_pid}")

while True:
    # Check deadline fallback (only when no PID available)
    if deadline is not None and time.time() >= deadline:
        _log("timeout waiting for response, denying")
        break

    # Check if bot process is still alive
    if bot_pid is not None:
        try:
            os.kill(bot_pid, 0)
        except OSError:
            _log("bot process gone, denying")
            break

    if os.path.exists(resp_path):
        _log("resp file found")
        try:
            with open(resp_path) as f:
                resp = json.load(f)
        except (json.JSONDecodeError, IOError):
            _log("resp file unreadable, retrying")
            time.sleep(0.5)
            continue
        try:
            os.remove(req_path)
        except FileNotFoundError:
            pass
        try:
            os.remove(resp_path)
        except FileNotFoundError:
            pass
        decision = resp.get("decision", "deny")
        _log(f"outputting decision: {decision}")
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision
            }
        }))
        sys.exit(0)
    time.sleep(1)

# Bot died or timeout — deny by default and clean up
try:
    os.remove(req_path)
except FileNotFoundError:
    pass
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny"
    }
}))
sys.exit(0)
