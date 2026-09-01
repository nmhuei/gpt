from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the production gateway runs on POSIX hosts.
    fcntl = None  # type: ignore[assignment]

DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("WEBGPT_RUNTIME_ROOT", "~/.local/share/webgpt")
).expanduser()

RUNTIME_SUBDIRECTORIES = (
    "runs/claude",
    "runs/opencode",
    "runs/smoke",
    "benchmarks/pcap",
    "reverse",
    "captures",
    "failed-runs",
    "successful-runs",
    "tmp",
)


def runtime_path(*parts: str) -> Path:
    return DEFAULT_RUNTIME_ROOT.joinpath(*parts)


def ensure_runtime_layout(root: str | Path | None = None) -> Path:
    base = Path(root).expanduser() if root is not None else DEFAULT_RUNTIME_ROOT
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except PermissionError:
        pass
    for relative in RUNTIME_SUBDIRECTORIES:
        child = base / relative
        child.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(child, 0o700)
        except PermissionError:
            pass
    return base


def assert_runtime_path(path: str | Path, root: str | Path | None = None) -> Path:
    base = Path(root).expanduser() if root is not None else DEFAULT_RUNTIME_ROOT
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    base_resolved = base.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(
            f"Runtime artifact path must stay under {base_resolved}: {candidate_resolved}"
        ) from exc
    return candidate_resolved


@contextmanager
def free_anonymous_gateway_lock(
    root: str | Path | None = None,
) -> Iterator[Path]:
    """Hold the single global Free-anonymous gateway slot for this host.

    The execution plan requires at most one browser generation globally for the
    anonymous account mode. A process-level flock prevents separate benchmark,
    smoke, or manually started api-server processes from accidentally running
    concurrent anonymous gateways and causing quota churn or conversation
    collisions.
    """

    if fcntl is None:  # pragma: no cover
        raise RuntimeError("Free-anonymous gateway locking requires a POSIX host")
    base = ensure_runtime_layout(root)
    lock_path = base / "tmp" / "free-anonymous-gateway.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            os.chmod(lock_path, 0o600)
        except PermissionError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.lseek(fd, 0, os.SEEK_SET)
            holder = os.read(fd, 128).decode("utf-8", errors="replace").strip()
            detail = f" (holder {holder})" if holder else ""
            raise RuntimeError(
                "Another Free-anonymous WebGPT gateway is already active" + detail
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(fd)
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


__all__ = [
    "DEFAULT_RUNTIME_ROOT",
    "RUNTIME_SUBDIRECTORIES",
    "assert_runtime_path",
    "ensure_runtime_layout",
    "free_anonymous_gateway_lock",
    "runtime_path",
]
