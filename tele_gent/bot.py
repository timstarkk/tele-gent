import atexit
import asyncio
import glob as globmod
import json
import logging
import os
import re
import sys
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tele_gent.config import (
    AUTHORIZED_USER_ID,
    BOT_TOKEN,
    CLAUDE_BIN,
    PERM_REQ_GLOB,
    START_DIR,
    TELEGRAM_MAX_LENGTH,
    TMUX_PIPE_FILE,
    TMUX_SESSION_NAME,
)
from tele_gent.pty_manager import PTYSession, _tmux

IMAGES_DIR = os.path.expanduser("~/.claude/images")
os.makedirs(IMAGES_DIR, exist_ok=True)

VOICE_DIR = os.path.expanduser("~/.claude/voice")
os.makedirs(VOICE_DIR, exist_ok=True)

# Lazy-loaded whisper model (initialized on first voice message)
_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
    return _whisper_model

# --- Zero logging: suppress all loggers ---
logging.disable(logging.CRITICAL)
# Extra safety: suppress httpx token leaking
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("telegram").setLevel(logging.CRITICAL)
logging.getLogger("telegram.ext").setLevel(logging.CRITICAL)

# --- Globals ---
session = None
app = None
_chat_id = None

# Claude mode state
_claude_mode = False
_claude_perm_mode = "normal"  # "normal", "auto", or "plan"
_telebot_session_id = uuid.uuid4().hex[:12]
_perm_queue: list[dict] = []  # each: {"uid", "tool_name", "tool_input", "sent_at"}
_claude_watch_task = None
_last_response_uuid = None
_last_jsonl_path = None
_jsonl_locked = False
_claude_cwd = None  # Pinned CWD at claude-mode entry (before Claude chdir's)

# Resume flow state
_resume_pending = False
_resume_sessions = []  # list of session IDs (jsonl stems)

# Claude JSONL conversation directory
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


# --- Auth ---
def authorized(update: Update) -> bool:
    user = update.effective_user
    if user is None or user.id != AUTHORIZED_USER_ID:
        return False
    return True


# --- Output sending ---
async def send_output(text: str):
    """Send terminal output back to Telegram, chunked if needed."""
    global _chat_id
    if _chat_id is None or app is None:
        return

    # Wrap in monospace code block
    # Split into chunks respecting Telegram's 4096 char limit
    # Reserve space for code block markers (``` + ``` + newlines = ~8 chars)
    max_chunk = TELEGRAM_MAX_LENGTH - 10
    chunks = []
    while text:
        if len(text) <= max_chunk:
            chunks.append(text)
            break
        # Find a good split point (newline near the limit)
        split_at = text.rfind("\n", 0, max_chunk)
        if split_at == -1 or split_at < max_chunk // 2:
            split_at = max_chunk
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    for chunk in chunks:
        msg = f"```\n{chunk}\n```"
        try:
            await app.bot.send_message(
                chat_id=_chat_id,
                text=msg,
                parse_mode="Markdown",
            )
        except Exception:
            # Fallback: send without markdown if parsing fails
            try:
                await app.bot.send_message(chat_id=_chat_id, text=chunk)
            except Exception:
                pass


# --- Build claude start command ---
def _build_claude_start_cmd() -> str:
    """Build the command to start the interactive Claude TUI."""
    parts = [CLAUDE_BIN]
    if _claude_perm_mode == "auto":
        parts.append("--dangerously-skip-permissions")
    elif _claude_perm_mode == "plan":
        parts.extend(["--permission-mode", "plan"])
    return " ".join(parts)


# --- Claude TUI readiness detection ---
def _get_pipe_size() -> int:
    try:
        return os.path.getsize(TMUX_PIPE_FILE)
    except OSError:
        return 0


async def _wait_for_claude_ready(timeout=5.0):
    """Wait for Claude TUI to produce output (welcome screen), then it's ready."""
    start = time.time()
    initial_size = _get_pipe_size()
    while time.time() - start < timeout:
        if _get_pipe_size() > initial_size:
            await asyncio.sleep(0.5)
            return True
        await asyncio.sleep(0.2)
    return False


async def _dismiss_trust_prompt():
    """Wait for Claude TUI to start, then send Enter to dismiss the trust prompt.
    The trust dialog has 'Yes, I trust this folder' pre-selected — Enter accepts it.
    If there's no trust prompt (already trusted), Enter at the input is harmless."""
    await _wait_for_claude_ready()
    if session is not None and session.alive:
        _tmux("send-keys", "-t", TMUX_SESSION_NAME, "Enter", check=False)
        await asyncio.sleep(1.5)


async def _start_and_prompt(prompt: str):
    """Wait for Claude TUI to be ready, dismiss trust prompt, then send the initial prompt."""
    await _dismiss_trust_prompt()
    if session is not None and session.alive:
        session.send_line(prompt.replace("\n", " "))


# --- Graceful Claude exit ---
async def _exit_claude():
    """Gracefully exit Claude TUI: /exit → wait → C-c → wait → C-c."""
    if session is None or not session.alive:
        return
    session.send_line("/exit")
    await asyncio.sleep(2.0)
    session.send_signal_char("\x03")
    await asyncio.sleep(1.0)
    session.send_signal_char("\x03")


# --- CWD tracking ---
def _get_pty_cwd() -> str:
    """Get the current working directory from the tmux session."""
    if session is None or not session.alive:
        return START_DIR
    return session.get_cwd()


# --- JSONL response extraction ---
def _get_latest_jsonl(cwd_override=None) -> str | None:
    """Find the most recently modified .jsonl in the Claude project dir for current CWD."""
    cwd = cwd_override or _get_pty_cwd()
    project_slug = cwd.replace("/", "-")
    project_dir = os.path.join(CLAUDE_PROJECTS_DIR, project_slug)
    if not os.path.isdir(project_dir):
        return None
    jsonls = [os.path.join(project_dir, f) for f in os.listdir(project_dir) if f.endswith(".jsonl")]
    if not jsonls:
        return None
    return max(jsonls, key=os.path.getmtime)


def _extract_last_response(jsonl_path: str, after_uuid: str | None,
                           allow_pending: bool = False) -> tuple[str | None, str | None]:
    """Read JSONL, find completed assistant text responses after after_uuid.

    Claude Code writes multiple JSONL lines per turn: a spacing text, a thinking
    block, then the real text response. A turn is considered complete when a
    non-assistant message (user/system/progress) follows it.

    If allow_pending=True, also returns the in-progress turn at EOF (used for
    final check when Claude exits or file is stale).

    Returns (response_text, last_uuid_seen) or (None, None).
    """
    messages = []
    found_marker = after_uuid is None
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not found_marker:
                if msg.get("uuid") == after_uuid:
                    found_marker = True
                continue
            messages.append(msg)

    if not messages:
        return None, None

    # Walk through messages, collecting completed assistant turns.
    last_completed_text = None
    last_completed_uuid = None
    current_turn_texts = []
    current_turn_last_uuid = None

    for msg in messages:
        msg_type = msg.get("type")
        if msg_type == "assistant":
            inner = msg.get("message", {})
            for block in inner.get("content", []):
                if block.get("type") == "text":
                    t = block.get("text", "")
                    if t.strip():
                        current_turn_texts.append(t)
            current_turn_last_uuid = msg.get("uuid")
        else:
            # Non-assistant message: if we had accumulated text, the turn is complete
            if current_turn_texts and current_turn_last_uuid:
                last_completed_text = "\n\n".join(current_turn_texts)
                last_completed_uuid = current_turn_last_uuid
            current_turn_texts = []
            current_turn_last_uuid = None

    # Handle pending turn at EOF
    if allow_pending and current_turn_texts and current_turn_last_uuid:
        last_completed_text = "\n\n".join(current_turn_texts)
        last_completed_uuid = current_turn_last_uuid

    return last_completed_text, last_completed_uuid


def _snapshot_last_response_uuid(pinned_jsonl=None):
    """Snapshot the current last response UUID so we only send NEW responses."""
    global _last_response_uuid, _last_jsonl_path, _jsonl_locked
    if pinned_jsonl and os.path.exists(pinned_jsonl):
        _jsonl_locked = True
        jsonl_path = pinned_jsonl
    else:
        _jsonl_locked = False
        jsonl_path = _get_latest_jsonl(_claude_cwd)
    if jsonl_path:
        # Use allow_pending=True so we bookmark past any existing assistant
        # messages at EOF (like Claude's greeting), avoiding re-sending them
        _, _last_response_uuid = _extract_last_response(jsonl_path, None, allow_pending=True)
        _last_jsonl_path = jsonl_path
    else:
        _last_response_uuid = None
        _last_jsonl_path = None


# --- Recent sessions listing for /resume ---
def _list_recent_sessions(n=5):
    """Return list of (session_id, preview, mtime) for the n most recent sessions.

    session_id is the JSONL filename stem (UUID), preview is the first user
    message truncated to 60 chars, mtime is the file modification time.
    """
    cwd = _get_pty_cwd()
    project_slug = cwd.replace("/", "-")
    project_dir = os.path.join(CLAUDE_PROJECTS_DIR, project_slug)
    if not os.path.isdir(project_dir):
        return []
    jsonls = globmod.glob(os.path.join(project_dir, "*.jsonl"))
    if not jsonls:
        return []
    # Sort by mtime descending
    jsonls.sort(key=os.path.getmtime, reverse=True)
    results = []
    for path in jsonls[:n]:
        session_id = os.path.splitext(os.path.basename(path))[0]
        mtime = os.path.getmtime(path)
        preview = ""
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("type") == "user":
                        inner = msg.get("message", {})
                        content = inner.get("content", [])
                        if isinstance(content, str):
                            preview = content.strip()
                        else:
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    preview = block.get("text", "").strip()
                                    break
                                elif isinstance(block, str):
                                    preview = block.strip()
                                    break
                        if preview:
                            break
        except (IOError, OSError):
            pass
        if len(preview) > 60:
            preview = preview[:57] + "..."
        results.append((session_id, preview or "(no preview)", mtime))
    return results


def _format_time_ago(mtime):
    """Format mtime as a human-readable 'X ago' string."""
    delta = time.time() - mtime
    if delta < 60:
        return "just now"
    elif delta < 3600:
        mins = int(delta // 60)
        return f"{mins} min ago"
    elif delta < 86400:
        hrs = int(delta // 3600)
        return f"{hrs} hr{'s' if hrs > 1 else ''} ago"
    else:
        days = int(delta // 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"


# --- Send Claude response (markdown, not code block) ---
async def send_claude_response(text: str):
    """Send Claude's response to Telegram as plain markdown (not wrapped in code blocks)."""
    if _chat_id is None or app is None:
        return
    max_chunk = TELEGRAM_MAX_LENGTH - 50
    chunks = []
    while text:
        if len(text) <= max_chunk:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_chunk)
        if split_at == -1 or split_at < max_chunk // 2:
            split_at = max_chunk
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    for chunk in chunks:
        try:
            await app.bot.send_message(
                chat_id=_chat_id,
                text=chunk,
                parse_mode="Markdown",
            )
        except Exception:
            try:
                await app.bot.send_message(chat_id=_chat_id, text=chunk)
            except Exception:
                pass


# --- Permission response helpers ---
def _send_perm_keystroke(decision: str) -> bool:
    """Send a keystroke to Claude's native permission prompt in tmux.

    Returns True if the prompt was visible and keystroke sent, False if stale.
    """
    if not _is_perm_prompt_visible():
        return False
    if decision == "allow":
        _tmux("send-keys", "-t", TMUX_SESSION_NAME, "Enter", check=False)
    else:
        _tmux("send-keys", "-t", TMUX_SESSION_NAME, "Escape", check=False)
    return True


async def _deny_perm_and_wait():
    """Deny all pending permissions by sending Escape keystrokes.

    Stops early if no prompt is visible (already handled elsewhere).
    Polls for the next prompt between items instead of using a fixed sleep.
    """
    global _perm_queue
    for item in _perm_queue:
        await _remove_buttons(item.get("msg_id", 0))
        if not _send_perm_keystroke("deny"):
            break  # No prompt visible — remaining items are stale
        await _wait_for_perm_prompt(timeout=2.0)
    _perm_queue = []


# --- Permission request formatting ---
def _format_perm_request(tool_name: str, tool_input: dict) -> str:
    """Format a permission request for Telegram display, with truncation."""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        desc = cmd[:200] + ("..." if len(cmd) > 200 else "")
        msg = f"Claude wants to run:\n{desc}"
    elif tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path", "unknown")
        msg = f"Claude wants to use: {tool_name}\nFile: {path}"
    else:
        inp = str(tool_input)[:200]
        if len(str(tool_input)) > 200:
            inp += "..."
        msg = f"Claude wants to use: {tool_name}\n{inp}"
    return msg[:500]


def _short_perm_desc(tool_name: str, tool_input: dict) -> str:
    """Return a short description like 'Bash: git diff' or 'Edit: src/foo.py'."""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"Bash: {cmd}"
    elif tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path", "unknown")
        return f"{tool_name}: {path}"
    else:
        return tool_name


def _perm_keyboard(uid: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard with Approve/Deny buttons for a permission request."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"perm_allow_{uid}"),
            InlineKeyboardButton("Deny", callback_data=f"perm_deny_{uid}"),
        ]
    ])


async def _remove_buttons(msg_id: int):
    """Remove inline keyboard from a Telegram message. Silently ignores failures."""
    try:
        await app.bot.edit_message_reply_markup(
            chat_id=_chat_id, message_id=msg_id, reply_markup=None,
        )
    except Exception:
        pass  # Message may be too old or already edited


_PERM_PROMPT_RE = re.compile(r"1\.\s*Yes\s+2\.\s*No")


def _is_perm_prompt_visible() -> bool:
    """Check if Claude's permission prompt is currently visible in tmux."""
    if session is None or not session.alive:
        return False
    return bool(_PERM_PROMPT_RE.search(session.capture_pane()))


async def _wait_for_perm_prompt(timeout: float = 5.0) -> bool:
    """Poll until a permission prompt appears or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _is_perm_prompt_visible():
            return True
        await asyncio.sleep(0.5)
    return False


# --- Claude watcher (permissions + exit detection + response extraction) ---
_SHELL_NAMES = frozenset(("bash", "zsh", "fish", "sh", "dash", "tcsh", "ksh"))


async def _claude_watcher():
    """Poll for permission requests, detect Claude exit, and extract responses."""
    global _perm_queue, _claude_mode, _last_response_uuid, _last_jsonl_path, _jsonl_locked
    req_glob = PERM_REQ_GLOB.format(session_id=_telebot_session_id)
    # Grace period: skip exit detection for the first few seconds
    # so the shell→claude transition has time to happen
    started_at = time.time()
    exit_detect_after = 5.0  # seconds

    while _claude_mode:
        try:
            # --- JSONL path lookup (locked after first switch to avoid
            # picking up other Claude sessions' files) ---
            if _jsonl_locked and _last_jsonl_path and os.path.exists(_last_jsonl_path):
                jsonl_path = _last_jsonl_path
            else:
                jsonl_path = _get_latest_jsonl(_claude_cwd)

            # --- Permission request polling ---
            MAX_PERM_QUEUE = 20
            req_files = sorted(globmod.glob(req_glob), key=os.path.getmtime)
            for req_file in req_files[:MAX_PERM_QUEUE - len(_perm_queue)]:
                try:
                    with open(req_file) as f:
                        req = json.load(f)
                except (json.JSONDecodeError, IOError):
                    continue
                tool_name = req.get("tool_name", "unknown")
                tool_input = req.get("tool_input", {})
                uid = req.get("uid", "")
                if not re.fullmatch(r'[0-9a-f]{1,16}', uid):
                    continue
                msg = _format_perm_request(tool_name, tool_input)
                pending_count = len(_perm_queue) + 1
                if pending_count > 1:
                    msg = f"[{pending_count} pending] {msg}"
                sent = await app.bot.send_message(
                    chat_id=_chat_id, text=msg,
                    reply_markup=_perm_keyboard(uid),
                )
                try:
                    os.remove(req_file)
                except FileNotFoundError:
                    pass
                _perm_queue.append({
                    "uid": uid,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "sent_at": time.time(),
                    "msg_id": sent.message_id,
                })

            # --- JSONL response extraction (before exit check to avoid missing final response) ---
            # Skip while permission is pending to avoid interleaving Claude text
            # with the permission prompt
            if jsonl_path and not _perm_queue:
                # New JSONL file = new Claude session; snapshot to skip
                # any existing content (like the greeting)
                if jsonl_path != _last_jsonl_path:
                    _jsonl_locked = True
                    _, _last_response_uuid = _extract_last_response(
                        jsonl_path, None, allow_pending=True,
                    )
                    _last_jsonl_path = jsonl_path

                # If the file hasn't been modified in 3+ seconds, the turn is
                # likely complete — allow pending (EOF) responses
                stale = (time.time() - os.path.getmtime(jsonl_path)) > 3.0
                text, new_uuid = _extract_last_response(
                    jsonl_path, _last_response_uuid, allow_pending=stale,
                )
                if text:
                    _last_response_uuid = new_uuid
                    await send_claude_response(text)

            # --- Stale permission detection ---
            # Two signals: (1) JSONL mtime advanced past the queue item,
            # (2) queue item >3s old and no prompt visible in tmux.
            if _perm_queue:
                now = time.time()
                stale_reason = None
                front_sent = _perm_queue[0]["sent_at"]

                # Signal 1: JSONL activity after the prompt was queued
                if jsonl_path:
                    try:
                        jsonl_mtime = os.path.getmtime(jsonl_path)
                    except OSError:
                        jsonl_mtime = 0
                    if jsonl_mtime > front_sent + 2.0:
                        stale_reason = "handled from terminal"

                # Signal 2: prompt old enough and not visible in tmux
                if not stale_reason and (now - front_sent) > 3.0:
                    if not _is_perm_prompt_visible():
                        stale_reason = "prompt no longer visible"

                if stale_reason:
                    for item in _perm_queue:
                        desc = _short_perm_desc(item["tool_name"], item["tool_input"])
                        await _remove_buttons(item.get("msg_id", 0))
                        try:
                            await app.bot.send_message(
                                chat_id=_chat_id,
                                text=f"{desc}: {stale_reason}",
                            )
                        except Exception:
                            pass
                    _perm_queue = []

                # Auto-deny items older than 60s to avoid infinite hangs
                while _perm_queue and (now - _perm_queue[0]["sent_at"]) > 60.0:
                    await _remove_buttons(_perm_queue[0].get("msg_id", 0))
                    _send_perm_keystroke("deny")
                    _perm_queue.pop(0)
                    await asyncio.sleep(0.3)

            # --- Claude exit detection (after grace period) ---
            if (time.time() - started_at > exit_detect_after
                    and session is not None and session.alive):
                fg = session.get_foreground_command()
                if fg in _SHELL_NAMES:
                    # Final JSONL check with allow_pending — grab anything left
                    if jsonl_path:
                        text, new_uuid = _extract_last_response(
                            jsonl_path, _last_response_uuid, allow_pending=True,
                        )
                        if text:
                            _last_response_uuid = new_uuid
                            await send_claude_response(text)
                    _claude_mode = False
                    session.suppress_output = False
                    for item in _perm_queue:
                        await _remove_buttons(item.get("msg_id", 0))
                        _send_perm_keystroke("deny")
                    _perm_queue = []
                    try:
                        await app.bot.send_message(
                            chat_id=_chat_id,
                            text="Claude exited. Back to terminal mode.",
                        )
                    except Exception:
                        pass
                    break

        except Exception as e:
            try:
                await app.bot.send_message(chat_id=_chat_id, text=f"[watcher error] {e}")
            except Exception:
                pass
        await asyncio.sleep(1.0)


def _start_claude_watcher():
    global _claude_watch_task
    if _claude_watch_task is not None and not _claude_watch_task.done():
        return
    _claude_watch_task = asyncio.ensure_future(_claude_watcher())


def _stop_claude_watcher():
    global _claude_watch_task
    if _claude_watch_task is not None:
        _claude_watch_task.cancel()
        _claude_watch_task = None


# --- Cleanup stale perm files from previous sessions ---
_perm_files_cleaned = False

def _cleanup_old_perm_files():
    """Remove any leftover perm files from previous sessions."""
    global _perm_files_cleaned
    if _perm_files_cleaned:
        return
    _perm_files_cleaned = True
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    for pattern in ("telebot_perm_req_*.json",):
        for f in globmod.glob(os.path.join(tmpdir, pattern)):
            try:
                os.remove(f)
            except OSError:
                pass


# --- PTY lifecycle ---
def start_session():
    global session
    _cleanup_old_perm_files()
    if session is not None and session.alive:
        session.stop_reading()
        session.kill()
    session = PTYSession()
    session.spawn(cwd=START_DIR, env={
        "TELEBOT_SESSION_ID": _telebot_session_id,
    })


async def setup_reader():
    if session is not None and session.alive:
        await session.start_reading(send_output)


# --- Command handlers ---
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    global _chat_id, _claude_mode, _perm_queue
    _chat_id = update.effective_chat.id

    _stop_claude_watcher()
    _claude_mode = False
    _perm_queue = []

    if session is not None:
        session.stop_reading()
        session.kill()

    start_session()
    await setup_reader()
    await update.message.reply_text("New terminal session started.")


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    global _claude_mode, _perm_queue

    _stop_claude_watcher()
    _claude_mode = False
    _perm_queue = []

    if session is not None and session.alive:
        session.stop_reading()
        session.kill()
        await update.message.reply_text("Session killed.")
    else:
        await update.message.reply_text("No active session.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if session is not None:
        await update.message.reply_text(session.status())
    else:
        await update.message.reply_text("No active session.")


async def cmd_ctrl_c(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if session is not None and session.alive:
        session.send_signal_char("\x03")
    else:
        await update.message.reply_text("No active session.")


async def cmd_ctrl_d(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if session is not None and session.alive:
        session.send_signal_char("\x04")
    else:
        await update.message.reply_text("No active session.")


async def cmd_ctrl_z(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    if session is not None and session.alive:
        session.send_signal_char("\x1a")
    else:
        await update.message.reply_text("No active session.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start — Telegram sends this when you first open the bot."""
    if not authorized(update):
        return
    global _chat_id
    _chat_id = update.effective_chat.id
    await update.message.reply_text(
        "Terminal bot ready.\n"
        "Send any text to execute in the terminal.\n"
        'Type "claude <prompt>" to enter Claude mode.\n\n'
        "Commands:\n"
        "/new — new terminal session\n"
        "/kill — kill current session\n"
        "/status — session info\n"
        "/terminal — exit Claude mode, back to terminal\n"
        "/claude_new — reset Claude conversation\n"
        "/resume — resume a recent Claude session\n"
        "/mode — show/set Claude permission mode (normal/auto/plan)\n"
        "/ctrl_c — send Ctrl+C\n"
        "/ctrl_d — send Ctrl+D\n"
        "/ctrl_z — send Ctrl+Z"
    )


# --- Claude mode command handlers ---
async def cmd_terminal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exit Claude mode, back to terminal."""
    if not authorized(update):
        return
    global _claude_mode, _perm_queue
    if _perm_queue:
        await _deny_perm_and_wait()
    _stop_claude_watcher()
    await _exit_claude()
    _claude_mode = False
    _perm_queue = []
    if session is not None:
        await asyncio.sleep(1.0)       # let remaining exit output drain while still suppressed
        session._output_buffer = ""    # discard any buffered exit noise
        session.suppress_output = False
    await update.message.reply_text("Back to terminal mode.")


async def cmd_claude_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exit current Claude session and start a fresh one."""
    if not authorized(update):
        return
    global _claude_mode, _perm_queue, _claude_cwd
    if _perm_queue:
        await _deny_perm_and_wait()
    if _claude_mode:
        _stop_claude_watcher()
        await _exit_claude()
        await asyncio.sleep(1.0)
    # Auto-start session if none exists
    if session is None or not session.alive:
        start_session()
        await setup_reader()
    # Start fresh Claude TUI
    _claude_cwd = _get_pty_cwd()
    _claude_mode = True
    _perm_queue = []
    session.suppress_output = True
    _snapshot_last_response_uuid()
    _start_claude_watcher()
    session.send_line(_build_claude_start_cmd())
    asyncio.ensure_future(_dismiss_trust_prompt())
    await update.message.reply_text("Fresh Claude session started.")


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show or set Claude permission mode (normal/auto/plan)."""
    if not authorized(update):
        return
    global _claude_perm_mode, _perm_queue, _claude_cwd

    args = context.args
    if not args:
        await update.message.reply_text(f"Claude permission mode: {_claude_perm_mode}")
        return

    new_mode = args[0].lower()
    if new_mode not in ("normal", "auto", "plan"):
        await update.message.reply_text("Usage: /mode [normal|auto|plan]")
        return

    # If permissions are pending, deny them first
    if _perm_queue:
        await _deny_perm_and_wait()

    _claude_perm_mode = new_mode

    # If Claude TUI is running, restart it with new flags
    if _claude_mode:
        _stop_claude_watcher()
        await _exit_claude()
        await asyncio.sleep(1.0)
        _claude_cwd = _get_pty_cwd()
        if session is not None and session.alive:
            session.send_line(_build_claude_start_cmd())
            asyncio.ensure_future(_dismiss_trust_prompt())
        await update.message.reply_text(f"Claude restarted with mode: {_claude_perm_mode}")
    else:
        await update.message.reply_text(f"Claude permission mode set to: {_claude_perm_mode}")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List recent Claude sessions for resuming."""
    if not authorized(update):
        return
    global _chat_id, _resume_pending, _resume_sessions
    _chat_id = update.effective_chat.id

    # Auto-start session if none exists (need cwd for project dir lookup)
    if session is None or not session.alive:
        start_session()
        await setup_reader()

    sessions = _list_recent_sessions(5)
    if not sessions:
        await update.message.reply_text("No recent sessions found.")
        return

    buttons = []
    _resume_sessions = []
    for i, (sid, preview, mtime) in enumerate(sessions, 1):
        ago = _format_time_ago(mtime)
        label = f"{i}. {preview[:30]} ({ago})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"resume_{i}")])
        _resume_sessions.append(sid)
    keyboard = InlineKeyboardMarkup(buttons)
    _resume_pending = True
    await update.message.reply_text("Recent sessions:", reply_markup=keyboard)


# --- Text message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    global _chat_id, _claude_mode, _perm_queue, _resume_pending, _resume_sessions, _claude_cwd
    _chat_id = update.effective_chat.id

    text = update.message.text

    # --- Resume selection handling ---
    if _resume_pending and text.strip().isdigit():
        idx = int(text.strip())
        if 1 <= idx <= len(_resume_sessions):
            session_id = _resume_sessions[idx - 1]
            _resume_pending = False
            _resume_sessions = []
            # Auto-start terminal session if needed
            if session is None or not session.alive:
                start_session()
                await setup_reader()
            # Exit current Claude if running
            if _claude_mode:
                _stop_claude_watcher()
                await _exit_claude()
                await asyncio.sleep(1.0)
            # Enter Claude mode with --resume
            _claude_cwd = _get_pty_cwd()
            _claude_mode = True
            _perm_queue = []
            session.suppress_output = True
            project_slug = _claude_cwd.replace("/", "-")
            expected_jsonl = os.path.join(CLAUDE_PROJECTS_DIR, project_slug, f"{session_id}.jsonl")
            _snapshot_last_response_uuid(pinned_jsonl=expected_jsonl)
            _start_claude_watcher()
            cmd = _build_claude_start_cmd() + f" --resume {session_id}"
            session.send_line(cmd)
            asyncio.ensure_future(_dismiss_trust_prompt())
            await update.message.reply_text(f"Resuming session {idx}...")
            return
        else:
            _resume_pending = False
            _resume_sessions = []
            await update.message.reply_text("Invalid selection. Resume cancelled.")
            return

    # Clear resume pending if user sends something else
    if _resume_pending:
        _resume_pending = False
        _resume_sessions = []

    # --- Claude trigger: "claude" or "claude <prompt>" ---
    text_lower = text.lower()
    if text_lower == "claude" or text_lower.startswith("claude "):
        # Auto-start session if none exists
        if session is None or not session.alive:
            start_session()
            await setup_reader()
        _claude_cwd = _get_pty_cwd()
        _claude_mode = True
        session.suppress_output = True
        _snapshot_last_response_uuid()
        _start_claude_watcher()
        prompt = text[len("claude"):].strip()
        # Start the interactive Claude TUI
        session.send_line(_build_claude_start_cmd())
        if prompt:
            asyncio.ensure_future(_start_and_prompt(prompt))
        else:
            asyncio.ensure_future(_dismiss_trust_prompt())
            await update.message.reply_text("Claude mode. Send a message or /terminal to exit.")
        return

    # --- Claude mode handling ---
    if _claude_mode:
        _start_claude_watcher()  # no-op if already running
        # ^C cancels active Claude process
        if text == "^C":
            if _perm_queue:
                await _deny_perm_and_wait()
            if session is not None and session.alive:
                session.send_signal_char("\x03")
                await update.message.reply_text("Claude cancelled.")
            return

        # Permission response (only when permissions are pending)
        # Allow — pop first item, send Enter to Claude's native prompt
        if _perm_queue and text.lower() in ("y", "yes"):
            item = _perm_queue.pop(0)
            # Refresh timestamps so stale detection doesn't race
            now = time.time()
            for remaining in _perm_queue:
                remaining["sent_at"] = now
            desc = _short_perm_desc(item["tool_name"], item["tool_input"])
            if not _send_perm_keystroke("allow"):
                # Prompt not visible — already handled elsewhere
                _perm_queue = []
                await update.message.reply_text(
                    f"{desc}: already handled (prompt not visible)"
                )
                return
            if _perm_queue:
                # Wait for the next prompt to render before showing it
                if await _wait_for_perm_prompt(3.0):
                    next_item = _perm_queue[0]
                    next_desc = _short_perm_desc(next_item["tool_name"], next_item["tool_input"])
                    await update.message.reply_text(
                        f"Allowed: {desc}\n\nNext: {next_desc}\n({len(_perm_queue)} pending) y/n?"
                    )
                else:
                    # Claude consumed remaining prompts (auto-approved, etc.)
                    _perm_queue = []
                    await update.message.reply_text(f"Allowed: {desc}\n(remaining items cleared — no prompt visible)")
            else:
                await update.message.reply_text(f"Allowed: {desc}")
            return

        # Deny — pop first item, send Escape to Claude's native prompt
        if _perm_queue and text.lower() in ("n", "no"):
            item = _perm_queue.pop(0)
            # Refresh timestamps so stale detection doesn't race
            now = time.time()
            for remaining in _perm_queue:
                remaining["sent_at"] = now
            desc = _short_perm_desc(item["tool_name"], item["tool_input"])
            if not _send_perm_keystroke("deny"):
                _perm_queue = []
                await update.message.reply_text(
                    f"{desc}: already handled (prompt not visible)"
                )
                return
            if _perm_queue:
                if await _wait_for_perm_prompt(3.0):
                    next_item = _perm_queue[0]
                    next_desc = _short_perm_desc(next_item["tool_name"], next_item["tool_input"])
                    await update.message.reply_text(
                        f"Denied: {desc}\n\nNext: {next_desc}\n({len(_perm_queue)} pending) y/n?"
                    )
                else:
                    _perm_queue = []
                    await update.message.reply_text(f"Denied: {desc}\n(remaining items cleared — no prompt visible)")
            else:
                await update.message.reply_text(f"Denied: {desc}")
            return

        # Any other message while permission pending — auto-deny all and forward
        if _perm_queue:
            await _deny_perm_and_wait()
            if session is not None and session.alive:
                session.send_signal_char("\x03")
            await asyncio.sleep(0.5)

        # Auto-start session if none exists
        if session is None or not session.alive:
            start_session()
            await setup_reader()

        # Send directly into the running Claude TUI (join multi-line into one)
        session.send_line(text.replace("\n", " "))
        return

    # --- Normal terminal mode below ---

    # Auto-start session if none exists
    if session is None or not session.alive:
        start_session()
        await setup_reader()

    # Check for special shortcuts
    if text == "^C":
        session.send_signal_char("\x03")
        return
    if text == "^D":
        session.send_signal_char("\x04")
        return
    if text == "^Z":
        session.send_signal_char("\x1a")
        return
    if text == ".":
        session.write("\n")
        return
    if text == "^[":
        session.send_signal_char("\x1b")
        return

    # Send to PTY
    session.send_line(text)


# --- Photo handler ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    global _chat_id
    _chat_id = update.effective_chat.id

    # Get the highest resolution photo
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    # Save with timestamp
    ts = int(time.time())
    filename = f"img_{ts}.jpg"
    filepath = os.path.join(IMAGES_DIR, filename)
    await file.download_to_drive(filepath)

    caption = update.message.caption or ""

    # Auto-start session if none exists
    if session is None or not session.alive:
        start_session()
        await setup_reader()

    if caption:
        # Send caption with image path to the PTY
        session.send_line(f"{caption} {filepath}")
        await update.message.reply_text(f"Saved: {filepath}\nSent to terminal with caption.")
    else:
        await update.message.reply_text(f"Saved: {filepath}")


# --- Voice handler ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    global _chat_id, _perm_queue
    _chat_id = update.effective_chat.id

    file = await context.bot.get_file(update.message.voice.file_id)
    ts = int(time.time())
    filepath = os.path.join(VOICE_DIR, f"voice_{ts}.ogg")
    await file.download_to_drive(filepath)

    await update.message.reply_text("Transcribing...")

    model = _get_whisper_model()
    segments, _ = model.transcribe(filepath)
    text = " ".join(seg.text for seg in segments).strip()

    if not text:
        await update.message.reply_text("Could not transcribe audio.")
        return

    await update.message.reply_text(f"Heard: {text}")

    # Auto-start session if none exists
    if session is None or not session.alive:
        start_session()
        await setup_reader()

    if _claude_mode:
        _start_claude_watcher()
        if _perm_queue:
            await _deny_perm_and_wait()
            if session is not None and session.alive:
                session.send_signal_char("\x03")
            await asyncio.sleep(0.5)
        session.send_line(text.replace("\n", " "))
    else:
        # In terminal mode, just display — don't auto-execute voice as commands
        await update.message.reply_text("(Terminal mode: transcription shown only, not executed)")


# --- Catch-all for unrecognized /commands ---
async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward unrecognized slash commands to Claude when in Claude mode."""
    if not authorized(update):
        return
    global _chat_id, _perm_queue
    _chat_id = update.effective_chat.id

    if _claude_mode:
        _start_claude_watcher()  # no-op if already running
        text = update.message.text  # e.g. "/research_codebase the auth module"
        if _perm_queue:
            await _deny_perm_and_wait()
            if session is not None and session.alive:
                session.send_signal_char("\x03")
            await asyncio.sleep(0.5)

        # Auto-start session if none exists
        if session is None or not session.alive:
            start_session()
            await setup_reader()

        # Send directly into the running Claude TUI
        session.send_line(text.replace("\n", " "))
    else:
        await update.message.reply_text("Unknown command. Send /start for help.")


# --- Inline keyboard callback handler ---
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button taps for permissions and resume."""
    global _perm_queue, _resume_pending, _resume_sessions, _claude_mode, _claude_cwd
    query = update.callback_query
    if query.from_user.id != AUTHORIZED_USER_ID:
        await query.answer("Unauthorized.")
        return

    data = query.data

    # --- Permission buttons ---
    if data.startswith("perm_allow_") or data.startswith("perm_deny_"):
        action = "allow" if data.startswith("perm_allow_") else "deny"
        uid = data.split("_", 2)[2]  # perm_allow_{uid} or perm_deny_{uid}

        # Validate: must be front of queue with matching UID
        if not _perm_queue:
            await query.answer("Expired or already handled.")
            await _remove_buttons(query.message.message_id)
            return

        if _perm_queue[0]["uid"] != uid:
            # Check if it's anywhere in the queue (out-of-order tap)
            if any(item["uid"] == uid for item in _perm_queue):
                await query.answer("Handle the first prompt first.")
            else:
                await query.answer("Expired or already handled.")
                await _remove_buttons(query.message.message_id)
            return

        item = _perm_queue.pop(0)
        # Refresh timestamps so stale detection doesn't race
        now = time.time()
        for remaining in _perm_queue:
            remaining["sent_at"] = now
        desc = _short_perm_desc(item["tool_name"], item["tool_input"])

        if not _send_perm_keystroke(action):
            # Prompt not visible — already handled elsewhere
            _perm_queue = []
            await query.answer(f"{desc}: already handled")
            await _remove_buttons(query.message.message_id)
            return

        label = "Allowed" if action == "allow" else "Denied"
        await query.answer(f"{label}: {desc}"[:200])
        await _remove_buttons(query.message.message_id)

        # If more pending, wait for next tmux prompt to render
        if _perm_queue:
            if not await _wait_for_perm_prompt(3.0):
                # Claude consumed remaining prompts
                for stale_item in _perm_queue:
                    await _remove_buttons(stale_item.get("msg_id", 0))
                _perm_queue = []
        return

    # --- Resume buttons ---
    if data.startswith("resume_"):
        idx_str = data.split("_", 1)[1]
        if not idx_str.isdigit():
            await query.answer("Invalid selection.")
            return
        idx = int(idx_str)

        if not _resume_pending or not (1 <= idx <= len(_resume_sessions)):
            await query.answer("Expired or invalid.")
            await _remove_buttons(query.message.message_id)
            return

        session_id = _resume_sessions[idx - 1]
        _resume_pending = False
        _resume_sessions = []
        await _remove_buttons(query.message.message_id)
        await query.answer(f"Resuming session {idx}...")

        # Auto-start terminal session if needed
        if session is None or not session.alive:
            start_session()
            await setup_reader()
        # Exit current Claude if running
        if _claude_mode:
            _stop_claude_watcher()
            await _exit_claude()
            await asyncio.sleep(1.0)
        # Enter Claude mode with --resume
        _claude_cwd = _get_pty_cwd()
        _claude_mode = True
        _perm_queue = []
        session.suppress_output = True
        project_slug = _claude_cwd.replace("/", "-")
        expected_jsonl = os.path.join(CLAUDE_PROJECTS_DIR, project_slug, f"{session_id}.jsonl")
        _snapshot_last_response_uuid(pinned_jsonl=expected_jsonl)
        _start_claude_watcher()
        cmd = _build_claude_start_cmd() + f" --resume {session_id}"
        session.send_line(cmd)
        asyncio.ensure_future(_dismiss_trust_prompt())
        return

    await query.answer("Unknown action.")


# --- App lifecycle ---
async def post_init(application: Application):
    """Called after the application is initialized — send startup notification."""
    global _chat_id, app
    app = application

    # Start a PTY session immediately
    start_session()
    await setup_reader()

    # Send startup message to the authorized user
    try:
        await application.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=(
                "Terminal bot started.\n"
                f"Attach locally: tmux attach -t {TMUX_SESSION_NAME}\n"
                "Send /start for help."
            ),
        )
        _chat_id = AUTHORIZED_USER_ID
    except Exception:
        pass


async def shutdown(application: Application):
    """Cleanup on shutdown."""
    _stop_claude_watcher()
    if session is not None:
        session.stop_reading()
        session.kill()


def main():
    global app

    builder = Application.builder().token(BOT_TOKEN)
    application = builder.post_init(post_init).post_shutdown(shutdown).build()
    app = application

    # Register handlers — callback queries, commands, then catch-all text
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("new", cmd_new))
    application.add_handler(CommandHandler("kill", cmd_kill))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("ctrl_c", cmd_ctrl_c))
    application.add_handler(CommandHandler("ctrl_d", cmd_ctrl_d))
    application.add_handler(CommandHandler("ctrl_z", cmd_ctrl_z))
    application.add_handler(CommandHandler("terminal", cmd_terminal))
    application.add_handler(CommandHandler("claude_new", cmd_claude_new))
    application.add_handler(CommandHandler("mode", cmd_mode))
    application.add_handler(CommandHandler("resume", cmd_resume))
    # Catch-all for unrecognized /commands — must be after specific CommandHandlers
    application.add_handler(
        MessageHandler(filters.COMMAND, handle_unknown_command)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )
    application.add_handler(
        MessageHandler(filters.VOICE, handle_voice)
    )

    # Ensure tmux session is killed on exit (Ctrl+C, crash, etc.)
    # post_shutdown only fires on clean exit, so atexit covers the rest.
    def _atexit_cleanup():
        if session is not None and session.alive:
            session.kill()

    atexit.register(_atexit_cleanup)

    # Run with polling (no webhooks, no open ports)
    sys.stderr.write(f"Starting bot... attach terminal: tmux attach -t {TMUX_SESSION_NAME}\n")
    sys.stderr.flush()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
