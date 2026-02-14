import asyncio
import fcntl
import os
import pty
import re
import signal
import struct
import termios
import time

from config import PTY_COLS, PTY_ROWS, TERM

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


def _cursor_forward_replacer(m):
    n = int(m.group(1)) if m.group(1) else 1
    return ' ' * n


def clean_output(text):
    """Strip ANSI codes, replacing cursor-forward with spaces."""
    # Replace cursor-forward sequences with spaces first
    text = _CURSOR_FORWARD_RE.sub(_cursor_forward_replacer, text)
    # Strip all remaining ANSI sequences (including bare \r)
    text = _ANSI_RE.sub('', text)
    return text


class PTYSession:
    def __init__(self):
        self.pid = None
        self.master_fd = None
        self.alive = False
        self._on_output = None
        self.started_at = None
        self._output_buffer = ""
        self._flush_task = None
        self._echo_suppress = None

    def spawn(self, cwd=None):
        pid, fd = pty.fork()
        if pid == 0:
            if cwd:
                os.chdir(cwd)
            env = os.environ.copy()
            env["TERM"] = TERM
            shell = os.environ.get("SHELL", "/bin/zsh")
            os.execve(shell, [shell, "-l"], env)
        else:
            self.pid = pid
            self.master_fd = fd
            self.alive = True
            self.started_at = time.time()

            winsize = struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

            attrs = termios.tcgetattr(fd)
            attrs[3] = attrs[3] & ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSANOW, attrs)

            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def write(self, data):
        if self.alive and self.master_fd is not None:
            os.write(self.master_fd, data.encode())

    def send_line(self, line):
        self._echo_suppress = line
        self.write(line + "\n")

    def send_signal_char(self, char):
        self.write(char)

    def kill(self):
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.alive = False
        self.pid = None
        self.master_fd = None
        self.started_at = None

    async def start_reading(self, on_output):
        self._on_output = on_output
        loop = asyncio.get_event_loop()

        def _reader():
            try:
                data = os.read(self.master_fd, 16384)
                if data:
                    text = data.decode(errors="replace")
                    cleaned = clean_output(text)
                    if cleaned:
                        self._output_buffer += cleaned
                else:
                    self.alive = False
            except OSError:
                self.alive = False
            except Exception:
                pass

        loop.add_reader(self.master_fd, _reader)

        # Start periodic flush loop
        self._flush_task = asyncio.ensure_future(self._flush_loop())

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
                    # ZLE echo produces "X cmd" (e.g. "c claude", "l ls")
                    # at the start of output. Find the command text and cut
                    # everything before and including it.
                    idx = output.find(cmd)
                    if idx != -1:
                        output = output[idx + len(cmd):]
                if self._on_output and output.strip():
                    await self._on_output(output)

    def stop_reading(self):
        if self.master_fd is not None:
            try:
                asyncio.get_event_loop().remove_reader(self.master_fd)
            except (ValueError, OSError):
                pass
        if self._flush_task is not None:
            self._flush_task.cancel()

    def status(self):
        if not self.alive:
            return "No active session"
        uptime = int(time.time() - self.started_at) if self.started_at else 0
        mins, secs = divmod(uptime, 60)
        hours, mins = divmod(mins, 60)
        return (
            f"PID: {self.pid}\n"
            f"Uptime: {hours}h {mins}m {secs}s\n"
            f"Terminal: {PTY_COLS}x{PTY_ROWS}"
        )
