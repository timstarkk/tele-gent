import asyncio
import os
import signal

from config import (
    CLAUDE_BIN,
    CLAUDE_FLUSH_INTERVAL,
    PERM_REQ_PATTERN,
    PERM_RESP_PATTERN,
)


class ClaudeRunner:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.proc = None
        self.active = False
        self.first_message = True
        self._on_output = None
        self._buffer = ""
        self._read_task = None

    async def run(self, prompt: str, cwd: str, on_output, permission_mode: str = "normal"):
        """Run claude -p with the given prompt. Uses --continue for follow-ups."""
        if self.active:
            return

        self._on_output = on_output
        self._buffer = ""
        self.active = True

        cmd = [CLAUDE_BIN, "-p"]
        if permission_mode == "auto":
            cmd.append("--dangerously-skip-permissions")
        elif permission_mode == "plan":
            cmd.extend(["--permission-mode", "plan"])
        if not self.first_message:
            # --continue resumes the last conversation in print mode
            # NOTE: If --continue doesn't carry context with -p, fall back to
            # --resume <session_id> or pass conversation history in the prompt.
            cmd.append("--continue")
        cmd.append(prompt)

        env = os.environ.copy()
        env["TELEBOT_SESSION_ID"] = self.session_id

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError:
            self.active = False
            await on_output("Claude CLI not found. Is 'claude' in PATH?")
            return

        self.first_message = False
        self._read_task = asyncio.ensure_future(self._stream_output())

    async def _stream_output(self):
        """Read stdout in chunks, flush to Telegram periodically."""
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.proc.stdout.read(4096), timeout=CLAUDE_FLUSH_INTERVAL
                    )
                except asyncio.TimeoutError:
                    # Flush interval elapsed — send buffered output
                    if self._buffer and self._on_output:
                        text = self._buffer
                        self._buffer = ""
                        await self._on_output(text)
                    continue

                if not chunk:
                    # EOF — process finished
                    break

                self._buffer += chunk.decode(errors="replace")

            # Final flush
            if self._buffer and self._on_output:
                text = self._buffer
                self._buffer = ""
                await self._on_output(text)

            # Check exit code
            await self.proc.wait()
            if self.proc.returncode != 0:
                stderr = await self.proc.stderr.read()
                err_text = stderr.decode(errors="replace").strip()
                if err_text:
                    # Show first 500 chars of stderr
                    snippet = err_text[:500]
                    await self._on_output(f"Claude error (exit {self.proc.returncode}): {snippet}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Flush anything buffered before reporting error
            if self._buffer and self._on_output:
                await self._on_output(self._buffer)
                self._buffer = ""
            if self._on_output:
                await self._on_output(f"Claude runner error: {e}")
        finally:
            self.active = False
            self.proc = None

    async def cancel(self):
        """Cancel a running Claude process. SIGINT first, SIGKILL after 3s."""
        if self.proc is None:
            return

        try:
            self.proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(self.proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass

        if self._read_task and not self._read_task.done():
            self._read_task.cancel()

        self.active = False
        self.proc = None

        # Clean up lingering permission files
        self._cleanup_perm_files()

    def reset(self):
        """Reset conversation state so next run starts a fresh session."""
        self.first_message = True

    def _cleanup_perm_files(self):
        """Remove any lingering permission request/response temp files."""
        for pattern in (PERM_REQ_PATTERN, PERM_RESP_PATTERN):
            path = pattern.format(session_id=self.session_id)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
