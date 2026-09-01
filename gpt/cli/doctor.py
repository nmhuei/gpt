from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from gpt.agent.runner import AgentRunner, AgentRunnerConfig
from gpt.auth.accounts import AccountStore
from gpt.core.paths import WebGPTPaths
from gpt.core.settings import Settings
from gpt.tools.patch import ApplyPatchTool
from gpt.tools.process import ProcessRunner

from .common import gateway_health, get_json, restart_gateway


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def _token_cache_check(settings: Settings) -> Check:
    try:
        record = AccountStore().get(settings.account)
    except (KeyError, ValueError):
        return Check("account profile", False, f"{settings.account!r} not registered")
    path = Path(record.profile_dir) / "webgpt-token-cache.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Check("token cache", False, "missing/unreadable")
    stored = raw.get("stored_at")
    if not isinstance(stored, (int, float)):
        return Check("token cache", False, "missing stored_at")
    age = max(0, int(time.time() - float(stored)))
    token = raw.get("access_token")
    return Check(
        "token cache",
        isinstance(token, str) and bool(token),
        f"age={age}s",
    )


def _local_tool_checks() -> list[Check]:
    runner = ProcessRunner(default_timeout_seconds=4)
    with tempfile.TemporaryDirectory(prefix="webgpt-doctor-") as raw:
        root = Path(raw)
        shell = runner.run("printf WEBGPT_DOCTOR_OK", cwd=root)
        (root / "a.txt").write_text("old\n", encoding="utf-8")
        patch = ApplyPatchTool(root, runner).execute(
            {
                "patch": "--- a.txt\n+++ a.txt\n@@ -1 +1 @@\n-old\n+new\n"
            }
        )
        return [
            Check(
                "shell executor",
                shell.status == "ok" and shell.stdout == "WEBGPT_DOCTOR_OK",
                f"exit={shell.exit_code}",
            ),
            Check(
                "patch executor",
                patch.status == "ok"
                and (root / "a.txt").read_text(encoding="utf-8") == "new\n",
                f"exit={patch.exit_code}",
            ),
        ]


def _deep_agent_check(settings: Settings) -> Check:
    config = AgentRunnerConfig(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        max_tokens=512,
        max_rounds=4,
        timeout_seconds=settings.timeout_seconds,
        verify="off",
        persist_session=False,
    )
    with AgentRunner(workspace=settings.workspace, config=config) as agent:
        result = agent.run(
            "Use the Bash tool to run exactly: printf WEBGPT_DOCTOR_AGENT_OK. "
            "Do not answer in prose before using the tool. After the tool succeeds, "
            "reply with exactly WEBGPT_DOCTOR_AGENT_OK."
        )
    return Check(
        "direct agent live",
        result.success and "WEBGPT_DOCTOR_AGENT_OK" in result.text,
        f"rounds={result.rounds} tools={result.tool_calls}",
    )


def collect_checks(settings: Settings, *, deep: bool = False) -> list[Check]:
    paths = WebGPTPaths.discover().ensure()
    checks = [
        Check("config", True, str(settings.user_config_file)),
        Check("state layout", paths.state_home.is_dir(), str(paths.state_home)),
    ]
    runner = ProcessRunner(default_timeout_seconds=5)
    service = runner.run(
        "systemctl --user is-active webgpt-gateway.service",
        cwd=settings.workspace,
    )
    checks.append(
        Check(
            "gateway service",
            service.status == "ok" and service.stdout.strip() == "active",
            service.stdout.strip() or service.stderr.strip(),
        )
    )
    code, health = gateway_health(settings)
    checks.append(
        Check(
            "gateway health",
            code == 200 and bool(health and health.get("ok")),
            f"http={code}",
        )
    )
    checks.append(_token_cache_check(settings))
    checks.extend(_local_tool_checks())
    if deep:
        ready_code, ready = get_json(settings.base_url.rstrip("/") + "/readyz", timeout=30)
        checks.append(
            Check(
                "browser/auth live",
                ready_code == 200 and bool(ready and ready.get("ready")),
                f"http={ready_code} auth={(ready or {}).get('auth_status', '-')}",
            )
        )
        try:
            checks.append(_deep_agent_check(settings))
        except (httpx.HTTPError, RuntimeError, ValueError) as exc:
            checks.append(Check("direct agent live", False, type(exc).__name__))
    return checks


def run_doctor(settings: Settings, *, deep: bool = False, fix: bool = False) -> int:
    checks = collect_checks(settings, deep=deep)
    if fix and not all(check.ok for check in checks):
        WebGPTPaths.discover().ensure()
        restart_gateway(settings)
        checks = collect_checks(settings, deep=deep)

    width = max(len(check.name) for check in checks)
    for check in checks:
        icon = "✓" if check.ok else "✗"
        detail = f"  {check.detail}" if check.detail else ""
        print(f"{icon} {check.name:<{width}}{detail}")
    failed = sum(not check.ok for check in checks)
    if failed:
        print(f"\n{failed} check(s) failed.")
        return 1
    print("\nAll checks passed.")
    return 0


__all__ = ["Check", "collect_checks", "run_doctor"]
