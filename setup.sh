#!/usr/bin/env bash
set -e

CLAUDE_DIR="$HOME/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SETTINGS="$CLAUDE_DIR/settings.json"
HOOK_SCRIPT="hooks/telegram-permission.py"
ENV_FILE=".env"

echo "=== tele-gent setup ==="
echo

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install Python 3.10+ first."
    exit 1
fi

# Check Claude Code
if ! command -v claude &>/dev/null; then
    echo "Error: claude not found."
    echo "Install it with: npm install -g @anthropic-ai/claude-code"
    echo "Then run 'claude' once to authenticate."
    exit 1
fi

if [ ! -d "$CLAUDE_DIR" ]; then
    echo "Error: ~/.claude not found. Run 'claude' once to initialize it."
    exit 1
fi

# Install Python dependencies
echo "Installing Python dependencies..."
pip install -r requirements.txt
echo

# Copy hook script
echo "Installing permission hook..."
mkdir -p "$HOOKS_DIR"
cp "$HOOK_SCRIPT" "$HOOKS_DIR/telegram-permission.py"
echo "  Copied to $HOOKS_DIR/telegram-permission.py"

# Merge hook into settings.json
echo "Configuring Claude Code hooks..."
if [ ! -f "$SETTINGS" ]; then
    echo '{}' > "$SETTINGS"
fi

python3 - "$SETTINGS" <<'PYEOF'
import json
import sys

path = sys.argv[1]
with open(path) as f:
    settings = json.load(f)

hook_entry = {
    "hooks": [
        {
            "type": "command",
            "command": "python3 ~/.claude/hooks/telegram-permission.py",
            "timeout": 86400
        }
    ]
}

hooks = settings.setdefault("hooks", {})
pre_tool = hooks.setdefault("PreToolUse", [])

# Check if already configured
already = any(
    any(
        "telegram-permission" in h.get("command", "")
        for h in entry.get("hooks", [])
    )
    for entry in pre_tool
)

if already:
    print("  Hook already configured, skipping.")
else:
    pre_tool.append(hook_entry)
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
    print("  Added PreToolUse hook to", path)

PYEOF

# Prompt for env vars
echo
if [ -f "$ENV_FILE" ]; then
    echo "Found existing .env file. Skipping."
else
    read -p "Telegram bot token (from @BotFather): " BOT_TOKEN
    read -p "Your Telegram user ID (from @userinfobot): " USER_ID

    cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$BOT_TOKEN
AUTHORIZED_USER_ID=$USER_ID
EOF
    echo "  Saved to .env"
fi

echo
echo "=== Setup complete ==="
echo
echo "Run the bot with:"
echo "  source .env && python bot.py"
