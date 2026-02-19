import asyncio
import glob as globmod
import json
import logging
import os
import signal
import sys
import time
import uuid

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from tele_gent.config import (
    AUTHORIZED_USER_ID,
    BOT_TOKEN,
    CLAUDE_BIN,
    PERM_REQ_PATTERN,
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
_perm_pending = False
_perm_set_at = 0.0
_claude_watch_task = None
_last_response_uuid = None
_last_jsonl_path = None

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
def _get_latest_jsonl() -> str | None:
    """Find the most recently modified .jsonl in the Claude project dir for current CWD."""
    cwd = _get_pty_cwd()
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


def _snapshot_last_response_uuid():
    """Snapshot the current last response UUID so we only send NEW responses."""
    global _last_response_uuid, _last_jsonl_path
    jsonl_path = _get_latest_jsonl()
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
                        for block in inner.get("content", []):
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


# --- Permission deny helper ---
async def _deny_perm_and_wait():
    """Send Escape to dismiss the native permission prompt."""
    global _perm_pending
    if session is not None and session.alive:
        _tmux("send-keys", "-t", TMUX_SESSION_NAME, "Escape", check=False)
    _perm_pending = False
    await asyncio.sleep(0.5)


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
    msg += "\n\nReply y to allow, n to deny"
    return msg[:500]


# --- Claude watcher (permissions + exit detection + response extraction) ---
_SHELL_NAMES = frozenset(("bash", "zsh", "fish", "sh", "dash", "tcsh", "ksh"))


async def _claude_watcher():
    """Poll for permission requests, detect Claude exit, and extract responses."""
    global _perm_pending, _perm_set_at, _claude_mode, _last_response_uuid, _last_jsonl_path
    req_path = PERM_REQ_PATTERN.format(session_id=_telebot_session_id)
    _last_jsonl_size = 0  # track JSONL growth for stale perm detection

    # Grace period: skip exit detection for the first few seconds
    # so the shell→claude transition has time to happen
    started_at = time.time()
    exit_detect_after = 5.0  # seconds

    while _claude_mode:
        try:
            # --- JSONL path lookup (used by both perm and response sections) ---
            jsonl_path = _get_latest_jsonl()

            # --- Permission request polling ---
            if os.path.exists(req_path) and not _perm_pending:
                try:
                    with open(req_path) as f:
                        req = json.load(f)
                except (json.JSONDecodeError, IOError):
                    await asyncio.sleep(0.5)
                    continue
                tool_name = req.get("tool_name", "unknown")
                tool_input = req.get("tool_input", {})
                msg = _format_perm_request(tool_name, tool_input)
                await app.bot.send_message(chat_id=_chat_id, text=msg)
                try:
                    os.remove(req_path)
                except FileNotFoundError:
                    pass
                _perm_pending = True
                _perm_set_at = time.time()
                if jsonl_path:
                    try:
                        _last_jsonl_size = os.path.getsize(jsonl_path)
                    except OSError:
                        pass

            # --- JSONL response extraction (before exit check to avoid missing final response) ---
            if jsonl_path:
                # New JSONL file = new Claude session; snapshot to skip
                # any existing content (like the greeting)
                if jsonl_path != _last_jsonl_path:
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
            # If perm has been pending >5s and JSONL grew, the user answered
            # from the terminal — auto-clear _perm_pending
            if _perm_pending and (time.time() - _perm_set_at) > 5.0 and jsonl_path:
                try:
                    cur_size = os.path.getsize(jsonl_path)
                except OSError:
                    cur_size = _last_jsonl_size
                if cur_size > _last_jsonl_size:
                    _perm_pending = False

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
                    _perm_pending = False
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


# --- PTY lifecycle ---
def start_session():
    global session
    if session is not None and session.alive:
        session.stop_reading()
        session.kill()
    session = PTYSession()
    session.spawn(cwd=START_DIR, env={"TELEBOT_SESSION_ID": _telebot_session_id})


async def setup_reader():
    if session is not None and session.alive:
        await session.start_reading(send_output)


# --- Command handlers ---
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    global _chat_id
    _chat_id = update.effective_chat.id

    if session is not None:
        session.stop_reading()
        session.kill()

    start_session()
    await setup_reader()
    await update.message.reply_text("New terminal session started.")


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
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
    global _claude_mode, _perm_pending
    if _perm_pending:
        await _deny_perm_and_wait()
    await _exit_claude()
    _claude_mode = False
    _perm_pending = False
    _stop_claude_watcher()
    if session is not None:
        session.suppress_output = False
    await update.message.reply_text("Back to terminal mode.")


async def cmd_claude_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exit current Claude session and start a fresh one."""
    if not authorized(update):
        return
    global _claude_mode, _perm_pending
    if _perm_pending:
        await _deny_perm_and_wait()
    if _claude_mode:
        await _exit_claude()
        await asyncio.sleep(1.0)
    # Auto-start session if none exists
    if session is None or not session.alive:
        start_session()
        await setup_reader()
    # Start fresh Claude TUI
    _claude_mode = True
    _perm_pending = False
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
    global _claude_perm_mode, _perm_pending

    args = context.args
    if not args:
        await update.message.reply_text(f"Claude permission mode: {_claude_perm_mode}")
        return

    new_mode = args[0].lower()
    if new_mode not in ("normal", "auto", "plan"):
        await update.message.reply_text("Usage: /mode [normal|auto|plan]")
        return

    # If a permission is pending, deny it first
    if _perm_pending:
        await _deny_perm_and_wait()

    _claude_perm_mode = new_mode

    # If Claude TUI is running, restart it with new flags
    if _claude_mode:
        await _exit_claude()
        await asyncio.sleep(1.0)
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

    lines = ["Recent sessions:"]
    _resume_sessions = []
    for i, (sid, preview, mtime) in enumerate(sessions, 1):
        ago = _format_time_ago(mtime)
        lines.append(f"{i}. {preview} ({ago})")
        _resume_sessions.append(sid)
    lines.append("\nReply with a number to resume.")
    _resume_pending = True
    await update.message.reply_text("\n".join(lines))


# --- Text message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    global _chat_id, _claude_mode, _perm_pending, _resume_pending, _resume_sessions
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
                await _exit_claude()
                await asyncio.sleep(1.0)
            # Enter Claude mode with --resume
            _claude_mode = True
            _perm_pending = False
            session.suppress_output = True
            _snapshot_last_response_uuid()
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
    if text == "claude" or text.startswith("claude "):
        # Auto-start session if none exists
        if session is None or not session.alive:
            start_session()
            await setup_reader()
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
            if _perm_pending:
                await _deny_perm_and_wait()
            if session is not None and session.alive:
                session.send_signal_char("\x03")
                await update.message.reply_text("Claude cancelled.")
            return

        # Permission response (only when a permission is pending)
        # Allow — press Enter to accept pre-selected "Allow once"
        if _perm_pending and text.lower() in ("y", "yes"):
            if session is not None and session.alive:
                _tmux("send-keys", "-t", TMUX_SESSION_NAME, "Enter", check=False)
            _perm_pending = False
            await update.message.reply_text("Allowed.")
            return

        # Deny — press Escape to dismiss
        if _perm_pending and text.lower() in ("n", "no"):
            if session is not None and session.alive:
                _tmux("send-keys", "-t", TMUX_SESSION_NAME, "Escape", check=False)
            _perm_pending = False
            await update.message.reply_text("Denied.")
            return

        # Any other message while permission pending — auto-deny and forward
        if _perm_pending:
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

    global _chat_id, _perm_pending
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
        if _perm_pending:
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
    global _chat_id, _perm_pending
    _chat_id = update.effective_chat.id

    if _claude_mode:
        _start_claude_watcher()  # no-op if already running
        text = update.message.text  # e.g. "/research_codebase the auth module"
        if _perm_pending:
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

    # Register handlers — commands first, then catch-all text
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

    # Run with polling (no webhooks, no open ports)
    sys.stderr.write(f"Starting bot... attach terminal: tmux attach -t {TMUX_SESSION_NAME}\n")
    sys.stderr.flush()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
