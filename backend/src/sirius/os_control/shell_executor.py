"""Safe, stateful asynchronous shell executor for developer and OS workflows."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ShellExecutionResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    cwd: str
    timed_out: bool = False


class StatefulShellExecutor:
    """Maintains persistent working directory and custom environment variables across commands."""

    _CWD_SENTINEL = "__SIRIUS_PWD_MARKER__"

    def __init__(
        self,
        initial_cwd: Path | None = None,
        max_output_chars: int = 16000,
    ) -> None:
        self._cwd = (initial_cwd or Path.home()).expanduser().resolve()
        self._env_overrides: dict[str, str] = {}
        self._max_output_chars = max_output_chars
        self._active_proc: asyncio.subprocess.Process | None = None

    @property
    def cwd(self) -> Path:
        return self._cwd

    def set_env(self, key: str, value: str) -> None:
        self._env_overrides[key] = value

    async def execute(
        self,
        command: str,
        timeout_seconds: float = 30.0,
    ) -> ShellExecutionResult:
        """Execute a shell command while tracking `cd` working directory transitions."""
        env = {**os.environ, **self._env_overrides}

        if sys.platform == "win32":
            wrapped = (
                f"{command}; $ec = $LASTEXITCODE; "
                f"Write-Output '{self._CWD_SENTINEL}'; (Get-Location).Path; exit $ec"
            )
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                wrapped,
                cwd=str(self._cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            wrapped = (
                f"{command}\n"
                f"__sirius_ec=$?\n"
                f"printf '\\n{self._CWD_SENTINEL}\\n%s\\n' \"$PWD\"\n"
                f"exit $__sirius_ec"
            )
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                wrapped,
                cwd=str(self._cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

        self._active_proc = proc
        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            self.abort_active()
            out_b, err_b = b"", b"Command timed out."
        finally:
            self._active_proc = None

        raw_out = out_b.decode("utf-8", errors="replace")
        raw_err = err_b.decode("utf-8", errors="replace")

        if self._CWD_SENTINEL in raw_out:
            before, after = raw_out.rsplit(self._CWD_SENTINEL, 1)
            new_pwd = after.strip().splitlines()
            if new_pwd:
                cand_str = new_pwd[-1].strip()
                resolved_cwd = await asyncio.to_thread(self._resolve_dir_sync, cand_str)
                if resolved_cwd is not None:
                    self._cwd = resolved_cwd
            clean_out = before.rstrip("\n")
        else:
            clean_out = raw_out.strip()

        return ShellExecutionResult(
            command=command,
            exit_code=int(proc.returncode if proc.returncode is not None else -1),
            stdout=clean_out[: self._max_output_chars],
            stderr=raw_err.strip()[: self._max_output_chars],
            cwd=str(self._cwd),
            timed_out=timed_out,
        )

    @staticmethod
    def _resolve_dir_sync(candidate: str) -> Path | None:
        p = Path(candidate)
        return p.resolve() if p.is_dir() else None

    def abort_active(self) -> None:
        """Immediately kill in-flight shell process group on emergency stop."""
        proc = self._active_proc
        if proc is None or proc.returncode is not None:
            return
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
