from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from gpt.drivers.ui import UIDriver
from gpt.profile import (
    DEFAULT_BRAVE_PROFILE_DIR,
    DEFAULT_CLOAK_PROFILE_DIR,
    DEFAULT_PROFILE_DIR,
    ensure_profile_dir,
)
from gpt.reverse.artifacts import ArtifactManager
from gpt.reverse.dom_probe import DOMProbe
from gpt.reverse.experiment import ExperimentRunner
from gpt.reverse.protocol_map import ProtocolLedger
from gpt.reverse.redact import default_redactor
from gpt.runtime_paths import (
    DEFAULT_RUNTIME_ROOT,
    assert_runtime_path,
    ensure_runtime_layout,
    free_anonymous_gateway_lock,
)
from gpt.state import WebChatError
from gpt.transport.browser import BrowserManager
from gpt.transport.session import ChatGPTWebSession
from gpt.types import ResponseCompleted, ResponseDelta, ResponseFailed
from gpt.verification import (
    ManualVerificationRecord,
    ManualVerificationStatus,
    append_manual_verification,
    load_manual_verifications,
    manual_verification_summary,
)


def _print_json(data) -> None:
    print(
        json.dumps(
            default_redactor.redact_json(data), indent=2, ensure_ascii=False
        )
    )


def _loopback_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _find_brave(executable_path: str | None) -> str:
    if executable_path:
        candidate_path = Path(executable_path).expanduser()
        if not candidate_path.is_file():
            raise FileNotFoundError(candidate_path)
        return str(candidate_path)
    for name in ("brave-browser", "brave", "/opt/brave.com/brave/brave"):
        candidate = shutil.which(name) if "/" not in name else name
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Brave executable not found; pass --executable-path.")


def _find_cloak(executable_path: str | None) -> str:
    if executable_path:
        candidate_path = Path(executable_path).expanduser()
        if candidate_path.is_file():
            return str(candidate_path)
    default_cloak = Path.home() / ".cloakbrowser" / "chromium-146.0.7680.177.5" / "chrome"
    if default_cloak.is_file():
        return str(default_cloak)
    cloak_dir = Path.home() / ".cloakbrowser"
    if cloak_dir.is_dir():
        for p in cloak_dir.glob("**/chrome"):
            if p.is_file():
                return str(p)
    cand = shutil.which("cloakbrowser") or shutil.which("chromium") or shutil.which("google-chrome")
    if cand:
        return cand
    raise FileNotFoundError("CloakBrowser binary not found. Run 'cloakbrowser install'.")


def cmd_cloak_launch(args: argparse.Namespace) -> None:
    """Launch a user-owned CloakBrowser stealth profile with loopback CDP enabled."""
    if _loopback_port_in_use(args.port):
        raise RuntimeError(
            f"CDP port {args.port} is already in use; refusing to attach to an unknown browser."
        )
    profile_dir = ensure_profile_dir(args.profile_dir)
    executable = _find_cloak(args.executable_path)
    subprocess.Popen(
        [
            executable,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={args.port}",
            f"--user-data-dir={profile_dir}",
            args.url,
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _print_json(
        {
            "ok": True,
            "browser": executable,
            "profile_dir": str(profile_dir),
            "cdp_url": f"http://127.0.0.1:{args.port}",
            "next": (
                f"gpt-web doctor --cdp-url http://127.0.0.1:{args.port} "
                f"--profile-dir {profile_dir} --browser"
            ),
        }
    )


def cmd_brave_launch(args: argparse.Namespace) -> None:
    """Launch a user-owned Brave profile with loopback CDP enabled."""
    if _loopback_port_in_use(args.port):
        raise RuntimeError(
            f"CDP port {args.port} is already in use; refusing to attach to an unknown browser."
        )
    profile_dir = ensure_profile_dir(args.profile_dir)
    executable = _find_brave(args.executable_path)
    subprocess.Popen(
        [
            executable,
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={args.port}",
            f"--user-data-dir={profile_dir}",
            args.url,
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _print_json(
        {
            "ok": True,
            "browser": executable,
            "profile_dir": str(profile_dir),
            "cdp_url": f"http://127.0.0.1:{args.port}",
            "next": (
                f"gpt-web setup --cdp-url http://127.0.0.1:{args.port} "
                f"--profile-dir {profile_dir}"
            ),
        }
    )


async def cmd_setup(args: argparse.Namespace) -> None:
    """Open the dedicated profile for manual login; never handles credentials."""
    manager = BrowserManager(
        headless=False,
        persistent=True,
        profile_dir=args.profile_dir,
        executable_path=args.executable_path,
        cdp_url=args.cdp_url,
    )
    try:
        page = await manager.new_page()
        await page.goto(args.url, wait_until="domcontentloaded", timeout=45_000)
        driver = UIDriver(page)
        print(f"Profile: {args.profile_dir}")
        if args.cdp_url:
            print(f"Attached to local CDP: {args.cdp_url}")
        print("Log in manually in Chromium. This command never reads your password or cookies.")
        deadline = time.monotonic() + args.wait_seconds
        authenticated_polls = 0
        while time.monotonic() < deadline:
            status = await driver.auth_status()
            if status == "authenticated":
                authenticated_polls += 1
                if authenticated_polls >= 3:
                    print("Authenticated profile detected and saved.")
                    return
            else:
                authenticated_polls = 0
            await asyncio.sleep(1)
        print("Login was not detected before the wait timeout.", file=sys.stderr)
        raise SystemExit(2)
    finally:
        await manager.stop()


async def cmd_login(args: argparse.Namespace) -> None:
    """Automate login with username/password and optional 2FA TOTP."""
    import os

    from gpt.auth import AutoLoginManager, LoginCredentials

    cred_str = args.cred
    if not cred_str and args.stdin:
        cred_str = sys.stdin.read().strip()

    if not cred_str and args.username and args.password:
        creds = LoginCredentials(
            username=args.username,
            password=args.password,
            totp_secret_or_code=args.two_factor
            or os.environ.get("CHATGPT_2FA_SECRET")
            or os.environ.get("CHATGPT_2FA"),
        )
    elif cred_str:
        creds = LoginCredentials.from_string(cred_str)
    else:
        username = os.environ.get("CHATGPT_USERNAME")
        password = os.environ.get("CHATGPT_PASSWORD")
        two_factor = os.environ.get("CHATGPT_2FA_SECRET") or os.environ.get("CHATGPT_2FA")
        if not username or not password:
            print(
                "Error: provide -u/--username and -p/--password, --cred 'user|pass|2fa', --stdin, or CHATGPT_USERNAME / CHATGPT_PASSWORD environment variables.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        creds = LoginCredentials(
            username=username,
            password=password,
            totp_secret_or_code=two_factor,
        )

    print(
        f"[*] Initiating automated login for: {creds.username} (2FA: {'configured' if creds.totp_secret_or_code else 'none'})..."
    )
    login_mgr = AutoLoginManager(
        profile_dir=args.profile_dir,
        headless=args.headless,
        cdp_url=args.cdp_url,
    )
    success = await login_mgr.login(creds, timeout_seconds=args.timeout)
    if success:
        print("[+] Login successful! Authenticated profile stored at:", args.profile_dir)
        _print_json(
            {"ok": True, "status": "authenticated", "profile_dir": str(args.profile_dir)}
        )
        return
    print("[-] Login failed.", file=sys.stderr)
    raise SystemExit(1)


async def cmd_probe(args: argparse.Namespace) -> None:
    manager = BrowserManager(
        headless=args.headless,
        persistent=args.persistent,
        profile_dir=args.profile_dir,
        executable_path=args.executable_path,
        cdp_url=args.cdp_url,
    )
    try:
        page = await manager.new_page()
        started = time.monotonic()
        await page.goto(args.url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(1_500)
        result = await DOMProbe(page).probe_all()
        result["navigation_time_ms"] = int((time.monotonic() - started) * 1_000)
        _print_json(result)
    finally:
        await manager.stop()


async def _create_session(args: argparse.Namespace) -> ChatGPTWebSession:
    is_free = getattr(args, "free", False)
    persistent = args.persistent and not is_free
    profile_dir = None if is_free else args.profile_dir
    return await ChatGPTWebSession.create(
        headless=args.headless,
        persistent=persistent,
        profile_dir=profile_dir,
        executable_path=args.executable_path,
        cdp_url=args.cdp_url,
        target_url=args.url,
    )


async def cmd_models(args: argparse.Namespace) -> None:
    session = await _create_session(args)
    try:
        models = await session.models()
        _print_json(
            {
                "ok": True,
                "models": [
                    {
                        "id": model.id,
                        "label": model.label,
                        "selected": model.selected,
                        "available": model.available,
                        "source": model.source,
                        "reasoning_efforts": getattr(
                            model, "reasoning_efforts", ["instant", "low", "medium", "high", "max"]
                        ),
                    }
                    for model in models
                ],
            }
        )
    finally:
        await session.close()


async def cmd_doctor(args: argparse.Namespace) -> None:
    """Report local/browser readiness without sending a prompt or opening a picker."""
    profile_dir = None if args.free else Path(args.profile_dir).expanduser()
    result: dict[str, object] = {
        "ok": True,
        "profile_dir": str(profile_dir) if profile_dir else None,
        "profile_exists": profile_dir.is_dir() if profile_dir else None,
        "profile_mode": (
            oct(profile_dir.stat().st_mode & 0o777) if profile_dir and profile_dir.exists() else None
        ),
        "ephemeral": bool(args.free),
        "cdp_url": args.cdp_url,
        "browser_checked": False,
    }
    if args.cdp_url:
        try:
            BrowserManager._validate_local_cdp_url(args.cdp_url)
            result["cdp_loopback"] = True
        except ValueError as exc:
            result["ok"] = False
            result["cdp_loopback"] = False
            result["error"] = str(exc)
    if not args.browser:
        _print_json(result)
        return
    session = await _create_session(args)
    try:
        result["browser_checked"] = True
        result["browser_connected"] = session.browser_manager.connected
        result["auth_status"] = await session.ui_driver.auth_status()
        try:
            await session.ui_driver.get_composer(timeout_ms=args.timeout_ms)
            result["composer"] = "available"
        except Exception as exc:
            result["ok"] = False
            result["composer"] = "unavailable"
            result["browser_error"] = type(exc).__name__
    finally:
        await session.close()
    _print_json(result)


async def cmd_send(args: argparse.Namespace) -> None:
    lock_context = free_anonymous_gateway_lock() if args.free else nullcontext()
    with lock_context:
        session = await _create_session(args)
        try:
            if args.conversation:
                await session.open(args.conversation)
            if args.model:
                await session.select_model(args.model)
            if getattr(args, "effort", None):
                await session.select_reasoning_effort(args.effort)
            session.drain_events()
            task = asyncio.create_task(
                session.send(
                    args.text,
                    timeout_seconds=args.timeout,
                    files=getattr(args, "files", None),
                )
            )
            if not args.json:
                async for event in session.events():
                    if isinstance(event, ResponseDelta) and not event.revision:
                        sys.stdout.write(event.text)
                        sys.stdout.flush()
                    elif isinstance(event, (ResponseCompleted, ResponseFailed)):
                        break
            try:
                result = await task
            except WebChatError as exc:
                if args.json:
                    _print_json(
                        {
                            "ok": False,
                            "session_id": session.session_id,
                            "state": session.state.value,
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc),
                            },
                        }
                    )
                    raise SystemExit(1) from None
                raise
            if args.json:
                _print_json(
                    {
                        "ok": True,
                        "session_id": session.session_id,
                        "state": session.state.value,
                        "conversation_id": result.conversation_id,
                        "turn_id": result.turn_id,
                        "model": result.model,
                        "text": result.text,
                        "duration_ms": result.duration_ms,
                    }
                )
            else:
                print()
        finally:
            await session.close()


async def cmd_experiment(args: argparse.Namespace) -> None:
    manager = BrowserManager(
        headless=args.headless,
        persistent=args.persistent,
        profile_dir=args.profile_dir,
        executable_path=args.executable_path,
        cdp_url=args.cdp_url,
    )
    ledger = ProtocolLedger()
    try:
        page = await manager.new_page()
        runner = ExperimentRunner(page, artifact_manager=ArtifactManager())
        await runner.initialize()
        await page.goto(args.url, wait_until="domcontentloaded", timeout=45_000)
        driver = UIDriver(page)
        async with runner.experiment(args.exp_id, args.variable) as experiment:
            if args.action == "idle":
                await asyncio.sleep(args.idle_seconds)
            elif args.action == "new-chat":
                await driver.new_conversation()
            elif args.action == "model-open":
                await driver.list_models()
            elif args.action == "send":
                prompt = f"{experiment.marker} - Respond with exactly 3 numbered lines."
                await driver.send(prompt, timeout_seconds=args.timeout)
        ledger.record_finding(
            name=f"experiment_{args.exp_id}",
            hypothesis=f"Completed controlled action: {args.action}",
            experiment_id=args.exp_id,
            confidence="high",
            details={"variable": args.variable, "action": args.action},
        )
        _print_json({"ok": True, "experiment_id": args.exp_id, "action": args.action})
    finally:
        await manager.stop()


async def cmd_mcp_bridge(args: argparse.Namespace) -> None:
    """Run an autonomous agent loop bridging local BQA / Burp MCP tools with ChatGPT Web."""
    from gpt.mcp_bridge import MCPBridge

    session = await _create_session(args)
    bridge = MCPBridge(
        bqa_base_url=getattr(args, "bqa_url", "http://127.0.0.1:18427/api/v1"),
        burp_mcp_url=getattr(args, "burp_url", "http://127.0.0.1:9876/"),
    )
    try:
        def on_turn(turn_idx: int, text: str, tool_calls):
            print(f"\n[Turn {turn_idx}]")
            if tool_calls:
                for call in tool_calls:
                    print(f"  -> Tool call: {call['function']['name']}({call['function']['arguments']})")
            elif text:
                print(f"  -> Assistant: {text}")

        print(f"[*] Starting MCP Bridge task: {args.task}")
        result = await bridge.run_autonomous_task(
            session=session,
            task=args.task,
            max_turns=args.max_turns,
            on_turn_callback=on_turn,
        )
        print("\n[+] Final Result:\n", result)
    finally:
        await session.close()


def cmd_manual_record(args: argparse.Namespace) -> None:
    status = ManualVerificationStatus(args.status)
    record = ManualVerificationRecord(
        feature_id=args.feature_id,
        status=status,
        expected=args.expected,
        observed=args.observed,
        verifier=args.verifier,
        environment=args.environment,
        evidence=list(args.evidence or []),
        metadata={"note": args.note} if args.note else {},
    )
    append_manual_verification(args.output, record)
    _print_json(
        {
            "ok": True,
            "feature_id": record.feature_id,
            "status": record.status.value,
            "output": str(Path(args.output).expanduser()),
        }
    )


def cmd_manual_status(args: argparse.Namespace) -> None:
    records = load_manual_verifications(args.input)
    summary = manual_verification_summary(records)
    _print_json({"ok": summary["ok"], "input": str(Path(args.input).expanduser()), **summary})
    if args.require_pass and not summary["ok"]:
        raise SystemExit(2)


def cmd_redact_test(_args: argparse.Namespace) -> None:
    sample = {
        "authorization": "Bearer sample-secret",
        "cookie": "session=sample-secret",
        "conversation_id": "12345678-1234-5678-1234-567812345678",
        "message_id": "99999999-9999-9999-9999-999999999999",
        "auth_status": "authenticated",
    }
    _print_json(default_redactor.redact_json(sample, normalize_ids=True))


def cmd_api_server(args: argparse.Namespace) -> None:
    import uvicorn

    if (
        not args.mock_backend
        and not getattr(args, "allow_authenticated", False)
        and args.max_workers != 1
    ):
        raise SystemExit("free_anonymous api-server requires --max-workers 1")
    ensure_runtime_layout()
    args.conversation_store = str(assert_runtime_path(args.conversation_store))
    if args.trace_file:
        args.trace_file = str(assert_runtime_path(args.trace_file))
    if args.prompt_debug_dir:
        args.prompt_debug_dir = str(assert_runtime_path(args.prompt_debug_dir))

    from gpt.api.model_registry import load_model_aliases
    from gpt.gateway import create_api_app

    try:
        lock = nullcontext() if args.mock_backend else free_anonymous_gateway_lock()
        with lock:
            uvicorn.run(
                create_api_app(
                    headless=args.headless,
                    persistent=args.persistent,
                    profile_dir=args.profile_dir,
                    executable_path=args.executable_path,
                    cdp_url=args.cdp_url,
                    transport=args.transport,
                    model_aliases=load_model_aliases(args.model_aliases_file),
                    conversation_store_path=args.conversation_store,
                    conversation_ttl_seconds=args.conversation_ttl_seconds,
                    force_anthropic_initial_tool=args.anthropic_force_initial_tool,
                    max_workers=args.max_workers,
                    warm_workers=args.warm_workers,
                    queue_timeout=args.queue_timeout,
                    trace_path=args.trace_file,
                    prompt_debug_dir=args.prompt_debug_dir,
                    prewarm=args.prewarm,
                    generation_timeout_seconds=args.generation_timeout,
                    require_anonymous=(
                        not args.mock_backend and not getattr(args, "allow_authenticated", False)
                    ),
                    mock_backend=args.mock_backend,
                ),
                host=args.host,
                port=args.port,
                log_level="info",
            )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


async def cmd_install_bqa_plugin(args: argparse.Namespace) -> None:
    from gpt.bqa_installer import BQAPluginInstaller

    installer = BQAPluginInstaller(cdp_url=args.cdp_url)
    res = await installer.register_plugin(bot_name=args.name, bot_description=args.description)
    detail_data = json.loads(res.detail) if res.detail.startswith("{") else res.detail
    payload = {
        "ok": res.ok,
        "gizmo_id": res.gizmo_id,
        "tunnel_url": res.tunnel_url,
        "status_code": res.status_code,
        "bot_name": res.bot_name,
        "detail": detail_data,
    }
    print(json.dumps(payload, indent=2))


def _browser_args(parser: argparse.ArgumentParser, *, persistent_default: bool = False) -> None:
    parser.add_argument("--url", default="https://chatgpt.com")
    parser.add_argument("--headful", dest="headless", action="store_false")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--persistent", action="store_true", default=persistent_default)
    parser.add_argument(
        "--free",
        "--anonymous",
        action="store_true",
        help="Use an isolated ephemeral/free session without using the Plus profile",
    )
    parser.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    parser.add_argument("--executable-path")
    parser.add_argument(
        "--cdp-url",
        help="Attach to an existing Chromium/Brave at a loopback CDP endpoint.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gpt-web", description="ChatGPT Web session and reverse-engineering toolkit"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    mcp_bridge_cmd = commands.add_parser(
        "mcp-bridge", help="Run autonomous agent loop using local BQA/Burp MCP tools"
    )
    _browser_args(mcp_bridge_cmd, persistent_default=True)
    mcp_bridge_cmd.add_argument("--task", required=True, help="Task description for the agent")
    mcp_bridge_cmd.add_argument(
        "--bqa-url", default="http://127.0.0.1:18427/api/v1", help="BQA REST API base URL"
    )
    mcp_bridge_cmd.add_argument(
        "--burp-url", default="http://127.0.0.1:9876/", help="Burp MCP SSE URL"
    )
    mcp_bridge_cmd.add_argument(
        "--max-turns", type=int, default=15, help="Maximum conversation turns"
    )

    mcp_bridge_cmd.add_argument(
        "--file", "-f", action="append", dest="files", help="Path to local file to attach to prompt"
    )

    install_bqa = commands.add_parser(
        "install-bqa-plugin",
        help="Automate BQA health check, Cloudflare tunnel, and Custom GPT Action registration.",
    )
    install_bqa.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    install_bqa.add_argument("--name", default="BQA Autonomous Security Bot")
    install_bqa.add_argument(
        "--description",
        default="Autonomous Security Bot connected to BQA Host MCP tools (noauth).",
    )

    setup = commands.add_parser("setup", help="Open the dedicated profile for manual login")
    setup.add_argument("--url", default="https://chatgpt.com")
    setup.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    setup.add_argument("--executable-path")
    setup.add_argument("--cdp-url", help="Attach to existing local Brave/Chromium CDP.")
    setup.add_argument("--wait-seconds", type=int, default=300)

    cloak_launch = commands.add_parser(
        "cloak-launch", help="Launch a dedicated CloakBrowser stealth profile with local CDP enabled"
    )
    cloak_launch.add_argument("--url", default="https://chatgpt.com")
    cloak_launch.add_argument("--profile-dir", default=str(DEFAULT_CLOAK_PROFILE_DIR))
    cloak_launch.add_argument("--executable-path")
    cloak_launch.add_argument("--port", type=_valid_port, default=9222)

    brave_launch = commands.add_parser(
        "brave-launch", help="Launch a dedicated Brave profile with local CDP enabled"
    )
    brave_launch.add_argument("--url", default="https://chatgpt.com")
    brave_launch.add_argument("--profile-dir", default=str(DEFAULT_BRAVE_PROFILE_DIR))
    brave_launch.add_argument("--executable-path")
    brave_launch.add_argument("--port", type=_valid_port, default=9222)

    probe = commands.add_parser("probe", help="Print DOM/accessibility reconnaissance JSON")
    _browser_args(probe)

    models = commands.add_parser("models", help="Discover currently visible models")
    _browser_args(models, persistent_default=True)

    doctor = commands.add_parser("doctor", help="Check local/browser readiness without sending a prompt")
    _browser_args(doctor, persistent_default=True)
    doctor.add_argument("--browser", action="store_true", help="Open browser and check auth/composer only")
    doctor.add_argument("--timeout-ms", type=int, default=5_000)

    send = commands.add_parser("send", help="Send one turn and stream its response")
    _browser_args(send, persistent_default=True)
    send.add_argument("--text", required=True)
    send.add_argument(
        "--file", "-f", action="append", dest="files", help="Path to local file to attach to prompt"
    )
    send.add_argument("--conversation", help="Open this conversation before sending")
    send.add_argument("--model", help="Exact visible model label (e.g. 'o3', 'GPT-5.5', 'GPT-5.6 Sol')")
    send.add_argument(
        "--effort",
        "--reasoning-effort",
        dest="effort",
        choices=["instant", "low", "medium", "high", "max"],
        help="Reasoning effort level ('instant', 'low', 'medium', 'high', 'max')",
    )
    send.add_argument("--timeout", type=float, default=120)
    send.add_argument("--json", action="store_true")

    experiment = commands.add_parser("experiment", help="Capture one controlled action")
    _browser_args(experiment, persistent_default=True)
    experiment.add_argument("--exp-id", required=True)
    experiment.add_argument("--variable", default="")
    experiment.add_argument(
        "--action", choices=["idle", "new-chat", "model-open", "send"], default="idle"
    )
    experiment.add_argument("--idle-seconds", type=float, default=5)
    experiment.add_argument("--timeout", type=float, default=120)

    # login: automated credential/TOTP login into a persistent browser profile.
    login_cmd = commands.add_parser(
        "login",
        help="Automate login using username, password, and optional 2FA TOTP",
    )
    _browser_args(login_cmd, persistent_default=True)
    login_cmd.add_argument("-u", "--username", help="ChatGPT account username / email")
    login_cmd.add_argument("-p", "--password", help="ChatGPT account password")
    login_cmd.add_argument(
        "-2fa",
        "--two-factor",
        dest="two_factor",
        help="2FA TOTP secret or current numeric code",
    )
    login_cmd.add_argument(
        "--cred",
        help="Credentials string formatted as 'username|password|2fa_secret'",
    )
    login_cmd.add_argument(
        "--stdin", action="store_true", help="Read credentials string from standard input"
    )
    login_cmd.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Seconds to wait for automated login to complete",
    )

    api = commands.add_parser("api-server", help="Run the local OpenAI-compatible gateway")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8765)
    api.add_argument("--headful", dest="headless", action="store_false")
    api.add_argument("--headless", dest="headless", action="store_true", default=True)
    api.add_argument("--persistent", action="store_true", default=True)
    api.add_argument("--ephemeral", dest="persistent", action="store_false")
    api.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    api.add_argument("--executable-path")
    api.add_argument("--cdp-url", help="Attach to existing local Brave/Chromium CDP.")
    api.add_argument(
        "--transport",
        choices=["hybrid", "browser"],
        default="browser",
        help="Generation transport: hybrid direct backend SSE.",
    )
    api.add_argument(
        "--model-aliases-file",
        help="JSON object mapping request aliases to exact visible UI model labels.",
    )
    api.add_argument(
        "--conversation-store",
        default=str(DEFAULT_RUNTIME_ROOT / "tmp" / "conversations.json"),
        help=(
            "Mode-0600 conversation/request ledger used for crash-safe retry reconciliation "
            f"(default: {DEFAULT_RUNTIME_ROOT / 'tmp' / 'conversations.json'})."
        ),
    )
    api.add_argument(
        "--conversation-ttl-seconds",
        type=float,
        default=86_400,
        help="Lifetime for records in --conversation-store (default: 86400).",
    )
    api.add_argument(
        "--anthropic-force-initial-tool",
        action="store_true",
        help="Require one tool call for the first Anthropic session turn (benchmark/debug only).",
    )
    api.add_argument(
        "--max-workers",
        type=int,
        default=1,
        help="Maximum browser page workers. Keep 1 until multi-session isolation is manually verified.",
    )
    api.add_argument(
        "--warm-workers",
        type=int,
        default=1,
        help="Number of idle page workers to keep warm.",
    )
    api.add_argument(
        "--queue-timeout",
        type=float,
        default=180.0,
        help="Maximum seconds a request may wait for worker capacity.",
    )
    api.add_argument(
        "--trace-file",
        help="Optional mode-0600 JSONL file for structural runtime boundary traces.",
    )
    api.add_argument(
        "--prompt-debug-dir",
        help=(
            "Optional mode-0700 directory for redacted pre-GPT prompt dumps. "
            "Use this to inspect the exact prompt converted from Claude Code/API requests."
        ),
    )
    api.add_argument(
        "--prewarm",
        action="store_true",
        help="Start the browser/session during server startup so the first request does not pay boot cost.",
    )
    api.add_argument(
        "--generation-timeout",
        type=float,
        default=120.0,
        help="Maximum seconds to wait for each ChatGPT Web generation before returning 504.",
    )
    api.add_argument(
        "--allow-authenticated",
        action="store_true",
        help="Allow serving requests using an authenticated Plus/Pro browser session.",
    )
    api.add_argument(
        "--mock-backend",
        "--fast-dev",
        dest="mock_backend",
        action="store_true",
        help=(
            "Run the browser-free deterministic backend for fast local Claude Code/API development."
        ),
    )

    manual = commands.add_parser(
        "manual-record",
        help="Append one mandatory manual-verification evidence record.",
    )
    manual.add_argument("--feature-id", required=True)
    manual.add_argument(
        "--status",
        choices=[item.value for item in ManualVerificationStatus],
        required=True,
    )
    manual.add_argument("--expected", required=True)
    manual.add_argument("--observed", required=True)
    manual.add_argument("--verifier", default="operator")
    manual.add_argument("--environment", default="local")
    manual.add_argument("--evidence", action="append", default=[])
    manual.add_argument("--note")
    manual.add_argument(
        "--output",
        default=str(DEFAULT_RUNTIME_ROOT / "manual-verification.jsonl"),
    )

    manual_status = commands.add_parser(
        "manual-status",
        help="Summarize mandatory manual-verification coverage.",
    )
    manual_status.add_argument(
        "--input",
        default=str(DEFAULT_RUNTIME_ROOT / "manual-verification.jsonl"),
    )
    manual_status.add_argument(
        "--require-pass",
        action="store_true",
        help="Exit non-zero unless all required manual checks are MANUAL_PASS.",
    )

    commands.add_parser("redact-test", help="Show deterministic redaction output")

    args = parser.parse_args()
    handlers = {
        "mcp-bridge": cmd_mcp_bridge,
        "setup": cmd_setup,
        "login": cmd_login,
        "probe": cmd_probe,
        "models": cmd_models,
        "doctor": cmd_doctor,
        "send": cmd_send,
        "experiment": cmd_experiment,
        "install-bqa-plugin": cmd_install_bqa_plugin,
    }
    if args.command in handlers:
        asyncio.run(handlers[args.command](args))
    elif args.command == "api-server":
        cmd_api_server(args)
    elif args.command == "cloak-launch":
        cmd_cloak_launch(args)
    elif args.command == "brave-launch":
        cmd_brave_launch(args)
    elif args.command == "manual-record":
        cmd_manual_record(args)
    elif args.command == "manual-status":
        cmd_manual_status(args)
    else:
        cmd_redact_test(args)


if __name__ == "__main__":
    main()
