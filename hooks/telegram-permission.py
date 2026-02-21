#!/usr/bin/env python3
"""PreToolUse hook for Telegram bot integration.

When TELEBOT_SESSION_ID is set (bot-managed session), writes a permission
request file so the Telegram bot can notify the user, then returns "ask"
so Claude Code shows its native permission prompt in the terminal.
When not set (normal interactive claude), auto-allows immediately.
"""
import json
import os
import sys
import tempfile
import time
import uuid

input_data = json.load(sys.stdin)
session_id = os.environ.get("TELEBOT_SESSION_ID")

# Not a bot-managed session — exit silently, Claude uses default behavior
if not session_id:
    sys.exit(0)

uid = uuid.uuid4().hex[:8]
tmpdir = os.environ.get("TMPDIR", "/tmp")
req_path = os.path.join(tmpdir, f"telebot_perm_req_{session_id}_{uid}.json")

# Write request for bot to pick up (atomic: write to tmp, then rename)
request = {
    "uid": uid,
    "tool_name": input_data.get("tool_name", "unknown"),
    "tool_input": input_data.get("tool_input", {}),
    "ts": int(time.time()),
}
fd, tmp_path = tempfile.mkstemp(dir=tmpdir, suffix=".tmp")
try:
    with os.fdopen(fd, "w") as f:
        json.dump(request, f)
    os.rename(tmp_path, req_path)
except Exception:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

# AskUserQuestion: allow the tool so the interactive prompt renders in tmux.
# Other tools: ask — Claude shows its native permission prompt in tmux,
# and the bot can respond by sending keystrokes (Enter / Escape) to the pane.
decision = "allow" if input_data.get("tool_name") == "AskUserQuestion" else "ask"
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision
    }
}))
sys.exit(0)
