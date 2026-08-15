from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from gpt.browser import BrowserManager
from gpt.drivers.ui import UIDriver
from gpt.profile import DEFAULT_BRAVE_PROFILE_DIR, DEFAULT_PROFILE_DIR, ensure_profile_dir
from gpt.reverse.artifacts import ArtifactManager
from gpt.reverse.dom_probe import DOMProbe
from gpt.reverse.experiment import ExperimentRunner
from gpt.reverse.protocol_map import ProtocolLedger
from gpt.reverse.redact import default_redactor
from gpt.session import ChatGPTWebSession
from gpt.types import ResponseCompleted, ResponseDelta, ResponseFailed


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
        candidate = Path(executable_path).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        return str(candidate)
    for name in ("brave-browser", "brave", "/opt/brave.com/brave/brave"):
        candidate = shutil.which(name) if "/" not in name else name
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Brave executable not found; pass --executable-path.")


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
    """Automate zero-interaction login with username|password|2fa."""
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
        u = os.environ.get("CHATGPT_USERNAME")
        p = os.environ.get("CHATGPT_PASSWORD")
        t = os.environ.get("CHATGPT_2FA_SECRET") or os.environ.get("CHATGPT_2FA")
        if u and p:
            creds = LoginCredentials(username=u, password=p, totp_secret_or_code=t)
        else:
            print(
                "Error: Must provide username/password or --cred 'user|pass|2fa' or CHATGPT_USERNAME / CHATGPT_PASSWORD env vars.",
                file=sys.stderr,
            )
            raise SystemExit(1)

    print(
        f"[*] Initiating automated login for: {creds.username} (2FA: {'configured' if creds.totp_secret_or_code else 'none'})..."
    )
    login_mgr = AutoLoginManager(
        profile_dir=args.profile_dir,
        headless=args.headless,
    )
    success = await login_mgr.login(creds, timeout_seconds=args.timeout)
    if success:
        print("[+] Login successful! Authenticated profile stored at:", args.profile_dir)
        _print_json(
            {"ok": True, "status": "authenticated", "profile_dir": str(args.profile_dir)}
        )
    else:
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
    return await ChatGPTWebSession.create(
        headless=args.headless,
        persistent=args.persistent,
        profile_dir=args.profile_dir,
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
                    }
                    for model in models
                ],
            }
        )
    finally:
        await session.close()


async def cmd_send(args: argparse.Namespace) -> None:
    session = await _create_session(args)
    try:
        if args.conversation:
            await session.open(args.conversation)
        if args.model:
            await session.select_model(args.model)
        session.drain_events()
        task = asyncio.create_task(session.send(args.text, timeout_seconds=args.timeout))
        if not args.json:
            async for event in session.events():
                if isinstance(event, ResponseDelta) and not event.revision:
                    sys.stdout.write(event.text)
                    sys.stdout.flush()
                elif isinstance(event, (ResponseCompleted, ResponseFailed)):
                    break
        result = await task
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

    from gpt.api.server import create_api_app

    uvicorn.run(
        create_api_app(
            headless=args.headless,
            persistent=args.persistent,
            profile_dir=args.profile_dir,
            executable_path=args.executable_path,
            cdp_url=args.cdp_url,
        ),
        host=args.host,
        port=args.port,
        log_level="info",
    )


def _browser_args(parser: argparse.ArgumentParser, *, persistent_default: bool = False) -> None:
    parser.add_argument("--url", default="https://chatgpt.com")
    parser.add_argument("--headful", dest="headless", action="store_false")
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--persistent", action="store_true", default=persistent_default)
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

    setup = commands.add_parser("setup", help="Open the dedicated profile for manual login")
    setup.add_argument("--url", default="https://chatgpt.com")
    setup.add_argument("--profile-dir", default=str(DEFAULT_PROFILE_DIR))
    setup.add_argument("--executable-path")
    setup.add_argument("--cdp-url", help="Attach to existing local Brave/Chromium CDP.")
    setup.add_argument("--wait-seconds", type=int, default=300)

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

    send = commands.add_parser("send", help="Send one turn and stream its response")
    _browser_args(send, persistent_default=True)
    send.add_argument("--text", required=True)
    send.add_argument("--conversation", help="Open this conversation before sending")
    send.add_argument("--model", help="Exact visible model label")
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

    # login (zero-interaction automated login)
    login_cmd = commands.add_parser(
        "login", help="Automate zero-interaction login using username, password, and 2FA TOTP"
    )
    _browser_args(login_cmd, persistent_default=True)
    login_cmd.add_argument("-u", "--username", help="ChatGPT account username / email")
    login_cmd.add_argument("-p", "--password", help="ChatGPT account password")
    login_cmd.add_argument(
        "-2fa",
        "--two-factor",
        dest="two_factor",
        help="2FA TOTP secret key (e.g. JBSWY3DPEHPK3PXP) or current 6-digit code",
    )
    login_cmd.add_argument(
        "--cred",
        help="Credentials string formatted as 'username|password|2fa_secret'",
    )
    login_cmd.add_argument(
        "--stdin", action="store_true", help="Read credentials string from standard input"
    )
    login_cmd.add_argument(
        "--timeout", type=int, default=90, help="Login workflow timeout in seconds"
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

    commands.add_parser("redact-test", help="Show deterministic redaction output")

    args = parser.parse_args()
    handlers = {
        "setup": cmd_setup,
        "login": cmd_login,
        "probe": cmd_probe,
        "models": cmd_models,
        "send": cmd_send,
        "experiment": cmd_experiment,
    }
    if args.command in handlers:
        asyncio.run(handlers[args.command](args))
    elif args.command == "api-server":
        cmd_api_server(args)
    elif args.command == "brave-launch":
        cmd_brave_launch(args)
    else:
        cmd_redact_test(args)


if __name__ == "__main__":
    main()
