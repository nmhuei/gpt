from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .result import ToolResult


class _ResultMixin:
    max_output_chars: int
    encoding: str

    @staticmethod
    def _decode(value: bytes | str | None, encoding: str) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return value.decode(encoding, errors="replace")

    def _bounded(self, stdout: str, stderr: str) -> tuple[str, str, bool]:
        cap = max(256, int(self.max_output_chars))
        if len(stdout) + len(stderr) <= cap:
            return stdout, stderr, False
        stderr_budget = min(len(stderr), max(128, cap // 4))
        stdout_budget = max(0, cap - stderr_budget)
        out = stdout[:stdout_budget]
        err = stderr[:stderr_budget]
        marker = "\n...[output truncated by WebGPT ProcessRunner]"
        if out:
            out += marker
        elif err:
            err += marker
        return out, err, True

    def _result(
        self,
        *,
        started: float,
        exit_code: int | None,
        stdout_b: bytes | str | None,
        stderr_b: bytes | str | None,
        status: str | None = None,
        timed_out: bool = False,
        error: str | None = None,
    ) -> ToolResult:
        stdout = self._decode(stdout_b, self.encoding)
        stderr = self._decode(stderr_b, self.encoding)
        stdout, stderr, truncated = self._bounded(stdout, stderr)
        resolved = status or ("ok" if exit_code == 0 else "error")
        return ToolResult(
            status=resolved,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
            truncated=truncated,
            error=error,
        )


@dataclass(slots=True)
class ProcessRunner(_ResultMixin):
    """Binary-safe synchronous command runner used by local agent tools."""

    default_timeout_seconds: float = 180.0
    max_output_chars: int = 12_000
    terminate_grace_seconds: float = 1.5
    encoding: str = "utf-8"

    @staticmethod
    def _terminate_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:  # pragma: no cover
                proc.terminate()
            else:  # pragma: no cover
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None,
        input_bytes: bytes | None,
        env: dict[str, str] | None,
    ) -> ToolResult:
        if not argv:
            return ToolResult(status="error", error="argv must be non-empty")
        timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if timeout <= 0:
            return ToolResult(status="error", error="timeout must be > 0")

        started = time.monotonic()
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=(os.name == "posix"),
        )
        timed_out = False
        try:
            stdout_b, stderr_b = proc.communicate(input=input_bytes, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_group(proc, signal.SIGTERM)
            try:
                stdout_b, stderr_b = proc.communicate(
                    timeout=self.terminate_grace_seconds
                )
            except subprocess.TimeoutExpired:
                self._terminate_group(proc, signal.SIGKILL)
                stdout_b, stderr_b = proc.communicate()

        if timed_out:
            return self._result(
                started=started,
                exit_code=124,
                stdout_b=stdout_b,
                stderr_b=stderr_b,
                status="timeout",
                timed_out=True,
                error=f"command timed out after {timeout:g}s",
            )
        return self._result(
            started=started,
            exit_code=int(proc.returncode or 0),
            stdout_b=stdout_b,
            stderr_b=stderr_b,
        )

    def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        input_bytes: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> ToolResult:
        return self._run_argv(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
            env=env,
        )

    def run(
        self,
        command: str,
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        input_bytes: bytes | None = None,
        env: dict[str, str] | None = None,
    ) -> ToolResult:
        if not command.strip():
            return ToolResult(status="error", error="command must be non-empty")
        return self._run_argv(
            ["/bin/bash", "-lc", command],
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            input_bytes=input_bytes,
            env=env,
        )


@dataclass(slots=True)
class AsyncProcessRunner(_ResultMixin):
    """Async argv runner with cooperative cancellation and process-tree cleanup."""

    default_timeout_seconds: float = 180.0
    max_output_chars: int = 12_000
    terminate_grace_seconds: float = 1.5
    encoding: str = "utf-8"

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:  # pragma: no cover
                proc.terminate()
            else:  # pragma: no cover
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        self._signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=max(0.01, self.terminate_grace_seconds)
            )
        except TimeoutError:
            self._signal_group(proc, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()

    async def run_argv(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ToolResult:
        if not argv:
            return ToolResult(status="error", error="argv must be non-empty")
        timeout = (
            self.default_timeout_seconds
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if timeout <= 0:
            return ToolResult(status="error", error="timeout must be > 0")

        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        communicate = asyncio.create_task(proc.communicate())
        timeout_task = asyncio.create_task(asyncio.sleep(timeout))
        stop_task = (
            asyncio.create_task(stop_event.wait()) if stop_event is not None else None
        )
        waiters: set[asyncio.Task[Any]] = {communicate, timeout_task}
        if stop_task is not None:
            waiters.add(stop_task)

        try:
            done, _ = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if communicate in done:
                stdout_b, stderr_b = communicate.result()
                return self._result(
                    started=started,
                    exit_code=int(proc.returncode or 0),
                    stdout_b=stdout_b,
                    stderr_b=stderr_b,
                )

            cancelled = bool(
                stop_task is not None
                and stop_task in done
                and stop_event is not None
                and stop_event.is_set()
            )
            timed_out = not cancelled
            await self._terminate(proc)
            stdout_b, stderr_b = await communicate
            if timed_out:
                return self._result(
                    started=started,
                    exit_code=124,
                    stdout_b=stdout_b,
                    stderr_b=stderr_b,
                    status="timeout",
                    timed_out=True,
                    error=f"command timed out after {timeout:g}s",
                )
            return self._result(
                started=started,
                exit_code=int(proc.returncode if proc.returncode is not None else -15),
                stdout_b=stdout_b,
                stderr_b=stderr_b,
                status="cancelled",
                error="command cancelled",
            )
        finally:
            for task in (timeout_task, stop_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (timeout_task, stop_task):
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            if not communicate.done():
                communicate.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await communicate


__all__ = ["AsyncProcessRunner", "ProcessRunner"]
