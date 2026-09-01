#!/usr/bin/env .venv/bin/python
"""Offline probe for the f/conversation branch (no real network).

Dry-checks that the fconv TokenManager port is present, validates the
browser profile path, and prints the live-run plan for the coordinator.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Capability surface the upstream f/conversation port must expose on
# gpt.transport.token_manager.TokenManager / module level.
REQUIRED_METHODS = (  # on TokenManager
    "bootstrap_proof_token",   # bootstrapProof equivalent
    "prepare_conduit",         # prepare handshake
)
REQUIRED_FUNCTIONS = (  # module level
    "solve_sentinel_pow",      # PoW solver
)


def check_port() -> bool:
    """Return True if every required fconv capability is importable."""
    try:
        import gpt.transport.token_manager as tm
    except Exception as exc:
        print(f"[port] token_manager import failed: {exc}", file=sys.stderr)
        return False
    missing_m = [a for a in REQUIRED_METHODS if not callable(getattr(tm.TokenManager, a, None))]
    missing_f = [a for a in REQUIRED_FUNCTIONS if not callable(getattr(tm, a, None))]
    if missing_m or missing_f:
        parts = []
        if missing_m:
            parts.append(f"TokenManager.{missing_m}")
        if missing_f:
            parts.append(f"{missing_f} (module)")
        print("[port] missing fconv capabilities: " + "; ".join(parts), file=sys.stderr)
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fconv-liveprobe",
        description="Offline probe for f/conversation TokenManager port (dry-check only).",
    )
    p.add_argument("--attempts", type=int, default=3, metavar="N",
                   help="number of PoW/bootstrap attempts to plan (default: 3)")
    p.add_argument("--timeout", type=float, default=30.0, metavar="SEC",
                   help="per-attempt timeout in seconds to plan (default: 30)")
    p.add_argument("--profile", default="/nonexistent", metavar="DIR",
                   help="browser profile dir to dry-check (default: /nonexistent)")
    return p


def print_plan(args: argparse.Namespace) -> None:
    """Steps the coordinator will run when the live probe is enabled."""
    steps = [
        f"bootstrap_proof_token(user_agent, device_id) x{args.attempts} "
        f"attempts, timeout {args.timeout}s each",
        "solve_sentinel_pow(seed) on the returned challenge",
        "prepare_conduit() handshake -> requirements/pow envelope",
        "exchange conduit token for sentinel tokens (fconv)",
        "verify token refresh within refresh_interval (no re-login)",
    ]
    print("[plan] live steps once coordinator enables network:")
    for i, s in enumerate(steps, 1):
        print(f"  {i}. {s}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not check_port():
        print("PORT CHƯA MERGE")
        return 2
    print("[ok] fconv port present: " + ", ".join(
        [f"TokenManager.{m}" for m in REQUIRED_METHODS]
        + [f"{f}()" for f in REQUIRED_FUNCTIONS]))

    # Dry-check the browser profile path (offline; no login attempted).
    profile = Path(args.profile)
    if not profile.is_dir():
        print(f"FAIL: profile dir does not exist: {profile} "
              "(pass --profile with a real CloakBrowser profile dir)")
        print_plan(args)
        return 1

    print(f"[ok] profile dir present: {profile}")
    print_plan(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
