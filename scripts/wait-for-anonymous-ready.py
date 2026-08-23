#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def _get_json(url: str, *, timeout: float) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            body = {"error": f"HTTP {exc.code}"}
        return exc.code, body


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Wait for the lightweight WebGPT liveness endpoint, then perform exactly one "
            "potentially expensive Free-anonymous readiness probe."
        )
    )
    parser.add_argument("port", type=int)
    parser.add_argument("--liveness-attempts", type=int, default=60)
    parser.add_argument("--liveness-interval", type=float, default=1.0)
    parser.add_argument("--readiness-timeout", type=float, default=90.0)
    parser.add_argument("--json-out")
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    live = False
    for _ in range(max(1, args.liveness_attempts)):
        try:
            status, body = _get_json(base + "/healthz", timeout=1.0)
        except Exception:
            status, body = 0, {}
        if status == 200 and body.get("ok") is True:
            live = True
            break
        time.sleep(max(0.0, args.liveness_interval))
    if not live:
        print("WebGPT server never became live", file=sys.stderr)
        return 1

    try:
        status, body = _get_json(base + "/readyz", timeout=args.readiness_timeout)
    except Exception as exc:
        print(f"Anonymous readiness probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json_out:
        from pathlib import Path

        target = Path(args.json_out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if status != 200 or body.get("ready") is not True or body.get("auth_status") != "anonymous":
        print(
            "Free-anonymous readiness failed: " + json.dumps(body, sort_keys=True),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(body, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
