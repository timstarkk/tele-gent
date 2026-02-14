import asyncio
import json
import logging
import os
import signal
import subprocess
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

from config import (
    AUTHORIZED_USER_ID,
    BOT_TOKEN,
    PERM_REQ_PATTERN,
    PERM_RESP_PATTERN,
    START_DIR,
    TELEGRAM_MAX_LENGTH,
)
from claude_runner import ClaudeRunner
from pty_manager import PTYSession

IMAGES_DIR = os.path.expanduser("~/.claude/images")
os.makedirs(IMAGES_DIR, exist_ok=True)

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
_claude_runner = ClaudeRunner(_telebot_session_id)
_perm_pending = False
_perm_watch_task = None


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


# --- Claude output sending ---
async def send_claude_output(text: str):
    """Send Claude output to Telegram, chunked if needed. Plain text (no code block)."""
    global _chat_id
    if _chat_id is None or app is None:
        return

    max_chunk = TELEGRAM_MAX_LENGTH - 10
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
            await app.bot.send_message(chat_id=_chat_id, text=chunk)
        except Exception:
            pass


# --- CWD tracking ---
def _get_pty_cwd() -> str:
    """Get the current working directory of the PTY child process."""
    if session is None or not session.alive or session.pid is None:
        return START_DIR
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(session.pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
    except Exception:
        pass
    return os.path.expanduser("~")


# --- Atomic file write helper ---
def _atomic_write_json(path: str, data: dict):
    """Write JSON atomically: write to temp file, then rename."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


# --- Permission deny helper ---
async def _deny_perm_and_wait():
    """Write a deny response file and wait for the hook to consume it."""
    global _perm_pending
    resp_path = PERM_RESP_PATTERN.format(session_id=_telebot_session_id)
    _atomic_write_json(resp_path, {"decision": "deny"})
    _perm_pending = False
    # Wait for hook to read + delete the file (polls every 1s)
    for _ in range(20):
        if not os.path.exists(resp_path):
            break
        await asyncio.sleep(0.1)


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


# --- Permission watcher ---
async def _perm_watcher():
    """Poll for permission request files and prompt the user."""
    global _perm_pending
    req_path = PERM_REQ_PATTERN.format(session_id=_telebot_session_id)

    while _claude_mode:
        try:
            # Safety net: clear stale _perm_pending if Claude exited unexpectedly
            if _perm_pending and not _claude_runner.active:
                _perm_pending = False
            if os.path.exists(req_path) and not _perm_pending:
                try:
                    with open(req_path) as f:
                        req = json.load(f)
                except (json.JSONDecodeError, IOError):
                    # File being written — retry next poll
                    await asyncio.sleep(0.5)
                    continue
                tool_name = req.get("tool_name", "unknown")
                tool_input = req.get("tool_input", {})
                msg = _format_perm_request(tool_name, tool_input)
                # Send message BEFORE deleting file / setting pending
                await app.bot.send_message(chat_id=_chat_id, text=msg)
                # Only now mark as pending and remove the file
                try:
                    os.remove(req_path)
                except FileNotFoundError:
                    pass
                _perm_pending = True
        except Exception as e:
            # Surface watcher errors instead of silent failure
            try:
                await app.bot.send_message(chat_id=_chat_id, text=f"[watcher error] {e}")
            except Exception:
                pass
        await asyncio.sleep(0.5)


def _start_perm_watcher():
    global _perm_watch_task
    if _perm_watch_task is not None and not _perm_watch_task.done():
        return
    _perm_watch_task = asyncio.ensure_future(_perm_watcher())


def _stop_perm_watcher():
    global _perm_watch_task
    if _perm_watch_task is not None:
        _perm_watch_task.cancel()
        _perm_watch_task = None


# --- PTY lifecycle ---
def start_session():
    global session
    if session is not None and session.alive:
        session.stop_reading()
        session.kill()
    session = PTYSession()
    session.spawn(cwd=START_DIR)


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
    if _claude_runner.active:
        await _claude_runner.cancel()
    _claude_mode = False
    _perm_pending = False
    _stop_perm_watcher()
    await update.message.reply_text("Back to terminal mode.")


async def cmd_claude_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset Claude conversation (fresh session)."""
    if not authorized(update):
        return
    if _claude_runner.active:
        await _claude_runner.cancel()
    _claude_runner.reset()
    await update.message.reply_text("Claude conversation reset. Next message starts fresh.")


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

    # If a permission is pending, deny it first since the new mode applies next prompt
    if _perm_pending:
        await _deny_perm_and_wait()
        if _claude_runner.active:
            await _claude_runner.cancel()

    _claude_perm_mode = new_mode
    await update.message.reply_text(f"Claude permission mode set to: {_claude_perm_mode}")


# --- Text message handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    global _chat_id, _claude_mode, _perm_pending
    _chat_id = update.effective_chat.id

    text = update.message.text

    # --- Claude trigger: "claude" or "claude <prompt>" ---
    if text == "claude" or text.startswith("claude "):
        _claude_mode = True
        _start_perm_watcher()
        prompt = text[len("claude"):].strip()
        if not prompt:
            await update.message.reply_text("Claude mode. Send a message or /terminal to exit.")
            return
        cwd = _get_pty_cwd()
        await _claude_runner.run(prompt, cwd, send_claude_output, _claude_perm_mode)
        return

    # --- Claude mode handling ---
    if _claude_mode:
        _start_perm_watcher()  # no-op if already running
        # ^C cancels active Claude process
        if text == "^C":
            if _perm_pending:
                await _deny_perm_and_wait()
            if _claude_runner.active:
                await _claude_runner.cancel()
                await update.message.reply_text("Claude cancelled.")
            return

        # Permission response (only when a permission is pending)
        if _perm_pending and text.lower() in ("y", "yes"):
            resp_path = PERM_RESP_PATTERN.format(session_id=_telebot_session_id)
            try:
                _atomic_write_json(resp_path, {"decision": "allow"})
            except Exception as e:
                await update.message.reply_text(f"Error writing permission: {e}")
                return
            _perm_pending = False
            await update.message.reply_text("Allowed.")
            return

        if _perm_pending and text.lower() in ("n", "no"):
            resp_path = PERM_RESP_PATTERN.format(session_id=_telebot_session_id)
            try:
                _atomic_write_json(resp_path, {"decision": "deny"})
            except Exception as e:
                await update.message.reply_text(f"Error writing permission: {e}")
                return
            _perm_pending = False
            await update.message.reply_text("Denied.")
            return

        # Any other message while permission pending — auto-deny and forward
        if _perm_pending:
            await _deny_perm_and_wait()
            if _claude_runner.active:
                await _claude_runner.cancel()
            cwd = _get_pty_cwd()
            await _claude_runner.run(text, cwd, send_claude_output, _claude_perm_mode)
            return

        # Claude is busy — reject new input
        if _claude_runner.active:
            await update.message.reply_text("Claude is working... ^C to cancel.")
            return

        # Follow-up prompt (--continue)
        cwd = _get_pty_cwd()
        await _claude_runner.run(text, cwd, send_claude_output, _claude_perm_mode)
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


# --- Catch-all for unrecognized /commands ---
async def handle_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward unrecognized slash commands to Claude when in Claude mode."""
    if not authorized(update):
        return
    global _chat_id, _perm_pending
    _chat_id = update.effective_chat.id

    if _claude_mode:
        _start_perm_watcher()  # no-op if already running
        text = update.message.text  # e.g. "/research_codebase the auth module"
        if _perm_pending:
            await _deny_perm_and_wait()
            if _claude_runner.active:
                await _claude_runner.cancel()
        elif _claude_runner.active:
            await update.message.reply_text("Claude is working... ^C to cancel.")
            return
        cwd = _get_pty_cwd()
        await _claude_runner.run(text, cwd, send_claude_output, _claude_perm_mode)
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
            text="Terminal bot started. Send /start for help.",
        )
        _chat_id = AUTHORIZED_USER_ID
    except Exception:
        pass


async def shutdown(application: Application):
    """Cleanup on shutdown."""
    _stop_perm_watcher()
    if _claude_runner.active:
        await _claude_runner.cancel()
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

    # Run with polling (no webhooks, no open ports)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
