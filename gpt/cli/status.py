from __future__ import annotations

from typing import Any

from gpt.core.settings import Settings

from .common import gateway_health, human_duration, json_print


def _workers(payload: dict[str, Any]) -> str:
    workers = payload.get("workers")
    if not isinstance(workers, dict):
        return "-"
    idle = workers.get("idle", 0)
    live = workers.get("live", 0)
    maximum = workers.get("max", "-")
    return f"{idle} idle / {live} live / {maximum} max"


def status_payload(settings: Settings) -> dict[str, Any]:
    code, health = gateway_health(settings)
    return {
        "reachable": code == 200,
        "http_status": code,
        "workspace": str(settings.workspace),
        "agent": {
            "model": settings.model,
            "verify": settings.verify,
        },
        "gateway": {
            "base_url": settings.base_url,
            "transport": settings.transport,
            "configured_workers": settings.workers,
        },
        "account": settings.account,
        "health": health,
    }


def print_status(settings: Settings, *, as_json: bool = False) -> int:
    data = status_payload(settings)
    if as_json:
        json_print(data)
        return 0 if data["reachable"] else 1

    health = data.get("health") or {}
    ok = bool(data["reachable"] and health.get("ok"))
    print(f"WebGPT  {'healthy' if ok else 'unavailable'}")
    print()
    print("Agent")
    print(f"  model       {settings.model}")
    print(f"  verify      {settings.verify}")
    print()
    print("Gateway")
    print(f"  transport   {settings.transport}")
    print(f"  browser     {health.get('browser', '-')}")
    print(f"  backend     {health.get('backend', '-')}")
    print(f"  workers     {_workers(health)}")
    print(f"  sessions    {health.get('active_sessions', '-')}")
    print()
    print("Account")
    print(f"  profile     {settings.account}")
    print(f"  auth        {health.get('auth_status', '-')}")
    usage = health.get("usage")
    if isinstance(usage, dict) and "accounts" not in usage:
        print()
        print("Usage")
        percent = usage.get("last_used_percent")
        percent_text = "-" if percent is None else f"{float(percent):.1f}%"
        print(f"  primary     {percent_text}")
        print(
            "  reset       "
            + human_duration(usage.get("last_seconds_until_reset"))
        )
        print(f"  poll        {human_duration(usage.get('poll_seconds'))}")
    print()
    print("Features")
    print(f"  images      {'on' if settings.image_upload else 'off'}")
    print(f"  resume      {'on' if settings.fconv_resume else 'off'}")
    return 0 if ok else 1


__all__ = ["print_status", "status_payload"]
