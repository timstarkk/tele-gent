import os
import sys

# Load .env from current directory if it exists
_env_path = os.path.join(os.getcwd(), ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())

BOT_TOKEN = os.environ.get("BOT_TOKEN")
AUTHORIZED_USER_ID = os.environ.get("AUTHORIZED_USER_ID")

if not BOT_TOKEN:
    sys.exit("BOT_TOKEN environment variable not set")

if not AUTHORIZED_USER_ID:
    sys.exit("AUTHORIZED_USER_ID environment variable not set")

AUTHORIZED_USER_ID = int(AUTHORIZED_USER_ID)

# PTY settings
PTY_COLS = 120
PTY_ROWS = 40
TERM = "xterm-256color"

# Output buffering — wait this long after last output before sending
OUTPUT_BUFFER_DELAY = 0.3

# Telegram message limit
TELEGRAM_MAX_LENGTH = 4096

# Claude Code settings
CLAUDE_BIN = "claude"
CLAUDE_FLUSH_INTERVAL = 2.0
TMPDIR = os.environ.get("TMPDIR", "/tmp")
PERM_REQ_PATTERN = os.path.join(TMPDIR, "telebot_perm_req_{session_id}.json")

# tmux settings
TMUX_SESSION_NAME = "tele-gent"
TMUX_PIPE_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "tele-gent-pipe.log")

# Initial working directory (where the user launched the bot from)
START_DIR = os.environ.get("TELEBOT_START_DIR", os.path.expanduser("~"))
