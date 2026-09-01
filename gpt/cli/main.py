from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from gpt.agent.events import AgentEvent
from gpt.agent.runner import AgentRunner, AgentRunnerConfig
from gpt.agent.session import SessionStore
from gpt.core.settings import Settings
from gpt.tools.process import ProcessRunner

from .account import run_account_command
from .common import ensure_gateway, restart_gateway
from .config_cmd import run_config_command
from .doctor import run_doctor
from .session import run_session_command
from .status import print_status

_COMMANDS = {
    "status",
    "doctor",
    "config",
    "session",
    "account",
    "bench",
    "restart",
    "logs",
    "debug",
    "compat",
    "help",
}


def _base_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpt",
        add_help=True,
        description="WebGPT direct agent. The common path is simply: gpt \"your task\"",
        epilog=(
            "commands: status, doctor, config, session, account, bench, logs\n"
            "advanced: debug, compat"
        ),
    )
    parser.add_argument(
        "-C",
        "--workspace",
        default=".",
        help="workspace directory (default: current directory)",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--model")
    parser.add_argument(
        "--verify",
        choices=["auto", "quick", "full", "off"],
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="forget the remembered session for this workspace before running",
    )
    parser.add_argument(
        "--no-session",
        action="store_true",
        help="do not remember/reuse a gateway session",
    )
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.load(
        workspace=Path(args.workspace),
        overrides={
            "model": args.model,
            "verify": args.verify,
            "verbosity": args.verbose,
        },
    )


def _event_printer(verbosity: int):
    def emit(event: AgentEvent) -> None:
        if event.kind == "response" and verbosity >= 2:
            print(
                f"  ↳ gateway {event.data.get('status_code')} "
                f"{event.data.get('elapsed_seconds')}s "
                f"session={event.data.get('session_id')}",
                file=sys.stderr,
                flush=True,
            )
            return
        if event.kind == "tool_start":
            tool = event.data.get("tool")
            value = event.data.get("input") or {}
            if tool == "Bash":
                command = value.get("command", "")
                if verbosity:
                    print(f"  $ {command}", file=sys.stderr, flush=True)
                else:
                    print("● shell", file=sys.stderr, flush=True)
            else:
                print(f"● {str(tool).lower()}", file=sys.stderr, flush=True)
            return
        if event.kind == "tool_end":
            status = event.data.get("status")
            if verbosity:
                print(
                    f"  {'✓' if status == 'ok' else '✗'} "
                    f"{event.data.get('tool')} "
                    f"{event.data.get('duration_ms')}ms",
                    file=sys.stderr,
                    flush=True,
                )
            return
        if event.kind == "verification_gate":
            print("● verification required", file=sys.stderr, flush=True)

    return emit


def _runner_config(settings: Settings, *, persist_session: bool) -> AgentRunnerConfig:
    return AgentRunnerConfig(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
        max_tokens=settings.max_tokens,
        max_rounds=settings.max_rounds,
        timeout_seconds=settings.timeout_seconds,
        verify=settings.verify,
        persist_session=persist_session,
    )


def _run_prompt(
    settings: Settings,
    prompt: str,
    *,
    verbosity: int,
    persist_session: bool,
    new_session: bool,
    json_output: bool,
) -> int:
    if not ensure_gateway(settings):
        print(
            "gpt: gateway is unavailable; run gpt doctor --fix.",
            file=sys.stderr,
        )
        return 1
    store = SessionStore.default()
    if new_session:
        store.clear(settings.workspace)
    callback = _event_printer(verbosity)
    with AgentRunner(
        workspace=settings.workspace,
        config=_runner_config(settings, persist_session=persist_session),
        event_callback=callback,
        session_store=store if persist_session else None,
    ) as agent:
        result = agent.run(prompt)
    if json_output:
        from .common import json_print

        json_print(
            {
                "success": result.success,
                "text": result.text,
                "rounds": result.rounds,
                "tool_calls": result.tool_calls,
                "stop_reason": result.stop_reason,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "verification_gate_count": result.verification_gate_count,
                "error": result.error,
            }
        )
    else:
        if result.text:
            print(result.text)
        if result.error:
            print(f"gpt: {result.error}", file=sys.stderr)
    return 0 if result.success else 1


def _interactive(settings: Settings, args: argparse.Namespace) -> int:
    if not ensure_gateway(settings):
        print("gpt: gateway unavailable; run gpt doctor --fix.", file=sys.stderr)
        return 1
    store = SessionStore.default()
    if args.new_session:
        store.clear(settings.workspace)
    persist = not args.no_session
    callback = _event_printer(args.verbose)
    with AgentRunner(
        workspace=settings.workspace,
        config=_runner_config(settings, persist_session=persist),
        event_callback=callback,
        session_store=store if persist else None,
    ) as agent:
        print(
            f"{settings.workspace} · {settings.model} · {settings.account}\n"
            "Type /exit to quit, /new to clear the remembered session.\n"
        )
        while True:
            try:
                prompt = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not prompt:
                continue
            if prompt in {"/exit", "/quit"}:
                return 0
            if prompt == "/new":
                store.clear(settings.workspace)
                print("Session cleared; restart gpt for a fresh in-memory runner.")
                continue
            result = agent.run(prompt)
            if result.text:
                print(result.text)
            if result.error:
                print(f"gpt: {result.error}", file=sys.stderr)


def _dispatch_command(
    settings: Settings, command: str, argv: Sequence[str], root: argparse.Namespace
) -> int:
    if command == "status":
        return print_status(settings, as_json=root.json_output)

    if command == "restart":
        ok = restart_gateway(settings)
        if not ok:
            print("Gateway restart failed.", file=sys.stderr)
            return 1
        return print_status(settings, as_json=root.json_output)

    if command == "doctor":
        parser = argparse.ArgumentParser(prog="gpt doctor")
        parser.add_argument("--deep", action="store_true")
        parser.add_argument("--fix", action="store_true")
        args = parser.parse_args(list(argv))
        return run_doctor(settings, deep=args.deep, fix=args.fix)

    if command == "config":
        parser = argparse.ArgumentParser(prog="gpt config")
        parser.add_argument(
            "action",
            nargs="?",
            choices=["show", "path", "init", "set"],
            default="show",
        )
        parser.add_argument("key", nargs="?")
        parser.add_argument("value", nargs="?")
        args = parser.parse_args(list(argv))
        return run_config_command(
            settings,
            args.action,
            key=args.key,
            value=args.value,
        )

    if command == "session":
        parser = argparse.ArgumentParser(prog="gpt session")
        parser.add_argument(
            "action",
            nargs="?",
            choices=["list", "current", "new", "clear"],
            default="current",
        )
        args = parser.parse_args(list(argv))
        return run_session_command(settings.workspace, args.action)

    if command == "account":
        if argv and argv[0] == "codex-login":
            from scripts.auth.codex_oauth_login import main as codex_login_main

            return int(codex_login_main(list(argv[1:])))
        parser = argparse.ArgumentParser(prog="gpt account")
        parser.add_argument(
            "action",
            nargs="?",
            choices=["list", "default", "status", "codex-login"],
            default="list",
        )
        parser.add_argument("name", nargs="?")
        args = parser.parse_args(list(argv))
        if args.action == "codex-login":
            from scripts.auth.codex_oauth_login import main as codex_login_main

            return int(codex_login_main([]))
        try:
            return run_account_command(args.action, args.name)
        except (KeyError, ValueError) as exc:
            print(f"gpt account: {exc}", file=sys.stderr)
            return 1

    if command == "logs":
        parser = argparse.ArgumentParser(prog="gpt logs")
        parser.add_argument("-n", "--lines", type=int, default=80)
        args = parser.parse_args(list(argv))
        runner = ProcessRunner(default_timeout_seconds=10, max_output_chars=50_000)
        result = runner.run(
            "journalctl --user -u webgpt-gateway.service --no-pager "
            f"-n {max(1, min(args.lines, 2000))}",
            cwd=settings.workspace,
        )
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return 0 if not result.is_error else 1

    if command == "bench":
        kinds = {"practical", "soak", "e2e", "selfcheck", "review"}
        if argv and argv[0] in {"-h", "--help"}:
            parser = argparse.ArgumentParser(prog="gpt bench")
            parser.add_argument(
                "kind",
                nargs="?",
                choices=sorted(kinds),
                default="practical",
            )
            parser.print_help()
            return 0
        if argv and not argv[0].startswith("-") and argv[0] not in kinds:
            parser = argparse.ArgumentParser(prog="gpt bench")
            parser.error(f"unknown benchmark kind: {argv[0]}")
        kind = argv[0] if argv and argv[0] in kinds else "practical"
        remainder = list(argv[1:] if argv and argv[0] in kinds else argv)
        if kind == "practical":
            from scripts.run_practical_bench import main as bench_main

            return int(bench_main(remainder))
        if kind == "soak":
            from scripts.bench.soak_runner import main as soak_main

            return int(soak_main(remainder))
        if kind == "e2e":
            from scripts.bench.e2e_project_benchmark import main as e2e_main

            return int(e2e_main(remainder))
        if kind == "selfcheck":
            from scripts.bench.selfcheck_practical_v2 import main as selfcheck_main

            return int(selfcheck_main(remainder))
        from scripts.review_gate import main as review_main

        return int(review_main(remainder))

    if command == "debug":
        from gpt import debug

        old = sys.argv
        sys.argv = ["gpt debug", *argv]
        try:
            debug.main()
        except SystemExit as exc:
            return int(exc.code or 0)
        finally:
            sys.argv = old
        return 0

    if command == "compat":
        if not argv or argv[0] != "claude":
            print("Usage: gpt compat claude [args...]", file=sys.stderr)
            return 2
        script = Path(__file__).resolve().parents[2] / "scripts" / "webgpt-claude.sh"
        os.execv(str(script), [str(script), *argv[1:]])
        return 1

    if command == "help":
        _base_parser().print_help()
        return 0

    raise ValueError(command)


def main(argv: list[str] | None = None) -> int:
    parser = _base_parser()
    args = parser.parse_args(argv)
    settings = _settings(args)
    rest = list(args.rest)

    if rest and rest[0] in _COMMANDS:
        return _dispatch_command(settings, rest[0], rest[1:], args)

    if rest:
        prompt = " ".join(rest).strip()
        return _run_prompt(
            settings,
            prompt,
            verbosity=args.verbose,
            persist_session=not args.no_session,
            new_session=args.new_session,
            json_output=args.json_output,
        )

    if not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        if not prompt:
            parser.print_help()
            return 2
        return _run_prompt(
            settings,
            prompt,
            verbosity=args.verbose,
            persist_session=not args.no_session,
            new_session=args.new_session,
            json_output=args.json_output,
        )
    return _interactive(settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
