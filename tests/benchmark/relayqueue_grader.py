from __future__ import annotations

import ast
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class GradeFailure(AssertionError):
    pass


def invoke(project: Path, db: Path, *args: str, expect: int = 0) -> dict[str, Any]:
    command = [sys.executable, "-m", "relayqueue", "--db", str(db), *args]
    completed = subprocess.run(command, cwd=project, text=True, capture_output=True, timeout=20)
    output = completed.stdout if completed.returncode == 0 else completed.stderr
    try:
        value = json.loads(output.strip())
    except json.JSONDecodeError as exc:
        raise GradeFailure(f"{' '.join(args)} did not emit one JSON object: {output!r}") from exc
    if completed.returncode != expect:
        raise GradeFailure(
            f"{' '.join(args)} exit {completed.returncode}, expected {expect}; stderr={completed.stderr!r}"
        )
    if not isinstance(value, dict):
        raise GradeFailure(f"{' '.join(args)} must emit a JSON object")
    return value


def task_id(value: dict[str, Any]) -> str:
    task = value.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("id"), str):
        raise GradeFailure(f"missing task id: {value!r}")
    return task["id"]


def successful(project: Path, db: Path, *args: str) -> dict[str, Any]:
    value = invoke(project, db, *args)
    if value.get("ok") is not True:
        raise GradeFailure(f"success result lacks ok=true: {value!r}")
    return value


def check_static(project: Path) -> None:
    sources = list(project.rglob("*.py"))
    if not sources:
        raise GradeFailure("project has no Python source")
    forbidden_calls = {("os", "system"), ("subprocess", "run"), ("subprocess", "Popen")}
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "eval":
                raise GradeFailure(f"eval is prohibited: {path}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                base = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                if (base, node.func.attr) in forbidden_calls:
                    raise GradeFailure(f"external command execution is prohibited: {path}")


def worker(project: Path, db: Path) -> list[str]:
    claimed: list[str] = []
    while True:
        value = successful(project, db, "claim", "--worker", f"worker-{os.getpid()}")
        task = value.get("task")
        if task is None:
            return claimed
        identifier = task_id(value)
        token = task.get("lease_token")
        if not isinstance(token, str):
            raise GradeFailure("claim did not return lease_token")
        successful(project, db, "ack", "--task", identifier, "--lease-token", token)
        claimed.append(identifier)


def grade(project: Path) -> None:
    if not project.is_dir():
        raise GradeFailure(f"project directory does not exist: {project}")
    help_result = subprocess.run(
        [sys.executable, "-m", "relayqueue", "--help"],
        cwd=project,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if help_result.returncode != 0:
        raise GradeFailure(f"module help failed: {help_result.stderr!r}")
    check_static(project)
    tests = subprocess.run(["pytest", "-q"], cwd=project, text=True, capture_output=True, timeout=60)
    if tests.returncode != 0:
        raise GradeFailure(f"agent unit tests failed: {tests.stdout}\n{tests.stderr}")

    with tempfile.TemporaryDirectory(prefix="relayqueue-grade-") as temp:
        root = Path(temp)
        db = root / "main.sqlite"
        init = successful(project, db, "init")
        if init.get("schema_version") != 1:
            raise GradeFailure("init must return schema_version 1")
        successful(project, db, "init")

        first = successful(project, db, "enqueue", "--payload", '{"order": 1}', "--key", "alpha")
        first_id = task_id(first)
        duplicate = successful(project, db, "enqueue", "--payload", '{"order": 1}', "--key", "alpha")
        if duplicate.get("deduplicated") is not True or task_id(duplicate) != first_id:
            raise GradeFailure("same idempotency key/payload must deduplicate")
        invoke(project, db, "enqueue", "--payload", '{"order": 2}', "--key", "alpha", expect=1)
        invoke(project, db, "enqueue", "--payload", "[]", expect=1)

        second = successful(project, db, "enqueue", "--payload", '{"order": 2}', "--key", "beta")
        claim_one = successful(project, db, "claim", "--worker", "a", "--lease-seconds", "2")
        if task_id(claim_one) != first_id:
            raise GradeFailure("claim must be FIFO")
        token = claim_one["task"].get("lease_token")
        invoke(project, db, "ack", "--task", first_id, "--lease-token", "wrong", expect=1)
        successful(project, db, "ack", "--task", first_id, "--lease-token", token)
        invoke(project, db, "ack", "--task", first_id, "--lease-token", token, expect=1)

        claim_two = successful(project, db, "claim", "--worker", "b", "--lease-seconds", "1")
        if task_id(claim_two) != task_id(second):
            raise GradeFailure("second FIFO claim failed")
        time.sleep(1.2)
        reclaimed = successful(project, db, "claim", "--worker", "c", "--lease-seconds", "2")
        if task_id(reclaimed) != task_id(second):
            raise GradeFailure("expired lease must be eligible again")
        successful(project, db, "ack", "--task", task_id(second), "--lease-token", reclaimed["task"]["lease_token"])

        retry = successful(project, db, "--max-attempts", "2", "enqueue", "--payload", '{"retry": true}')
        retry_id = task_id(retry)
        lease = successful(project, db, "--max-attempts", "2", "claim", "--worker", "retry")
        failed = successful(
            project, db, "--max-attempts", "2", "fail", "--task", retry_id,
            "--lease-token", lease["task"]["lease_token"], "--retry-after", "0"
        )
        if failed["task"].get("state") != "ready" or failed["task"].get("attempts") != 1:
            raise GradeFailure("first failure must schedule ready retry with attempt 1")
        lease = successful(project, db, "--max-attempts", "2", "claim", "--worker", "retry")
        dead = successful(
            project, db, "--max-attempts", "2", "fail", "--task", retry_id,
            "--lease-token", lease["task"]["lease_token"], "--retry-after", "0"
        )
        if dead["task"].get("state") != "dead" or dead["task"].get("attempts") != 2:
            raise GradeFailure("max attempts must create dead task")

        injection = successful(
            project, db, "enqueue", "--payload", '{"value": "x; DROP TABLE tasks;"}',
            "--key", "x'); DROP TABLE tasks; --"
        )
        successful(project, db, "stats")
        if not task_id(injection):
            raise GradeFailure("injection payload was not retained as data")

        concurrent_db = root / "concurrent.sqlite"
        successful(project, concurrent_db, "init")
        for index in range(100):
            successful(project, concurrent_db, "enqueue", "--payload", json.dumps({"n": index}))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            batches = list(pool.map(lambda _unused: worker(project, concurrent_db), range(4)))
        all_ids = [identifier for batch in batches for identifier in batch]
        if len(all_ids) != 100 or len(set(all_ids)) != 100:
            raise GradeFailure(f"concurrent claims are not unique: {len(all_ids)} total, {len(set(all_ids))} unique")
        stats = successful(project, concurrent_db, "stats")
        if stats.get("counts", {}).get("succeeded") != 100:
            raise GradeFailure(f"concurrency tasks not all succeeded: {stats!r}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: grader.py <relayqueue-project>")
    try:
        grade(Path(sys.argv[1]).resolve())
    except (GradeFailure, subprocess.SubprocessError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps({"ok": True, "checks": [f"RQ-{index:02d}" for index in range(1, 14)]}))


if __name__ == "__main__":
    main()

