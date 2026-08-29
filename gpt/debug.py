from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from gpt.drivers.ui import UIDriver
from gpt.profile import (
    DEFAULT_BRAVE_PROFILE_DIR,
    DEFAULT_CLOAK_PROFILE_DIR,
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


def _configured_profile_dir() -> str:
    """Resolve the default persistent profile from .env/config."""
    from gpt.config.settings import load_config

    return str(load_config().profile_dir)




def _read_credential_line() -> str:
    """Read exactly one credential line so interactive stdin returns on Enter."""
    line = sys.stdin.readline()
    if not line:
        return ""
    return line.strip()

def _login_credentials_from_args(args):
    """Resolve login credentials from CLI args, .env config, or legacy env names."""
    import os

    from gpt.auth import LoginCredentials
    from gpt.config.settings import load_config

    cred_str = args.cred
    if not cred_str and args.stdin:
        cred_str = _read_credential_line()
    if cred_str:
        return LoginCredentials.from_string(cred_str)

    config = load_config()
    username = args.username or config.email or os.environ.get("CHATGPT_USERNAME")
    password = args.password or config.password
    two_factor = (
        args.two_factor
        or config.totp_key
        or os.environ.get("CHATGPT_2FA_SECRET")
        or os.environ.get("CHATGPT_2FA")
    )
    if not username or not password:
        print(
            "Error: provide -u/--username and -p/--password, --cred 'user|pass|2fa', "
            "--stdin, or CHATGPT_EMAIL / CHATGPT_PASSWORD in .env.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return LoginCredentials(
        username=username,
        password=password,
        totp_secret_or_code=two_factor,
    )


def _account_store():
    from gpt.auth import AccountStore

    return AccountStore()


def _resolve_browser_identity(args) -> tuple[bool, str | None, str | None]:
    """Return (anonymous, profile_dir, account_name) for browser-backed commands."""
    account_name = getattr(args, "account", None)
    if account_name:
        try:
            record = _account_store().get(account_name)
        except KeyError as exc:
            raise SystemExit(str(exc)) from None
        return False, record.profile_dir, record.name
    if bool(getattr(args, "free", False)):
        return True, None, None
    profile_dir = getattr(args, "profile_dir", None)
    if profile_dir:
        return False, str(Path(profile_dir).expanduser()), None
    return True, None, None


def _maybe_set_first_default(store, name: str) -> None:
    """After a successful login, adopt the account as default if none exists."""
    try:
        if store.get_default() is None:
            store.set_default(name)
            print(f"Default account set: {name}")
    except (KeyError, ValueError, OSError):
        pass


async def cmd_account_login(args: argparse.Namespace) -> None:
    from gpt.auth import AutoLoginManager, LoginError
    from gpt.auth.accounts import find_cloak_executable, manual_cloak_login

    store = _account_store()
    record = store.ensure(args.name)
    if not args.auto:
        print(f"Opening CloakBrowser for account profile: {record.name}")
        print(f"Profile: {record.profile_dir}")
        try:
            saved = store.load_credentials(record.name)
        except (FileNotFoundError, ValueError):
            saved = None
        if saved is not None:
            print(f"Login account: {saved.username}")
            print("Paste the account above into the ChatGPT login form.")
        ok = await manual_cloak_login(
            record.profile_dir, url=args.url, wait_seconds=args.wait_seconds
        )
        store.update_status(record.name, "authenticated" if ok else "login_required")
        if not ok:
            print("Login was not detected before timeout.", file=sys.stderr)
            raise SystemExit(2)
        print("Authenticated profile detected and saved.")
        _maybe_set_first_default(store, record.name)
        return

    credentials = None
    if args.cred or args.stdin:
        credentials = _login_credentials_from_args(args)
    elif args.use_saved:
        try:
            credentials = store.load_credentials(record.name)
        except (FileNotFoundError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from None
    else:
        try:
            credentials = store.load_credentials(record.name)
        except (FileNotFoundError, ValueError):
            credentials = _login_credentials_from_args(args)
    if args.save_credentials:
        store.save_credentials(record.name, credentials)
    manager = AutoLoginManager(
        profile_dir=record.profile_dir,
        headless=not args.headful_auto,
        executable_path=str(find_cloak_executable()),
    )
    try:
        # Same total-deadline pattern as cmd_login: the in-flow waits
        # (navigation/email/password/MFA) each carry their own inner budget,
        # so the outer cap needs grace on top of --timeout or slow-but-valid
        # logins get cancelled early.
        deadline_seconds = _login_flow_deadline_seconds(args.timeout)
        ok = await asyncio.wait_for(
            manager.login(credentials, timeout_seconds=args.timeout),
            timeout=deadline_seconds,
        )
    except TimeoutError:
        store.update_status(record.name, "login_required")
        print(
            f"Auto login aborted: exceeded total deadline of {deadline_seconds:.0f}s "
            "(tune WEBGPT_LOGIN_DEADLINE_SECONDS).",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except LoginError as exc:
        store.update_status(record.name, "login_required")
        print(f"Auto login blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    store.update_status(record.name, "authenticated" if ok else "login_required")
    if not ok:
        raise SystemExit(1)
    print(f"Authenticated profile saved: {record.name}")
    _maybe_set_first_default(store, record.name)


def cmd_account_default(args: argparse.Namespace) -> None:
    store = _account_store()
    if args.clear:
        store.clear_default()
        print("Default account cleared.")
        return
    if args.show or not args.name:
        _print_json({"default": store.get_default()})
        return
    try:
        store.set_default(args.name)
    except KeyError as exc:
        raise SystemExit(str(exc)) from None
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Default account set: {args.name}")


def cmd_account_list(_args: argparse.Namespace) -> None:
    rows = []
    for record in _account_store().list():
        rows.append(
            {
                "name": record.name,
                "profile_dir": record.profile_dir,
                "auth_status": record.auth_status,
                "auto_login_saved": bool(record.credentials_file),
            }
        )
    _print_json({"accounts": rows, "count": len(rows)})


async def cmd_account_status(args: argparse.Namespace) -> None:
    from gpt.auth.accounts import browser_session_authenticated

    try:
        record = _account_store().get(args.name)
    except KeyError as exc:
        raise SystemExit(str(exc)) from None
    payload: dict[str, object] = {
        "name": record.name,
        "profile_dir": record.profile_dir,
        "profile_exists": Path(record.profile_dir).is_dir(),
        "auto_login_saved": bool(record.credentials_file),
        "auth_status": record.auth_status,
        "live_checked": False,
    }
    if args.live:
        manager = BrowserManager(
            headless=True, persistent=True, profile_dir=record.profile_dir
        )
        try:
            page = await manager.new_page()
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=45_000)
            authenticated = await browser_session_authenticated(page)
            payload["live_checked"] = True
            payload["authenticated"] = authenticated
            payload["auth_status"] = "authenticated" if authenticated else "login_required"
            _account_store().update_status(record.name, str(payload["auth_status"]))
        finally:
            await manager.stop()
    _print_json(payload)


def cmd_account_credentials_set(args: argparse.Namespace) -> None:
    from gpt.auth import LoginCredentials

    raw = args.cred
    if not raw and args.stdin:
        raw = _read_credential_line()
    if not raw:
        raise SystemExit("Provide --cred 'user|pass|2fa' or --stdin.")
    try:
        credentials = LoginCredentials.from_string(raw)
        _account_store().save_credentials(args.name, credentials)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Saved credentials for account: {args.name}")


def cmd_account_credentials_delete(args: argparse.Namespace) -> None:
    try:
        _account_store().delete_credentials(args.name)
    except KeyError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Saved credentials removed for account: {args.name}")


def cmd_account_remove(args: argparse.Namespace) -> None:
    try:
        _account_store().remove(args.name, delete_profile=args.delete_profile)
    except KeyError as exc:
        raise SystemExit(str(exc)) from None
    print(f"Account registry entry removed: {args.name}")


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


# Extra budget on top of --timeout for the pre-landing phases of the login
# flow (navigation, email, password, MFA waits each carry their own inner
# deadline; worst case sums to roughly timeout + ~140s).
LOGIN_FLOW_GRACE_SECONDS = 180.0


def _login_flow_deadline_seconds(timeout_seconds: int) -> float:
    """Total wall-clock budget for one whole ``cmd_login`` run.

    Mirrors the env-deadline pattern used elsewhere in the repo
    (e.g. ``WEBGPT_INSTANCE_LIVE_DEADLINE``): invalid or non-positive values
    fall back to the computed default instead of disabling the bound.
    """
    default_total = max(float(timeout_seconds), 1.0) + LOGIN_FLOW_GRACE_SECONDS
    raw = os.environ.get("WEBGPT_LOGIN_DEADLINE_SECONDS")
    if not raw:
        return default_total
    try:
        value = float(raw)
    except ValueError:
        return default_total
    return value if value > 0 else default_total


async def cmd_login(args: argparse.Namespace) -> None:
    """Automate login with username/password and optional 2FA TOTP."""
    from gpt.auth import AutoLoginManager, LoginError
    from gpt.config.settings import load_config

    creds = _login_credentials_from_args(args)
    config = load_config()
    profile_dir = args.profile_dir or config.profile_dir
    deadline_seconds = _login_flow_deadline_seconds(args.timeout)

    print(
        f"[*] Initiating automated login (2FA: {'configured' if creds.totp_secret_or_code else 'none'})..."
    )
    print(f"[*] Browser mode: {'headless' if args.headless else 'headful'}; profile: {profile_dir}")
    print(f"[*] Total login deadline: {deadline_seconds:.0f}s")
    login_mgr = AutoLoginManager(
        profile_dir=profile_dir,
        headless=args.headless,
        cdp_url=args.cdp_url,
    )
    try:
        # Outer hard cap: every loop inside AutoLoginManager.login has its own
        # deadline, but a hung browser call could otherwise stall forever.
        success = await asyncio.wait_for(
            login_mgr.login(creds, timeout_seconds=args.timeout),
            timeout=deadline_seconds,
        )
    except LoginError as exc:
        print(f"[-] Login blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except asyncio.TimeoutError:
        print(
            f"[-] Login aborted: exceeded total deadline of {deadline_seconds:.0f}s "
            "(tune WEBGPT_LOGIN_DEADLINE_SECONDS).",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    if success:
        print("[+] Login successful! Authenticated profile stored at:", profile_dir)
        _print_json(
            {"ok": True, "status": "authenticated", "profile_dir": str(profile_dir)}
        )
        return
    print("[-] Login failed.", file=sys.stderr)
    raise SystemExit(1)


async def cmd_probe(args: argparse.Namespace) -> None:
    is_anonymous, profile_dir, _account_name = _resolve_browser_identity(args)
    manager = BrowserManager(
        headless=args.headless,
        persistent=args.persistent and not is_anonymous,
        profile_dir=profile_dir,
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
    is_anonymous, profile_dir, _account_name = _resolve_browser_identity(args)
    persistent = args.persistent and not is_anonymous
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
    is_anonymous, resolved_profile, account_name = _resolve_browser_identity(args)
    profile_dir = Path(resolved_profile).expanduser() if resolved_profile else None
    result: dict[str, object] = {
        "ok": True,
        "profile_dir": str(profile_dir) if profile_dir else None,
        "profile_exists": profile_dir.is_dir() if profile_dir else None,
        "profile_mode": (
            oct(profile_dir.stat().st_mode & 0o777) if profile_dir and profile_dir.exists() else None
        ),
        "ephemeral": is_anonymous,
        "account": account_name,
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
    is_anonymous, _profile_dir, _account_name = _resolve_browser_identity(args)
    lock_context = free_anonymous_gateway_lock() if is_anonymous else nullcontext()
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


async def cmd_sandbox_exec(args: argparse.Namespace) -> None:
    """Execute Python code in the ChatGPT Web Sandbox (Code Interpreter) and output results."""
    code = getattr(args, "code", None)
    if getattr(args, "file", None):
        code = Path(args.file).read_text(encoding="utf-8")
    if not code or not code.strip():
        print("[-] Error: No Python code provided. Use --code '...' or --file path/to/script.py", file=sys.stderr)
        raise SystemExit(1)

    prompt = (
        "Hãy thực thi chính xác đoạn mã Python sau đây trong môi trường Python Sandbox (Code Interpreter). "
        "Sau khi thực thi, hãy in ra toàn bộ kết quả stdout/stderr thực tế từ sandbox:\n\n"
        f"```python\n{code.strip()}\n```"
    )
    args.text = prompt
    await cmd_send(args)



async def cmd_experiment(args: argparse.Namespace) -> None:
    is_anonymous, profile_dir, _account_name = _resolve_browser_identity(args)
    manager = BrowserManager(
        headless=args.headless,
        persistent=args.persistent and not is_anonymous,
        profile_dir=profile_dir,
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


def cmd_env_export(args: argparse.Namespace) -> None:
    """Print export lines so one shell can scope Claude Code to the local gateway.

    Usage: ``eval "$(gpt-web env)"`` — values already present in the
    environment win, so other terminals and sessions stay untouched.
    """
    from gpt.orchestrator.session_runner import (
        DEFAULT_ANTHROPIC_API_KEY,
        DEFAULT_ANTHROPIC_BASE_URL,
        DEFAULT_CLAUDE_CONTEXT_TOKENS,
        DEFAULT_CLAUDE_MODEL,
        DEFAULT_CLAUDE_OUTPUT_TOKENS,
    )

    values = {
        "ANTHROPIC_BASE_URL": args.gateway_url or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_ANTHROPIC_BASE_URL,
        "ANTHROPIC_API_KEY": args.api_key or os.environ.get("ANTHROPIC_API_KEY") or DEFAULT_ANTHROPIC_API_KEY,
        "CLAUDE_DEFAULT_MODEL": args.model or os.environ.get("CLAUDE_DEFAULT_MODEL") or DEFAULT_CLAUDE_MODEL,
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": os.environ.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") or DEFAULT_CLAUDE_CONTEXT_TOKENS,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS") or DEFAULT_CLAUDE_OUTPUT_TOKENS,
    }
    for name, value in values.items():
        print(f"export {name}={shlex.quote(value)}")


# Disk growth guard (RAM-TOP5): prompt-debug dumps (.txt + .json per dump)
# and rotated trace segments grow without bound on long-lived gateways.
# WEBGPT_DEBUG_MAX_FILES caps how many files each debug location may keep;
# the oldest files are deleted first and every cleanup that removes something
# is logged.
DEBUG_MAX_FILES_ENV = "WEBGPT_DEBUG_MAX_FILES"
DEFAULT_DEBUG_MAX_FILES = 500
# Only these suffixes are ever pruned inside a prompt-debug directory so a
# mis-pointed directory can never nuke unrelated operator files.
_PROMPT_DEBUG_PATTERNS = ("*.txt", "*.json")


def resolve_debug_max_files(default: int = DEFAULT_DEBUG_MAX_FILES) -> int:
    """Resolve ``WEBGPT_DEBUG_MAX_FILES``; invalid values keep the default."""
    raw = os.environ.get(DEBUG_MAX_FILES_ENV, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def prune_debug_files(
    directory: Path | str | None,
    *,
    max_files: int | None = None,
    patterns: tuple[str, ...] = ("*",),
) -> int:
    """Delete the oldest files in ``directory`` until at most ``max_files`` remain.

    Counted per file (a prompt-debug dump writes a ``.txt`` plus a ``.json``
    sidecar, so one dump consumes two slots).  Best-effort: unreadable
    directories and unremovable files are skipped so cleanup never breaks a
    running gateway.  Returns the number of files removed; every run that
    deletes something logs ``debug_files_pruned``.
    """
    if directory is None or max_files is None or max_files < 0:
        return 0
    base = Path(directory).expanduser()
    try:
        candidates = [
            entry
            for entry in base.iterdir()
            if entry.is_file() and any(entry.match(pattern) for pattern in patterns)
        ]
    except OSError:
        return 0

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    removed = 0
    for entry in sorted(candidates, key=_mtime):
        if len(candidates) - removed <= max_files:
            break
        try:
            entry.unlink()
        except OSError:
            continue
        removed += 1
    if removed:
        logging.getLogger("gpt.debug").info(
            "debug_files_pruned dir=%s removed=%d remaining=%d cap=%d",
            base,
            removed,
            len(candidates) - removed,
            max_files,
        )
    return removed


def _debug_prune_loop(
    directories: list[Path], stop: threading.Event, interval_seconds: float = 300.0
) -> None:
    """Background sweeper: re-prune debug dirs while the api-server runs."""
    while not stop.wait(interval_seconds):
        max_files = resolve_debug_max_files()
        for directory in directories:
            prune_debug_files(directory, max_files=max_files)


def _start_debug_prune_loop(directories: list[Path]) -> threading.Event | None:
    """Start the periodic sweeper once; returns its stop event or None."""
    if not directories:
        return None
    stop = threading.Event()
    threading.Thread(
        target=_debug_prune_loop,
        args=(directories, stop),
        name="webgpt-debug-prune",
        daemon=True,
    ).start()
    return stop


def apply_stream_deadline_floor(server) -> float | None:
    """Raise the live-SSE deadline to the correction-loop worst case.

    The stream deadline must cover queue wait plus up to
    ``1 + WEBGPT_MAX_CORRECTIONS`` full generations; otherwise every long
    correction loop is guaranteed to hit ``GenerationTimeout`` mid-turn and its
    task cancellation poisons the leased browser worker (verify-fromscratch
    2026-08-25, ĐỨT #1/#2). An explicit ``WEBGPT_STREAM_DEADLINE_SECONDS`` keeps
    its old verbatim-override behavior -- but per the constraint documented in
    ``gpt.gateway.runtime.derived_stream_deadline_seconds`` it must then be at
    or above that worst case, or long corrections will be cut short by design.

    Returns the new deadline when it was raised, else None.
    """
    if os.environ.get("WEBGPT_STREAM_DEADLINE_SECONDS", "").strip():
        return None
    from gpt.gateway.runtime import derived_stream_deadline_seconds

    runtime = server.completion_runtime
    floor = derived_stream_deadline_seconds(
        queue_timeout=float(server.queue_timeout),
        generation_timeout=float(runtime.generation_timeout_seconds),
        max_corrections=int(runtime.max_corrections),
    )
    if floor > server.stream_deadline_seconds:
        server.stream_deadline_seconds = floor
        logging.getLogger("gpt.debug").info(
            "stream_deadline_derived %.0fs (covers correction worst case: "
            "queue_timeout + (1 + WEBGPT_MAX_CORRECTIONS=%d) * generation_timeout=%.0fs)",
            floor,
            int(runtime.max_corrections),
            float(runtime.generation_timeout_seconds),
        )
        return floor
    return None


def cmd_api_server(args: argparse.Namespace) -> None:
    import uvicorn

    account_profiles: dict[str, str] = {}
    for account_name in getattr(args, "accounts", None) or []:
        try:
            record = _account_store().get(account_name)
        except KeyError as exc:
            raise SystemExit(str(exc)) from None
        account_profiles[record.name] = record.profile_dir

    authenticated_runtime = bool(account_profiles) or bool(args.profile_dir)
    require_anonymous = not args.mock_backend and not authenticated_runtime
    if account_profiles:
        args.persistent = True
        args.profile_dir = None
    elif require_anonymous:
        args.persistent = False
        args.profile_dir = None

    if require_anonymous and args.max_workers != 1:
        raise SystemExit("free_anonymous api-server requires --max-workers 1")
    if authenticated_runtime and not account_profiles and not args.allow_authenticated:
        raise SystemExit(
            "Legacy --profile-dir requires --allow-authenticated. Prefer --account NAME."
        )
    ensure_runtime_layout()
    args.conversation_store = str(assert_runtime_path(args.conversation_store))
    if args.trace_file:
        args.trace_file = str(assert_runtime_path(args.trace_file))
    if args.prompt_debug_dir:
        args.prompt_debug_dir = str(assert_runtime_path(args.prompt_debug_dir))

    # Disk-growth guard (RAM-TOP5): prune the prompt-debug directory down to
    # WEBGPT_DEBUG_MAX_FILES at startup and keep sweeping while serving.  The
    # active trace file is bounded separately by segment rotation in
    # gpt.utils.tracing (RuntimeTraceBus._append_file).
    prompt_debug_dirs = [Path(args.prompt_debug_dir)] if args.prompt_debug_dir else []
    for debug_dir in prompt_debug_dirs:
        prune_debug_files(
            debug_dir,
            max_files=resolve_debug_max_files(),
            patterns=_PROMPT_DEBUG_PATTERNS,
        )
    _start_debug_prune_loop(prompt_debug_dirs)

    from gpt.api.model_registry import load_model_aliases
    from gpt.gateway import create_api_app

    try:
        lock = free_anonymous_gateway_lock() if require_anonymous else nullcontext()
        with lock:
            app = create_api_app(
                headless=args.headless,
                persistent=args.persistent,
                profile_dir=args.profile_dir,
                account_profiles=account_profiles or None,
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
                require_anonymous=require_anonymous,
                mock_backend=args.mock_backend,
            )
            apply_stream_deadline_floor(app.state.server)
            uvicorn.run(
                app,
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
    parser.add_argument(
        "--account",
        help="Use a named account profile created by 'gpt-web account login'.",
    )
    parser.add_argument(
        "--profile-dir",
        default=None,
        help="Legacy explicit persistent profile path. Without --account/--profile-dir, use anonymous mode.",
    )
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

    account = commands.add_parser(
        "account", help="Manage named ChatGPT Web account profiles"
    )
    account_commands = account.add_subparsers(dest="account_command", required=True)

    account_login = account_commands.add_parser(
        "login", help="Open CloakBrowser for manual login or use optional saved credentials"
    )
    account_login.add_argument("--name", required=True)
    account_login.add_argument("--url", default="https://chatgpt.com/")
    account_login.add_argument("--wait-seconds", type=int, default=300)
    account_login.add_argument(
        "--auto", action="store_true", help="Automate normal username/password/TOTP login"
    )
    account_login.add_argument(
        "--headful-auto", action="store_true", help="Show CloakBrowser during automated login"
    )
    account_login.add_argument("-u", "--username")
    account_login.add_argument("-p", "--password")
    account_login.add_argument("-2fa", "--two-factor", dest="two_factor")
    account_login.add_argument(
        "--cred", help="Credentials formatted as 'username|password|2fa_secret'"
    )
    account_login.add_argument(
        "--stdin", action="store_true", help="Read username|password|2fa from stdin"
    )
    account_login.add_argument(
        "--use-saved", action="store_true", help="Use this account's saved credential file"
    )
    account_login.add_argument(
        "--save-credentials",
        action="store_true",
        help="Persist username|password|2fa in a separate mode-0600 account credential file",
    )
    account_login.add_argument("--timeout", type=int, default=180)

    account_commands.add_parser("list", help="List registered account profiles")
    account_status = account_commands.add_parser("status", help="Show account profile status")
    account_status.add_argument("--name", required=True)
    account_status.add_argument(
        "--live", action="store_true", help="Open the profile headlessly and verify /api/auth/session"
    )
    account_cred_set = account_commands.add_parser(
        "credentials-set", help="Save username|password|2fa for later automated login"
    )
    account_cred_set.add_argument("--name", required=True)
    account_cred_set.add_argument("--cred")
    account_cred_set.add_argument("--stdin", action="store_true")
    account_cred_delete = account_commands.add_parser(
        "credentials-delete", help="Delete saved username|password|2fa for one account"
    )
    account_cred_delete.add_argument("--name", required=True)
    account_remove = account_commands.add_parser("remove", help="Remove an account registry entry")
    account_remove.add_argument("--name", required=True)
    account_remove.add_argument(
        "--delete-profile", action="store_true", help="Also delete that account's browser profile"
    )
    account_default = account_commands.add_parser(
        "default",
        help="Show, set, or clear the sticky default account for new sessions",
    )
    account_default.add_argument(
        "name", nargs="?", default=None, help="Account name to set as default"
    )
    account_default.add_argument("--show", action="store_true", help="Print current default")
    account_default.add_argument("--clear", action="store_true", help="Clear default")

    setup = commands.add_parser("setup", help="Open the dedicated profile for manual login")
    setup.add_argument("--url", default="https://chatgpt.com")
    setup.add_argument("--profile-dir", default=_configured_profile_dir())
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
    send.add_argument("--timeout", type=float, default=300)
    send.add_argument("--json", action="store_true")

    sandbox_exec = commands.add_parser(
        "sandbox-exec",
        help="Execute Python code inside ChatGPT Web Sandbox (Code Interpreter) and stream output",
    )
    _browser_args(sandbox_exec, persistent_default=True)
    sandbox_exec.add_argument("--code", "-c", help="Python code string to execute")
    sandbox_exec.add_argument("--file", "-f", help="Path to Python script file to execute")
    sandbox_exec.add_argument("--conversation", help="Open this conversation before executing")
    sandbox_exec.add_argument(
        "--model",
        default="gpt-5-6",
        help="Exact visible model label (default 'gpt-5-6' / 'GPT-5.6 Sol')",
    )
    sandbox_exec.add_argument(
        "--effort",
        "--reasoning-effort",
        dest="effort",
        choices=["instant", "low", "medium", "high", "max"],
        default="high",
        help="Reasoning effort level (default 'high')",
    )
    sandbox_exec.add_argument("--timeout", type=float, default=300)
    sandbox_exec.add_argument("--json", action="store_true")


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
    # None means: use PROFILE_DIR from .env/config. An explicit CLI value still wins.
    login_cmd.set_defaults(profile_dir=None)
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
    api.add_argument(
        "--profile-dir",
        default=None,
        help="Legacy explicit profile. Without --account/--profile-dir, gateway runs anonymous.",
    )
    api.add_argument("--executable-path")
    api.add_argument("--cdp-url", help="Attach to existing local Brave/Chromium CDP.")
    api.add_argument(
        "--account",
        action="append",
        dest="accounts",
        help="Use a named account profile. Repeat for multiple accounts.",
    )
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
        default=float(os.environ.get("WEBGPT_QUEUE_TIMEOUT", "600.0")),
        help="Maximum seconds a request may wait for worker capacity (default 600s).",
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
        default=float(os.environ.get("WEBGPT_GENERATION_TIMEOUT", "600.0")),
        help="Maximum seconds to wait for each ChatGPT Web generation before returning 504 (default 600s).",
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

    env_cmd = commands.add_parser(
        "env",
        help="Print shell exports scoping this terminal to the local gateway",
    )
    env_cmd.add_argument(
        "--gateway-url",
        default=None,
        help="Override the gateway base URL (default: ANTHROPIC_BASE_URL or built-in default)",
    )
    env_cmd.add_argument("--api-key", default=None, help="Override the gateway API key value")
    env_cmd.add_argument("--model", default=None, help="Override CLAUDE_DEFAULT_MODEL")

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
        "sandbox-exec": cmd_sandbox_exec,
        "experiment": cmd_experiment,
        "install-bqa-plugin": cmd_install_bqa_plugin,
    }
    if args.command == "account":
        account_handlers = {
            "login": lambda value: asyncio.run(cmd_account_login(value)),
            "list": cmd_account_list,
            "status": lambda value: asyncio.run(cmd_account_status(value)),
            "credentials-set": cmd_account_credentials_set,
            "credentials-delete": cmd_account_credentials_delete,
            "remove": cmd_account_remove,
            "default": cmd_account_default,
        }
        account_handlers[args.account_command](args)
    elif args.command in handlers:
        asyncio.run(handlers[args.command](args))
    elif args.command == "env":
        cmd_env_export(args)
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
