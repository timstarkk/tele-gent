import asyncio
import os
import re
import shutil
import subprocess
import time

from tele_gent.config import PTY_COLS, PTY_ROWS, TMUX_SESSION_NAME, TMUX_PIPE_FILE

# Resolve tmux binary path at import time
_TMUX_BIN = shutil.which("tmux") or "/opt/homebrew/bin/tmux"

# ANSI stripping: replace cursor-forward with spaces, strip everything else
_CURSOR_FORWARD_RE = re.compile(r'\x1b\[(\d*)C')
_ANSI_RE = re.compile(
    r'('
    r'\x1b\[[0-9;?<>=! "\']*[@-~]'   # CSI: covers SGR mouse, kitty keyboard, all private modes
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC
    r'|\x1bP[^\x1b]*\x1b\\'          # DCS
    r'|\x1b[^[\]P]'                   # Two-char ESC sequences
    r'|\x9b[0-9;?<>=! ]*[@-~]'       # 8-bit CSI
    r'|\r(?!\n)'                      # Bare CR
    r'|\x07'                          # Bell
    r'|\x00'                          # Null
    r')'
)

# Max pipe file size before truncation (1 MB)
_PIPE_FILE_MAX = 1_000_000


def _cursor_forward_replacer(m):
    n = int(m.group(1)) if m.group(1) else 1
    return ' ' * n


def clean_output(text):
    """Strip ANSI codes, replacing cursor-forward with spaces."""
    text = _CURSOR_FORWARD_RE.sub(_cursor_forward_replacer, text)
    text = _ANSI_RE.sub('', text)
    return text


def _tmux(*args, check=True, capture=False):
    """Run a tmux command. Raises FileNotFoundError if tmux is missing."""
    cmd = [_TMUX_BIN] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True if capture else False,
        stdout=None if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result


class PTYSession:
    def __init__(self):
        self.alive = False
        self._on_output = None
        self.started_at = None
        self._output_buffer = ""
        self._flush_task = None
        self._tail_task = None
        self._echo_suppress = None
        self._file_pos = 0
        self.suppress_output = False

    def spawn(self, cwd=None, env=None):
        cwd = cwd or os.path.expanduser("~")

        # Kill stale session if it exists
        _tmux("kill-session", "-t", TMUX_SESSION_NAME, check=False)

        # Truncate / create pipe file
        with open(TMUX_PIPE_FILE, "w"):
            pass
        self._file_pos = 0

        cmd = [
            "new-session", "-d",
            "-s", TMUX_SESSION_NAME,
            "-x", str(PTY_COLS),
            "-y", str(PTY_ROWS),
            "-c", cwd,
        ]
        if env:
            for key, value in env.items():
                cmd.extend(["-e", f"{key}={value}"])

        try:
            _tmux(*cmd)
        except FileNotFoundError:
            raise RuntimeError(
                "tmux is not installed. Install it with: brew install tmux"
            )

        # Enable mouse scrolling and increase scrollback buffer
        _tmux("set-option", "-t", TMUX_SESSION_NAME, "mouse", "on", check=False)
        _tmux("set-option", "-t", TMUX_SESSION_NAME, "history-limit", "50000", check=False)

        # Pipe pane output to file
        _tmux(
            "pipe-pane", "-t", TMUX_SESSION_NAME,
            f"cat >> {TMUX_PIPE_FILE}",
        )

        self.alive = True
        self.started_at = time.time()

    def write(self, data):
        if not self.alive:
            return
        # send-keys -l sends literal text (no key name interpretation)
        # Use -- to prevent tmux from interpreting text as flags
        _tmux("send-keys", "-t", TMUX_SESSION_NAME, "-l", "--", data, check=False)

    def send_line(self, line):
        if not self.alive:
            return
        self._echo_suppress = line
        # Send the text literally, then press Enter separately
        _tmux("send-keys", "-t", TMUX_SESSION_NAME, "-l", "--", line, check=False)
        _tmux("send-keys", "-t", TMUX_SESSION_NAME, "Enter", check=False)

    def send_signal_char(self, char):
        if not self.alive:
            return
        key_map = {
            "\x03": "C-c",
            "\x04": "C-d",
            "\x1a": "C-z",
            "\x1b": "Escape",
        }
        tmux_key = key_map.get(char)
        if tmux_key:
            _tmux("send-keys", "-t", TMUX_SESSION_NAME, tmux_key, check=False)
        else:
            _tmux("send-keys", "-t", TMUX_SESSION_NAME, "-l", "--", char, check=False)

    def get_cwd(self):
        """Get the current working directory from tmux."""
        try:
            result = _tmux(
                "display-message", "-t", TMUX_SESSION_NAME,
                "-p", "#{pane_current_path}",
                capture=True,
            )
            path = result.stdout.strip()
            if path:
                return path
        except Exception:
            pass
        return os.path.expanduser("~")

    def get_foreground_command(self):
        """Get the foreground command running in the tmux pane."""
        try:
            result = _tmux(
                "display-message", "-t", TMUX_SESSION_NAME,
                "-p", "#{pane_current_command}",
                capture=True,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def kill(self):
        _tmux("kill-session", "-t", TMUX_SESSION_NAME, check=False)
        self.alive = False
        self.started_at = None
        # Clean up pipe file
        try:
            os.remove(TMUX_PIPE_FILE)
        except FileNotFoundError:
            pass

    def _has_session(self):
        """Check if the tmux session still exists."""
        result = _tmux("has-session", "-t", TMUX_SESSION_NAME, check=False)
        return result.returncode == 0

    async def start_reading(self, on_output):
        self._on_output = on_output
        self._tail_task = asyncio.ensure_future(self._tail_loop())
        self._flush_task = asyncio.ensure_future(self._flush_loop())

    async def _tail_loop(self):
        """Tail the pipe file for new output."""
        while self.alive:
            await asyncio.sleep(0.2)
            try:
                # Check if session is still alive
                if not self._has_session():
                    self.alive = False
                    break

                size = os.path.getsize(TMUX_PIPE_FILE)
                if size < self._file_pos:
                    # File was truncated externally
                    self._file_pos = 0

                if size > self._file_pos:
                    with open(TMUX_PIPE_FILE, "rb") as f:
                        f.seek(self._file_pos)
                        data = f.read()
                    self._file_pos += len(data)
                    text = data.decode(errors="replace")
                    cleaned = clean_output(text)
                    if cleaned:
                        self._output_buffer += cleaned

                # Rotate pipe file if too large
                if size > _PIPE_FILE_MAX:
                    with open(TMUX_PIPE_FILE, "w"):
                        pass
                    self._file_pos = 0

            except FileNotFoundError:
                pass
            except Exception:
                pass

    async def _flush_loop(self):
        """Flush buffered output every second."""
        while self.alive:
            await asyncio.sleep(1.0)
            if self._output_buffer:
                output = self._output_buffer
                self._output_buffer = ""
                # Strip echo of the last sent command
                if self._echo_suppress:
                    cmd = self._echo_suppress
                    self._echo_suppress = None
                    idx = output.find(cmd)
                    if idx != -1:
                        output = output[idx + len(cmd):]
                if self._on_output and output.strip() and not self.suppress_output:
                    await self._on_output(output)

    def stop_reading(self):
        if self._tail_task is not None:
            self._tail_task.cancel()
            self._tail_task = None
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

    def status(self):
        if not self.alive:
            return "No active session"
        uptime = int(time.time() - self.started_at) if self.started_at else 0
        mins, secs = divmod(uptime, 60)
        hours, mins = divmod(mins, 60)
        return (
            f"Session: {TMUX_SESSION_NAME}\n"
            f"Uptime: {hours}h {mins}m {secs}s\n"
            f"Terminal: {PTY_COLS}x{PTY_ROWS}\n"
            f"Attach: tmux attach -t {TMUX_SESSION_NAME}"
        )
