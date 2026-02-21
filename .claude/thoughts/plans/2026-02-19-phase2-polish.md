# Phase 2: Polish — Implementation Plan

## Overview

Add three features to tele-gent that close the UX gaps identified in research: inline keyboard buttons for approvals and session selection, a task queue for handling messages while Claude is busy, and smart progress indicators that surface tool-level activity.

## Current State Analysis

- **Permission flow**: Text-based "Reply y/n" (`bot.py:415-430`). The watcher at `bot.py:454-475` sends a plain text message, and `handle_message` at `bot.py:881-892` matches `y/yes/n/no` text. Works but feels clunky on mobile.
- **Resume flow**: Text-based number selection (`bot.py:772-840`). User sends a digit 1-5, matched at `bot.py:811`.
- **Busy handling**: Any non-y/n message while permission is pending auto-denies and sends Ctrl+C (`bot.py:894-899`). This is destructive — it kills Claude's current work.
- **Progress**: Zero feedback between sending a prompt and receiving the response. The watcher only sends completed responses and permission prompts.
- **JSONL structure**: Claude writes `tool_use` and `tool_result` message types between assistant text blocks. These contain `tool_name` and `tool_input` — perfect for progress indicators.

### Key Discoveries:
- `python-telegram-bot` v22+ has full `InlineKeyboardMarkup` and `CallbackQueryHandler` support
- The `_claude_watcher` loop at `bot.py:448` polls every 1 second — this is the right place to add progress and queue processing
- JSONL messages have types: `user`, `assistant`, `tool_use`, `tool_result` — we can detect tool calls for progress
- The `_perm_pending` flag plus JSONL mtime can reliably detect "Claude is busy"

## Desired End State

1. Permission prompts arrive with inline `[Approve] [Deny]` buttons. Tapping a button immediately responds — no need to type y/n.
2. `/resume` shows sessions with inline buttons instead of requiring number input.
3. When Claude is busy, new messages are queued with acknowledgment ("Queued — 1 ahead"). They're processed sequentially when Claude finishes.
4. While Claude works, a single "working" message is sent and edited in-place with tool activity: "Reading: package.json...", "Running: npm test...", etc.

### Verification:
- Send a permission-triggering prompt → see inline buttons → tap Approve → tool executes
- Send `/resume` → see session buttons → tap one → session resumes
- Send a message while Claude processes another → see "Queued" acknowledgment → message processes after first completes
- Send a prompt → see progress message update with tool names → final response arrives

## What We're NOT Doing

- Streaming Claude's response text in real-time (too noisy, unnecessary)
- Multi-user support or concurrent Claude sessions
- Persistent queue (in-memory is fine — bot restart clears it)
- Progress bars or percentages (we don't know total work)

## Implementation Approach

Three independent features, implemented in order of dependency:
1. **Inline buttons** first — simplest, no dependencies, enables cleaner permission UX
2. **Task queue** second — builds on the button-based permission flow
3. **Progress indicators** third — needs queue awareness (don't send progress for queued items)

---

## Phase 1: Inline Keyboard Buttons

### Overview
Replace text-based y/n permission prompts and /resume number selection with Telegram inline keyboard buttons.

### Changes Required:

#### 1. Add imports
**File**: `tele_gent/bot.py`
**Changes**: Add `InlineKeyboardButton`, `InlineKeyboardMarkup`, `CallbackQuery` imports and `CallbackQueryHandler`.

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
```

#### 2. Permission prompt with inline buttons
**File**: `tele_gent/bot.py`
**Changes**: Modify the permission request sending in `_claude_watcher` (line 464) to include an inline keyboard.

In `_format_perm_request` (line 415), remove the `"\n\nReply y to allow, n to deny"` suffix — the buttons replace it.

When sending the permission message, attach an `InlineKeyboardMarkup`:

```python
keyboard = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("Approve", callback_data="perm_allow"),
        InlineKeyboardButton("Deny", callback_data="perm_deny"),
    ]
])
await app.bot.send_message(
    chat_id=_chat_id, text=msg, reply_markup=keyboard
)
```

#### 3. Callback query handler for permission buttons
**File**: `tele_gent/bot.py`
**Changes**: Add a new handler function and register it.

```python
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != AUTHORIZED_USER_ID:
        await query.answer("Unauthorized.")
        return

    data = query.data

    # Permission responses
    if data == "perm_allow" and _perm_pending:
        _write_perm_response("allow")
        global _perm_pending
        _perm_pending = False
        await query.answer("Approved")
        await query.edit_message_reply_markup(reply_markup=None)
    elif data == "perm_deny" and _perm_pending:
        _write_perm_response("deny")
        _perm_pending = False
        await query.answer("Denied")
        await query.edit_message_reply_markup(reply_markup=None)
    elif data.startswith("resume_"):
        # Resume session selection
        idx = int(data.split("_")[1])
        await query.answer(f"Resuming session {idx}...")
        await query.edit_message_reply_markup(reply_markup=None)
        await _resume_session(idx)
    else:
        await query.answer("Expired or invalid.")
```

Register in `main()` before other handlers:
```python
application.add_handler(CallbackQueryHandler(handle_callback_query))
```

#### 4. Resume with inline buttons
**File**: `tele_gent/bot.py`
**Changes**: Modify `cmd_resume` (line 772) to send inline buttons instead of text numbers.

```python
buttons = []
for i, (sid, preview, mtime) in enumerate(sessions, 1):
    ago = _format_time_ago(mtime)
    label = f"{i}. {preview[:30]} ({ago})"
    buttons.append([InlineKeyboardButton(label, callback_data=f"resume_{i}")])
keyboard = InlineKeyboardMarkup(buttons)
await update.message.reply_text("Recent sessions:", reply_markup=keyboard)
```

Extract the resume logic from `handle_message` into a reusable `_resume_session(idx)` async function that both the callback handler and the text fallback can call.

#### 5. Keep text-based y/n as fallback
**Changes**: Keep the existing text-based y/n handling in `handle_message` (lines 881-892) as a fallback. Users can still type y/n if they prefer. Similarly, keep the digit-based resume selection working alongside buttons.

### Success Criteria:

#### Automated Verification:
- [ ] Bot starts without errors: `tele-gent` (launch and check for crash)
- [ ] No import errors: `python3 -c "from tele_gent.bot import main"`

#### Manual Verification:
- [ ] Permission prompt shows Approve/Deny buttons in Telegram
- [ ] Tapping Approve allows the tool (check JSONL for tool execution)
- [ ] Tapping Deny blocks the tool
- [ ] Buttons disappear after tapping (reply_markup removed)
- [ ] Text-based y/n still works as fallback
- [ ] `/resume` shows clickable session buttons
- [ ] Tapping a resume button starts the correct session
- [ ] Expired/stale buttons show "Expired or invalid" toast

**Implementation Note**: After completing this phase and all automated verification passes, pause here for manual confirmation that buttons work correctly in Telegram before proceeding.

---

## Phase 2: Task Queue

### Overview
Add an in-memory message queue so that messages sent while Claude is busy are held and processed sequentially, instead of being auto-denied/dropped.

### Changes Required:

#### 1. Queue data structure and state
**File**: `tele_gent/bot.py`
**Changes**: Add queue globals after the existing globals section (around line 76).

```python
import collections

# Task queue for messages while Claude is busy
_message_queue = collections.deque()  # deque of (text, chat_id) tuples
_claude_busy = False  # True when Claude is actively processing (not idle at prompt)
```

#### 2. Busy detection
**File**: `tele_gent/bot.py`
**Changes**: Add logic to track whether Claude is actively working. Two signals:
- `_perm_pending` is True (waiting on approval)
- JSONL file was modified within the last 3 seconds (Claude is generating)

Add a helper:

```python
def _is_claude_busy() -> bool:
    """Check if Claude is actively processing (not idle at prompt)."""
    if _perm_pending:
        return True
    if not _last_jsonl_path:
        return False
    try:
        mtime = os.path.getmtime(_last_jsonl_path)
        return (time.time() - mtime) < 3.0
    except OSError:
        return False
```

#### 3. Queue incoming messages in Claude mode
**File**: `tele_gent/bot.py`
**Changes**: In `handle_message`, in the Claude mode section (line 868), before sending to Claude TUI:

- Check `_is_claude_busy()`
- If busy: append to `_message_queue`, send acknowledgment, return
- If not busy: send directly (existing behavior), set `_claude_busy = True`

```python
# In Claude mode, after ^C and permission handling...
if _is_claude_busy():
    _message_queue.append(text.replace("\n", " "))
    pos = len(_message_queue)
    if pos == 1:
        await update.message.reply_text("Queued. Will send when Claude is ready.")
    else:
        await update.message.reply_text(f"Queued ({pos} in queue).")
    return

# Send directly
session.send_line(text.replace("\n", " "))
```

Also queue voice transcriptions and photo captions when in Claude mode and busy.

#### 4. Process queue in the watcher
**File**: `tele_gent/bot.py`
**Changes**: In `_claude_watcher`, after extracting and sending a response (line 495-497), check if the queue has items and Claude has become idle:

```python
# After sending a response, check queue
if text and _message_queue and not _is_claude_busy():
    await asyncio.sleep(1.0)  # Brief pause for Claude to settle
    next_msg = _message_queue.popleft()
    remaining = len(_message_queue)
    notice = f"Sending queued message..."
    if remaining > 0:
        notice += f" ({remaining} still queued)"
    await app.bot.send_message(chat_id=_chat_id, text=notice)
    session.send_line(next_msg)
```

Also drain the queue when Claude exits (line 523) — discard remaining items and notify:

```python
if _message_queue:
    count = len(_message_queue)
    _message_queue.clear()
    await app.bot.send_message(
        chat_id=_chat_id,
        text=f"Claude exited. {count} queued message(s) discarded.",
    )
```

#### 5. Clear queue on /kill, /new, /terminal
**File**: `tele_gent/bot.py`
**Changes**: In `cmd_kill`, `cmd_new`, and `cmd_terminal`, clear `_message_queue`.

#### 6. Remove auto-deny behavior
**File**: `tele_gent/bot.py`
**Changes**: In `handle_message` at line 894-899, instead of auto-denying and sending Ctrl+C when a non-y/n message arrives during a pending permission, queue the message:

```python
# Old: auto-deny and Ctrl+C (destructive)
# New: queue the message
if _perm_pending:
    _message_queue.append(text.replace("\n", " "))
    await update.message.reply_text("Permission pending. Message queued for after.")
    return
```

### Success Criteria:

#### Automated Verification:
- [ ] Bot starts without errors: `tele-gent`
- [ ] No import errors: `python3 -c "from tele_gent.bot import main"`

#### Manual Verification:
- [ ] Send a prompt to Claude, then immediately send another message → see "Queued" acknowledgment
- [ ] After first response completes, queued message is automatically sent
- [ ] Queue position is reported correctly (1 in queue, 2 in queue, etc.)
- [ ] Sending a non-y/n message during permission prompt queues it instead of killing Claude
- [ ] `/terminal` clears the queue
- [ ] Claude exiting naturally reports discarded queue count (if any)
- [ ] Voice memos sent while Claude is busy are queued

**Implementation Note**: After completing this phase, pause for manual testing of queue behavior before proceeding.

---

## Phase 3: Smart Progress Indicators

### Overview
Send a single "working" message when Claude starts processing, then edit it in-place as tools are called. This gives visibility without flooding the chat.

### Design Decision
Although the user chose "tool names + snippets", combining this with editing a single message keeps the chat clean while showing detail. The progress message updates like:

```
Working...
> Reading: package.json
> Running: npm test
> Editing: src/app.py
```

### Changes Required:

#### 1. Progress state tracking
**File**: `tele_gent/bot.py`
**Changes**: Add globals for progress message tracking.

```python
_progress_message_id = None  # Telegram message ID of the progress message
_progress_lines = []  # List of progress entries
_last_tool_use_uuid = None  # Track which tool_use messages we've already reported
```

#### 2. JSONL tool_use extraction
**File**: `tele_gent/bot.py`
**Changes**: Add a function to extract recent tool_use events from JSONL.

```python
def _extract_tool_uses(jsonl_path: str, after_uuid: str | None) -> list[tuple[str, str, str]]:
    """Extract (uuid, tool_name, brief_input) from tool_use messages after after_uuid."""
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

    results = []
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        inner = msg.get("message", {})
        for block in inner.get("content", []):
            if block.get("type") == "tool_use":
                tool_name = block.get("name", "unknown")
                tool_input = block.get("input", {})
                brief = _brief_tool_input(tool_name, tool_input)
                results.append((msg.get("uuid"), tool_name, brief))
    return results
```

#### 3. Brief tool input formatter (with sanitization)
**File**: `tele_gent/bot.py`

**Security note**: Tool inputs come from Claude's JSONL, which reflects what Claude decided to do. While Claude is trusted in this context, the inputs could contain file paths or commands with special characters, Unicode, or unexpectedly long strings. Mitigations:
- Truncate all output to 60 chars max
- Strip control characters (newlines, tabs, ANSI escapes)
- Send progress messages with `parse_mode=None` (plain text) so no Markdown/HTML injection is possible
- Only extract known safe fields (`command`, `file_path`, `pattern`) — never dump raw `tool_input`

```python
def _sanitize_progress_text(text: str, max_len: int = 60) -> str:
    """Sanitize text for progress display: strip control chars, truncate."""
    # Remove newlines, tabs, and control characters
    cleaned = "".join(c if c.isprintable() else " " for c in text)
    cleaned = cleaned.strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "..."
    return cleaned

def _brief_tool_input(tool_name: str, tool_input: dict) -> str:
    """Format a brief description of a tool call for progress display."""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        return _sanitize_progress_text(cmd)
    elif tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        return _sanitize_progress_text(tool_input.get("file_path", "unknown"))
    elif tool_name == "Glob":
        return _sanitize_progress_text(tool_input.get("pattern", ""))
    elif tool_name == "Grep":
        return _sanitize_progress_text(tool_input.get("pattern", ""))
    elif tool_name in ("WebSearch", "WebFetch"):
        val = tool_input.get("query", tool_input.get("url", ""))
        return _sanitize_progress_text(val)
    elif tool_name == "Task":
        return _sanitize_progress_text(tool_input.get("description", "sub-task"))
    else:
        return tool_name
```

#### 4. Progress update in the watcher
**File**: `tele_gent/bot.py`
**Changes**: In `_claude_watcher`, add progress detection alongside the existing JSONL response extraction (after line 480). When new tool_use messages appear, update the progress message.

```python
# --- Progress indicator ---
if jsonl_path and not _perm_pending:
    tool_uses = _extract_tool_uses(jsonl_path, _last_tool_use_uuid)
    if tool_uses:
        _last_tool_use_uuid = tool_uses[-1][0]
        for _, tool_name, brief in tool_uses:
            _progress_lines.append(f"> {tool_name}: {brief}")
        # Keep only the last 5 lines
        _progress_lines = _progress_lines[-5:]
        progress_text = "Working...\n" + "\n".join(_progress_lines)
        if _progress_message_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=_chat_id,
                    message_id=_progress_message_id,
                    text=progress_text,
                    parse_mode=None,  # Plain text — no injection risk
                )
            except Exception:
                pass  # Message may be too old to edit
        else:
            msg = await app.bot.send_message(
                chat_id=_chat_id, text=progress_text, parse_mode=None,
            )
            _progress_message_id = msg.message_id
```

#### 5. Reset progress on response delivery and Claude exit
**File**: `tele_gent/bot.py`
**Changes**: When a completed response is sent (line 497) or Claude exits (line 523), clear progress state:

```python
_progress_message_id = None
_progress_lines = []
_last_tool_use_uuid = None  # Reset — will re-snapshot on next prompt
```

Also delete the progress message when the actual response arrives (cleaner chat):

```python
if _progress_message_id:
    try:
        await app.bot.delete_message(chat_id=_chat_id, message_id=_progress_message_id)
    except Exception:
        pass
    _progress_message_id = None
```

#### 6. Send initial "Working..." when a prompt is sent
**File**: `tele_gent/bot.py`
**Changes**: When a message is sent to Claude TUI (in `handle_message` Claude mode, and when queue processes), send the initial progress message:

```python
msg = await app.bot.send_message(chat_id=_chat_id, text="Working...")
_progress_message_id = msg.message_id
_progress_lines = []
```

Reset `_last_tool_use_uuid` to `_last_response_uuid` so we only track new tool calls for this prompt.

### Success Criteria:

#### Automated Verification:
- [ ] Bot starts without errors: `tele-gent`
- [ ] No import errors: `python3 -c "from tele_gent.bot import main"`

#### Manual Verification:
- [ ] Send a prompt that triggers tool use → see "Working..." appear
- [ ] Progress message updates with tool names as Claude works
- [ ] Progress message is deleted when the actual response arrives
- [ ] No progress message appears for quick responses (no tool use)
- [ ] Progress message shows at most 5 recent tool lines
- [ ] Permission prompts don't interfere with progress messages
- [ ] Queue processing shows progress for each queued item

**Implementation Note**: After completing this phase, all three Phase 2 features are complete. Do a full end-to-end test.

---

## Testing Strategy

### Integration Testing (Manual):
1. Start bot → send "claude hello" → verify progress → verify response
2. Send a multi-step prompt → watch progress update with tool names
3. While Claude works, send another message → verify queue acknowledgment
4. After first response, verify queued message auto-sends
5. Trigger a permission prompt → verify inline buttons appear
6. Tap Approve → verify tool executes
7. Send `/resume` → verify inline session buttons
8. Tap a session button → verify resume works
9. Send multiple messages rapidly while Claude is busy → verify queue ordering
10. Send `/terminal` while queue has items → verify queue clears

### Edge Cases to Test:
- Bot restart while Claude is processing (queue lost, but that's by design)
- Permission timeout (button becomes stale)
- Very long Claude response (progress deleted, response chunked correctly)
- Voice memo while Claude is busy (should queue the transcription)

## Performance Considerations

- Queue is in-memory (`deque`) — O(1) append/popleft, no persistence overhead
- Progress message editing uses Telegram's `edit_message_text` — 1 API call per tool use, not per second
- JSONL parsing for tool_use detection adds one file read per watcher tick (1/sec) — same as existing response extraction, can share the parse
- `_is_claude_busy()` does one `os.path.getmtime()` call — negligible

## References

- Research document: `/Users/tim/Documents/Obsidian/MacVault/~/ideas/research.md`
- Telegram InlineKeyboardButton docs: python-telegram-bot v22 API
- Current permission flow: `bot.py:415-475`, `hooks/telegram-permission.py`
- Current resume flow: `bot.py:772-840`
- JSONL watcher: `bot.py:437-540`
