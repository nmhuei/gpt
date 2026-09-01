#!/usr/bin/env python
"""Offline auto-grader for the practical benchmark (benchmarks/practical).

Usage:
    .venv/bin/python benchmarks/practical/grade.py --task <bugfix|feature|refactor> --ws <workspace>

What it does (all offline, all inside throwaway tmp copies):
  1. baseline sanity   -- pristine fixture + visible tests must fail (or pass,
     per task.json) and pristine fixture + hidden grader suite must fail;
     proves the environment and the suites are discriminating.
  2. required files    -- workspace must contain the contract files.
  3. main pytest run   -- visible tests AND injected hidden grader suite must
     be fully green in the workspace copy, with minimum test counts met.
  4. structural checks -- optional source regexes from task.json (used by the
     refactor task to demand a real shared util import).
  5. mutation check    -- for every declared mutation, grade.py injects a
     deliberately broken implementation (via an auto-generated pytest plugin
     that stomps the target function across the package) into a fresh copy of
     the graded workspace and requires the combined suite to FAIL. A suite
     that still passes on broken code is trivially-passing -> overall FAIL.
     For the refactor task this also proves the shared util is load-bearing.

Output: human-readable check table ending in "RESULT: PASS|FAIL".
Exit codes: 0 = PASS, 1 = FAIL, 2 = usage/config error.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS_DIR = HERE / "tasks"
GRADER_SUITES_DIR = HERE / "grader_suites"
GRADER_SUBDIR = "_grader"
MUTATION_PLUGIN_NAME = "wb_mutation_plugin"

# ---------------------------------------------------------------------------
# Auto-generated pytest plugin used for mutation checks. Static code; the
# concrete patches are selected at runtime via WB_MUTATION (JSON in env).
# ---------------------------------------------------------------------------
MUTATION_PLUGIN_SOURCE = '''\
"""Auto-generated plugin for practical-bench mutation checks.

Loaded with `-p wb_mutation_plugin`; configured via the WB_MUTATION env var
(JSON: {"package": ..., "patches": [{"kind": ..., "attr": ...}]}).
Patches run in pytest_configure, i.e. BEFORE any test module is imported,
so `from pkg import name` bindings created during collection see the mutant.
The mutant attribute is stomped on EVERY module of the package so both
`pkg.name` re-exports and `from .x import name` copies are covered.
"""
import importlib
import json
import os
import pkgutil

_CFG = json.loads(os.environ["WB_MUTATION"])


def _k_bugfix_edge_strip(module, orig):
    import string

    def _mutant(text):
        toks = [t.strip(string.punctuation) for t in text.split()]
        return len([t for t in toks if t])

    return _mutant


def _k_bugfix_unique_count(module, orig):
    import re

    def _mutant(text):
        return len(set(re.findall(r"[A-Za-z0-9]+", text)))

    return _mutant


def _k_feature_keep_case(module, orig):
    import re
    import unicodedata

    def _mutant(text, sep="-"):
        t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore")
        t = t.decode("ascii")
        t = re.sub(r"[^a-z0-9]+", sep, t)
        return t.strip(sep)

    return _mutant


def _k_feature_no_dedupe(module, orig):
    base = module.slugify

    def _mutant(text, existing=None, sep="-"):
        return base(text, sep)

    return _mutant


def _k_refactor_loose_normalize(module, orig):
    def _mutant(token):
        return token.strip().lower()

    return _mutant


def _k_v2bug_compound_pct(module, orig):
    def _mutant(order, discounts):
        total = sum(li.qty * li.unit_price_cents for li in order.items)
        for d in discounts:
            if d.kind == "pct":
                total = (total * (10000 - d.basis_points)) // 10000
            elif d.kind == "flat":
                total -= d.cents
        return max(total, 0)
    return _mutant


def _k_v2bug_half_up_round(module, orig):
    def _mutant(order, discounts):
        subtotal = sum(li.qty * li.unit_price_cents for li in order.items)
        bp = min(10000, sum(max(0, d.basis_points) for d in discounts if d.kind == "pct"))
        total = (subtotal * (10000 - bp) + 5000) // 10000
        total -= sum(d.cents for d in discounts if d.kind == "flat")
        return max(total, 0)
    return _mutant


def _k_v2bug_first_fit_alloc(module, orig):
    def _mutant(total_cents, weights):
        if not weights:
            return []
        total_weight = sum(weights)
        if total_weight <= 0:
            raise ValueError("weights must sum to a positive value")
        shares = [total_cents * w // total_weight for w in weights]
        shares[0] += total_cents - sum(shares)
        return shares
    return _mutant


def _tb_state(self):
    self._refill()
    return self._tokens


def _k_v2tb_drain_on_deny(module, orig):
    def _mutant(self, n=1):
        if n <= 0:
            raise ValueError("n must be positive")
        self._refill()
        if n > self.capacity or self._tokens < n:
            self._tokens = 0.0
            return False
        self._tokens -= n
        return True
    return _mutant


def _k_v2tb_no_cap_write(module, orig):
    def _mutant(self):
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = self._tokens + elapsed * self.refill_per_sec
        self._last = now
    return _mutant


def _k_v2tb_partial_consume(module, orig):
    def _mutant(self, n=1):
        if n <= 0:
            raise ValueError("n must be positive")
        self._refill()
        if n > self.capacity:
            return False
        if self._tokens < n:
            self._tokens = 0.0
            return True
        self._tokens -= n
        return True
    return _mutant


def _k_v2tb_over_capacity_consume(module, orig):
    def _mutant(self, n=1):
        if n <= 0:
            raise ValueError("n must be positive")
        self._refill()
        if n > self.capacity:
            self._tokens = 0.0
            return True
        if self._tokens < n:
            return False
        self._tokens -= n
        return True
    return _mutant


def _k_v2ref_drop_nfc(module, orig):
    def _mutant(value):
        if value is None:
            return None
        text = str(value).strip()
        return None if text == "" else text
    return _mutant


def _k_v2ref_drop_strip(module, orig):
    import unicodedata
    def _mutant(value):
        if value is None:
            return None
        text = unicodedata.normalize("NFC", str(value))
        return None if text == "" else text
    return _mutant


def _k_v2ref_empty_stays_empty(module, orig):
    import unicodedata
    def _mutant(value):
        if value is None:
            return None
        return unicodedata.normalize("NFC", str(value)).strip()
    return _mutant


def _k_v2log_remove_guard(module, orig):
    def _mutant(self, key, statuses):
        for status in statuses:
            result = self.transport.request(status)
            if result >= 500:
                continue
            self.sink.send(key)
            self.sink.send(key)
            return result
        return statuses[-1] if statuses else 200
    return _mutant


def _k_v2log_leak_session(module, orig):
    def _mutant(self, status):
        self.sessions_open += 1
        if status >= 500:
            return status
        self.sessions_close += 1
        return status
    return _mutant


def _k_v2api_retry_disabled(module, orig):
    def _mutant(self, status=None, exc=None):
        return False
    return _mutant


def _k_v2api_fixed_sleep(module, orig):
    def _mutant(self, attempt, retry_after=None):
        return 0.1
    return _mutant


def _k_v2api_cursor_drop(module, orig):
    def _mutant(client, since=None):
        page = client.fetch_page(cursor=None, since=since)
        yield from page["items"]
    return _mutant


def _k_v2api_retry_on_401(module, orig):
    def _mutant(self, status=None, exc=None):
        if exc is not None:
            return True
        return status == 429 or (status is not None and status >= 500) or status == 401
    return _mutant


_FACTORIES = {
    "bugfix:edge_strip": _k_bugfix_edge_strip,
    "bugfix:unique_count": _k_bugfix_unique_count,
    "feature:keep_case": _k_feature_keep_case,
    "feature:no_dedupe": _k_feature_no_dedupe,
    "refactor:loose_normalize": _k_refactor_loose_normalize,
    "v2bug:compound_pct": _k_v2bug_compound_pct,
    "v2bug:half_up_round": _k_v2bug_half_up_round,
    "v2bug:first_fit_alloc": _k_v2bug_first_fit_alloc,
    "v2tb:drain_on_deny": _k_v2tb_drain_on_deny,
    "v2tb:no_cap_write": _k_v2tb_no_cap_write,
    "v2tb:partial_consume": _k_v2tb_partial_consume,
    "v2tb:over_capacity_consume": _k_v2tb_over_capacity_consume,
    "v2ref:drop_nfc": _k_v2ref_drop_nfc,
    "v2ref:drop_strip": _k_v2ref_drop_strip,
    "v2ref:empty_stays_empty": _k_v2ref_empty_stays_empty,
    "v2log:remove_inflight_guard": _k_v2log_remove_guard,
    "v2log:leak_session_again": _k_v2log_leak_session,
    "v2api:retry_disabled": _k_v2api_retry_disabled,
    "v2api:fixed_sleep": _k_v2api_fixed_sleep,
    "v2api:cursor_drop": _k_v2api_cursor_drop,
    "v2api:retry_on_401": _k_v2api_retry_on_401,
}


def pytest_configure(config):
    pkg_name = _CFG["package"]
    pkg = importlib.import_module(pkg_name)
    modules = [pkg]
    for mi in pkgutil.walk_packages(pkg.__path__, pkg_name + "."):
        modules.append(importlib.import_module(mi.name))
    for patch in _CFG["patches"]:
        attr = patch["attr"]
        target = next((m for m in modules if hasattr(m, attr)), None)
        if target is not None:
            mutant = _FACTORIES[patch["kind"]](target, getattr(target, attr))
            for mod in modules:
                if hasattr(mod, attr):
                    try:
                        setattr(mod, attr, mutant)
                    except (AttributeError, TypeError):
                        pass
            continue
        # V2: support method-level mutation. Find every class carrying attr and
        # patch it in-place before test modules import the package surface.
        for mod in modules:
            for obj in vars(mod).values():
                if isinstance(obj, type) and hasattr(obj, attr):
                    orig = getattr(obj, attr)
                    mutant = _FACTORIES[patch["kind"]](mod, orig)
                    try:
                        setattr(obj, attr, mutant)
                    except (AttributeError, TypeError):
                        pass
'''


class SuiteStats:
    def __init__(self, total: int, failed: int, errors: int, skipped: int,
                 grader_total: int, grader_failed: int):
        self.total = total
        self.failed = failed
        self.errors = errors
        self.skipped = skipped
        self.grader_total = grader_total
        self.grader_failed = grader_failed

    @property
    def passed(self) -> int:
        return self.total - self.failed - self.errors - self.skipped

    @property
    def grader_passed(self) -> int:
        return self.grader_total - self.grader_failed

    @property
    def visible_passed(self) -> int:
        return self.passed - self.grader_passed

    def describe(self) -> str:
        return (
            f"rc-ok passed={self.passed} failed={self.failed} "
            f"errors={self.errors} skipped={self.skipped} "
            f"(grader {self.grader_passed}/{self.grader_total})"
        )


def parse_junit(junit_path: Path) -> SuiteStats | None:
    if not junit_path.exists():
        return None
    try:
        root = ET.parse(junit_path).getroot()
    except ET.ParseError:
        return None
    suite = root.find(".//testsuite")
    if suite is None:
        return None
    total = int(suite.get("tests", "0"))
    failed = int(suite.get("failures", "0"))
    errors = int(suite.get("errors", "0"))
    skipped = int(suite.get("skipped", "0"))
    grader_total = grader_failed = 0
    for case in suite.iter("testcase"):
        # classnames look like "_grader.grader_check_bugfix.test_x" -- match
        # on the marker anywhere in classname/file to stay layout-agnostic.
        marker_hit = "grader_check" in (case.get("classname") or "") or \
            "grader_check" in (case.get("file") or "")
        if marker_hit:
            grader_total += 1
            if case.find("failure") is not None or case.find("error") is not None:
                grader_failed += 1
    return SuiteStats(total, failed, errors, skipped, grader_total, grader_failed)


def run_pytest(paths: list[str], cwd: Path, junit: Path,
               extra_args: list[str] | None = None,
               env_extra: dict[str, str] | None = None,
               timeout: int = 240):
    cmd = [sys.executable, "-m", "pytest", "-q", "--junitxml", str(junit)]
    cmd += list(paths) + list(extra_args or [])
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    tail = ""
    rc: int | None
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, timeout=timeout,
                              capture_output=True, text=True)
        rc = proc.returncode
        tail_lines = (proc.stdout or "").strip().splitlines()
        tail = tail_lines[-1] if tail_lines else ""
    except subprocess.TimeoutExpired:
        rc = None
        tail = f"TIMEOUT after {timeout}s"
    return rc, tail, parse_junit(junit)


def copy_tree(src: Path, parent: Path, name: str) -> Path:
    dst = parent / name
    shutil.copytree(src, dst, symlinks=True)
    return dst


def _ignored_relpath(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part in {"__pycache__", ".pytest_cache", ".git"} for part in parts)


def _tree_hashes(root: Path) -> dict[str, str]:
    """Return stable SHA-256 hashes for regular files under root."""
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _ignored_relpath(rel):
            continue
        try:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            out[rel] = "<unreadable>"
    return out


def _changed_paths(pristine: Path, candidate: Path) -> set[str]:
    before = _tree_hashes(pristine)
    after = _tree_hashes(candidate)
    return {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}


def _glob_allowed(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)


def _check_diff_confinement(spec: dict, fixture: Path, ws: Path) -> tuple[bool, str] | None:
    patterns = list(spec.get("allowed_globs") or [])
    if not patterns:
        return None
    changed = sorted(_changed_paths(fixture, ws))
    bad = [rel for rel in changed if not _glob_allowed(rel, patterns)]
    if bad:
        return False, f"changes outside allowed_globs: {', '.join(bad[:12])}"
    return True, f"{len(changed)} changed path(s), all confined to allowed_globs"


def _check_locked_paths(spec: dict, fixture: Path, ws: Path) -> tuple[bool, str] | None:
    locked = list(dict.fromkeys(spec.get("locked_paths") or []))
    if not locked:
        return None
    bad: list[str] = []
    for rel in locked:
        src, dst = fixture / rel, ws / rel
        if not src.is_file() or not dst.is_file():
            bad.append(rel)
            continue
        try:
            if hashlib.sha256(src.read_bytes()).digest() != hashlib.sha256(dst.read_bytes()).digest():
                bad.append(rel)
        except OSError:
            bad.append(rel)
    if bad:
        return False, f"locked path changed/missing: {', '.join(bad[:12])}"
    return True, f"{len(locked)} locked path(s) byte-identical"



def _count_asserts(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return 0
    return sum(isinstance(node, ast.Assert) for node in ast.walk(tree))


def _check_model_regression(spec: dict, fixture: Path, ws: Path, tmp: Path) -> tuple[bool, str] | None:
    cfg = spec.get("model_regression_check")
    if not isinstance(cfg, dict):
        return None
    rel = str(cfg.get("path") or "")
    if not rel:
        return False, "model_regression_check.path missing"
    candidate_test = ws / rel
    min_asserts = int(cfg.get("min_asserts", 1))
    asserts = _count_asserts(candidate_test)
    if asserts < min_asserts:
        return False, f"{rel} has {asserts} assert(s), need >= {min_asserts}"
    rc_ws, _tail, _st = run_pytest([rel], ws, tmp / "j_model_regression_ws.xml")
    if rc_ws != 0:
        return False, f"candidate regression test is not green (rc={rc_ws})"
    pristine = copy_tree(fixture, tmp, "model_regression_pristine")
    target = pristine / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidate_test, target)
    rc_pristine, _tail, _st = run_pytest([rel], pristine, tmp / "j_model_regression_pristine.xml")
    require_red = bool(cfg.get("must_fail_on_fixture", True))
    if require_red and rc_pristine == 0:
        return False, "regression test also passes on pristine fixture; not a red→green proof"
    return True, f"{rel}: {asserts} asserts, candidate green, pristine rc={rc_pristine}"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)


def _check_git_history(spec: dict, ws: Path, tmp: Path) -> tuple[bool, str] | None:
    cfg = spec.get("git_history")
    if not isinstance(cfg, dict):
        return None
    if not (ws / ".git").exists():
        return False, "workspace has no .git history"
    log = _git("log", "--reverse", "--format=%H%x09%s", cwd=ws)
    if log.returncode != 0:
        return False, f"git log failed: {(log.stderr or log.stdout).strip()[:160]}"
    commits = [line.split("\t", 1) for line in log.stdout.splitlines() if "\t" in line]
    min_commits = int(cfg.get("min_commits", 0))
    if len(commits) < min_commits:
        return False, f"history has {len(commits)} commits, need >= {min_commits}"
    cursor = 0
    matched: list[tuple[str, str]] = []
    for rule in cfg.get("require_ordered_commits") or []:
        prefix = str(rule.get("message_prefix") or "")
        glob = str(rule.get("paths_glob") or "")
        found = None
        for idx in range(cursor, len(commits)):
            sha, subject = commits[idx]
            if subject.startswith(prefix):
                found = (idx, sha, subject)
                break
        if found is None:
            return False, f"missing ordered commit prefix {prefix!r}"
        idx, sha, subject = found
        changed = _git("diff-tree", "--no-commit-id", "--name-only", "-r", sha, cwd=ws)
        paths = [line.strip() for line in changed.stdout.splitlines() if line.strip()]
        bad = [path for path in paths if glob and not fnmatch.fnmatch(path, glob)]
        if bad:
            return False, f"commit {subject!r} changes outside {glob}: {', '.join(bad[:8])}"
        matched.append((sha, subject))
        cursor = idx + 1
    # TDD-specific mechanical proof: checkout the tests commit and require the
    # test suite to be red there. This is enabled by the schema's convention.
    tests_commit = next(((sha, sub) for sha, sub in matched if sub.startswith("[tdd] tests")), None)
    if tests_commit is not None:
        checkout = tmp / "git_history_tests_commit"
        shutil.copytree(ws, checkout, symlinks=True)
        co = _git("checkout", "--detach", tests_commit[0], cwd=checkout)
        if co.returncode != 0:
            return False, f"cannot checkout TDD tests commit: {co.stderr.strip()[:160]}"
        rc, _tail, _st = run_pytest(["tests"], checkout, tmp / "j_tdd_red.xml")
        if rc == 0:
            return False, "[tdd] tests commit is green; required red-before-implementation proof missing"
    return True, f"{len(commits)} commits; ordered rules={len(matched)}" + ("; TDD tests commit red" if tests_commit else "")


def _run_test_strength(spec: dict, work: Path, grader_relpath: str, tmp: Path) -> tuple[bool, str] | None:
    cfg = spec.get("test_strength")
    if not isinstance(cfg, dict):
        return None
    suite_glob = str(cfg.get("suite_glob") or "")
    wanted = set(str(x) for x in (cfg.get("mutants_ref") or []))
    min_kill = int(cfg.get("min_kill", len(wanted)))
    mutations = [m for m in spec.get("mutations", []) if str(m.get("id")) in wanted]
    if not suite_glob or not mutations:
        return False, "test_strength requires suite_glob and referenced mutations"
    suites = sorted(path.relative_to(work).as_posix() for path in work.glob(suite_glob) if path.is_file())
    if not suites:
        return False, f"test_strength suite_glob matched nothing: {suite_glob}"
    killed = 0
    for m in mutations:
        mcopy = copy_tree(work, tmp, f"strength_{m['id']}")
        plugdir = mcopy / "_wb_mutation"
        plugdir.mkdir()
        (plugdir / f"{MUTATION_PLUGIN_NAME}.py").write_text(MUTATION_PLUGIN_SOURCE, encoding="utf-8")
        cfg_env = {"package": spec["entry_package"], "patches": [{"kind": m["kind"], "attr": m["attr"]}]}
        pypath = os.environ.get("PYTHONPATH", "")
        env = {"WB_MUTATION": json.dumps(cfg_env), "PYTHONPATH": str(plugdir) + (os.pathsep + pypath if pypath else "")}
        rc, _tail, _st = run_pytest(suites, mcopy, tmp / f"j_strength_{m['id']}.xml", extra_args=["-p", MUTATION_PLUGIN_NAME], env_extra=env)
        if rc != 0:
            killed += 1
    return killed >= min_kill, f"model-authored suite killed {killed}/{len(mutations)} mutants; need >= {min_kill}"



def _format_cmd(parts: list[str], **values: object) -> list[str]:
    mapping = {"python": sys.executable, **{k: str(v) for k, v in values.items()}}
    return [str(part).format(**mapping) for part in parts]


def _run_command(
    cmd: list[str], cwd: Path, *, timeout: int = 60,
    env_extra: dict[str, str] | None = None,
) -> tuple[int | None, str, str]:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env, timeout=timeout,
            capture_output=True, text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _check_heldout_corpus(
    spec: dict, work: Path, tmp: Path,
) -> tuple[bool, str] | None:
    """Run a trusted generated corpus through reference and candidate CLIs.

    V2 config is deliberately command-based so the engine stays language- and
    project-agnostic. Commands may use {python}, {input}, {output}, {expected}.
    The trusted reference command executes from the benchmark root while the
    candidate/generator execute in the isolated candidate copy.
    """
    cfg = spec.get("heldout_corpus")
    if not isinstance(cfg, dict):
        return None
    generator = list(cfg.get("generator_cmd") or cfg.get("cmd") or [])
    candidate = list(cfg.get("candidate_cmd") or spec.get("entry_cli") or [])
    reference = list(cfg.get("reference_cmd") or [])
    if not generator or not candidate or not reference:
        return False, "heldout_corpus needs generator_cmd/candidate_cmd/reference_cmd"

    case_dir = tmp / "heldout"
    case_dir.mkdir()
    input_path = case_dir / str(cfg.get("input_name") or "input.dat")
    output_path = case_dir / str(cfg.get("output_name") or "candidate.out")
    expected_path = case_dir / str(cfg.get("expected_name") or "expected.out")
    values = {"input": input_path, "output": output_path, "expected": expected_path}

    rc, out, err = _run_command(
        _format_cmd(generator, **values), work,
        timeout=int(cfg.get("timeout_s", 60)),
    )
    if rc != 0:
        return False, f"heldout generator failed rc={rc}: {err.strip()[:160]}"
    input_path.write_bytes(out.encode("utf-8"))

    rc, _out, err = _run_command(
        _format_cmd(reference, **values), HERE,
        timeout=int(cfg.get("timeout_s", 60)),
    )
    if rc != 0 or not expected_path.is_file():
        return False, f"heldout reference failed rc={rc}: {err.strip()[:160]}"

    rc, _out, err = _run_command(
        _format_cmd(candidate, **values), work,
        timeout=int(cfg.get("timeout_s", 60)),
    )
    if rc != 0 or not output_path.is_file():
        return False, f"heldout candidate failed rc={rc}: {err.strip()[:160]}"

    compare = str(cfg.get("compare") or "bytes")
    if compare != "bytes":
        return False, f"unsupported heldout compare mode: {compare}"
    actual = output_path.read_bytes()
    expected = expected_path.read_bytes()
    return actual == expected, f"heldout bytes candidate={len(actual)} expected={len(expected)}"


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http_ready(url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if 200 <= resp.status < 500:
                    return True
        except Exception:
            time.sleep(0.03)
    return False


def _check_integration(
    spec: dict, work: Path, grader_relpath: str, tmp: Path,
) -> tuple[bool, str] | None:
    """Run hidden integration cases against an ephemeral local subprocess.

    Each case can select a hidden pytest expression and inject environment into
    the server. Candidate tests receive WB_BASE_URL and WB_SCENARIO. The server
    is always terminated in a finally block and the aggregate wall time is a
    hard gate.
    """
    cfg = spec.get("integration")
    if not isinstance(cfg, dict):
        return None
    spawn_cmd = list(cfg.get("spawn_cmd") or [])
    cases = list(cfg.get("cases") or [])
    if not spawn_cmd or not cases:
        return False, "integration needs spawn_cmd and cases"
    ready_path = str(cfg.get("ready_path") or "/healthz")
    wall_budget = float(cfg.get("wall_budget_s", 60))
    started = time.monotonic()
    passed = 0
    for idx, raw_case in enumerate(cases):
        case = dict(raw_case)
        port = _free_tcp_port()
        base_url = f"http://127.0.0.1:{port}"
        cmd = _format_cmd(spawn_cmd, port=port, base_url=base_url)
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (case.get("server_env") or {}).items()})
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(work), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
            )
            if not _wait_http_ready(base_url + ready_path, float(cfg.get("ready_timeout_s", 4))):
                return False, f"integration case {idx} server not ready"
            extra = ["-k", str(case.get("pytest_k") or "integration")]
            case_env = {
                "WB_BASE_URL": base_url,
                "WB_SCENARIO": str((case.get("server_env") or {}).get("SCENARIO", "")),
            }
            rc, tail, _stats = run_pytest(
                [grader_relpath], work, tmp / f"j_integration_{idx}.xml",
                extra_args=extra, env_extra=case_env,
                timeout=int(cfg.get("pytest_timeout_s", 90)),
            )
            if rc != 0:
                return False, f"integration case {idx} failed rc={rc}: {tail[:120]}"
            passed += 1
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        if time.monotonic() - started > wall_budget:
            return False, f"integration wall budget exceeded {wall_budget:.1f}s"
    elapsed = time.monotonic() - started
    return True, f"integration cases {passed}/{len(cases)} in {elapsed:.2f}s <= {wall_budget:.1f}s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="practical-bench offline grader")
    tasks_available = sorted(p.parent.name for p in TASKS_DIR.glob("*/task.json"))
    parser.add_argument("--task", required=True, choices=tasks_available)
    parser.add_argument("--ws", required=True, type=Path,
                        help="workspace (candidate solution) directory to grade")
    parser.add_argument("--json", action="store_true",
                        help="also print one JSON result line at the end")
    args = parser.parse_args(argv)

    task_dir = TASKS_DIR / args.task
    spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    fixture_dir = task_dir / "fixture"
    grader_file = GRADER_SUITES_DIR / f"grader_check_{args.task}.py"
    ws: Path = args.ws.resolve()

    checks: list[dict] = []

    def add(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    with tempfile.TemporaryDirectory(prefix="wb-practical-grade-") as raw_tmp:
        tmp = Path(raw_tmp)

        # -- 1a. baseline: pristine fixture + visible tests -------------------
        b_vis = copy_tree(fixture_dir, tmp, "baseline_visible")
        exp_vis = spec.get("baseline", {}).get("pytest_expect", "fail")
        rc, _tail, st = run_pytest(["tests"], b_vis, tmp / "j_baseline_visible.xml")
        ok = (rc != 0) if exp_vis == "fail" else (rc == 0)
        det = f"expect {exp_vis}; got rc={rc}" + (f"; {st.describe()}" if st else "")
        add("baseline_visible_expectation", ok, det)

        # -- 1b. baseline: pristine fixture + hidden grader suite -------------
        b_grad = copy_tree(fixture_dir, tmp, "baseline_grader")
        gdir = b_grad / GRADER_SUBDIR
        gdir.mkdir()
        shutil.copy2(grader_file, gdir / grader_file.name)
        rc, _tail, st = run_pytest([str(gdir / grader_file.name)], b_grad,
                                  tmp / "j_baseline_grader.xml")
        add("baseline_gradersuite_red", rc != 0,
            f"hidden suite must fail on pristine fixture; got rc={rc}"
            + (f"; {st.describe()}" if st else ""))

        # -- 2. workspace presence / required files ---------------------------
        if not ws.is_dir():
            add("required_files_present", False, f"workspace not found: {ws}")
            missing_all = True
        else:
            missing = [f for f in spec.get("required_files", [])
                       if not (ws / f).exists()]
            missing_all = False
            add("required_files_present", not missing,
                "all present" if not missing else f"missing: {', '.join(missing)}")

        if ws.is_dir():
            regression = _check_model_regression(spec, fixture_dir, ws, tmp)
            if regression is not None:
                add("model_regression_red_green", regression[0], regression[1])
            history = _check_git_history(spec, ws, tmp)
            if history is not None:
                add("git_history", history[0], history[1])
            confinement = _check_diff_confinement(spec, fixture_dir, ws)
            if confinement is not None:
                add("diff_confinement", confinement[0], confinement[1])
            locked = _check_locked_paths(spec, fixture_dir, ws)
            if locked is not None:
                add("locked_paths_unchanged", locked[0], locked[1])

        main_stats: SuiteStats | None = None
        proceed = all(c["ok"] is not False for c in checks)
        if not missing_all and proceed:
            # -- 3. main run: visible tests + injected hidden grader suite ----
            grader_relpath = str(Path(GRADER_SUBDIR) / grader_file.name)
            work = copy_tree(ws, tmp, "work")
            gdir = work / GRADER_SUBDIR
            gdir.mkdir(exist_ok=True)
            shutil.copy2(grader_file, gdir / grader_file.name)
            rc, _tail, main_stats = run_pytest(
                ["tests", grader_relpath], work, tmp / "j_main.xml")
            ok = (
                rc == 0
                and main_stats is not None
                and main_stats.failed == 0
                and main_stats.errors == 0
                and main_stats.skipped == 0
                and main_stats.grader_passed >= spec.get("min_grader_tests_passed", 1)
                and main_stats.visible_passed >= spec.get("min_visible_tests_passed", 1)
            )
            if main_stats is None:
                det = f"pytest produced no report (rc={rc})"
            else:
                det = main_stats.describe()
                det += (f"; mins visible>="
                        f"{spec.get('min_visible_tests_passed', 1)} "
                        f"grader>={spec.get('min_grader_tests_passed', 1)}")
            add("suite_green_with_hidden_assertions", ok, det)

            # -- 4. structural checks -----------------------------------------
            for s in spec.get("structural_checks", []):
                src_path = work / s["file"]
                try:
                    src = src_path.read_text(encoding="utf-8")
                    ok = bool(re.search(s["regex"], src))
                    det = f"{s['file']} matches {s['regex']!r}: {ok}"
                except OSError as exc:
                    ok = False
                    det = f"{s['file']} unreadable: {exc}"
                add(f"structural:{Path(s['file']).name}", ok, det)

            strength = _run_test_strength(spec, work, grader_relpath, tmp)
            if strength is not None:
                add("test_strength", strength[0], strength[1])

            heldout = _check_heldout_corpus(spec, work, tmp)
            if heldout is not None:
                add("heldout_corpus", heldout[0], heldout[1])

            integration = _check_integration(spec, work, grader_relpath, tmp)
            if integration is not None:
                add("integration", integration[0], integration[1])

            # -- 5. mutation checks -------------------------------------------
            for m in spec.get("mutations", []):
                mcopy = copy_tree(work, tmp, f"mut_{m['id']}")
                plugdir = mcopy / "_wb_mutation"
                plugdir.mkdir()
                (plugdir / f"{MUTATION_PLUGIN_NAME}.py").write_text(
                    MUTATION_PLUGIN_SOURCE, encoding="utf-8")
                cfg = {"package": spec["entry_package"],
                       "patches": [{"kind": m["kind"], "attr": m["attr"]}]}
                pypath = os.environ.get("PYTHONPATH", "")
                env = {
                    "WB_MUTATION": json.dumps(cfg),
                    "PYTHONPATH": str(plugdir) + (os.pathsep + pypath if pypath else ""),
                }
                junit_m = tmp / f"j_mut_{m['id']}.xml"
                rc, _tail, st = run_pytest(
                    ["tests", grader_relpath],
                    mcopy, junit_m,
                    extra_args=["-p", MUTATION_PLUGIN_NAME], env_extra=env)
                caught = rc != 0
                det = ("caught by suite" if caught else "SURVIVED -- tests pass "
                       "on deliberately broken code") + \
                      (f" ({st.describe()})" if st else f" (rc={rc})")
                add(f"mutation:{m['id']}", caught, det)

    # -- render ---------------------------------------------------------------
    passed_n = sum(1 for c in checks if c["ok"] is True)
    total_n = len(checks)
    verdict = "PASS" if total_n > 0 and passed_n == total_n else "FAIL"
    line = "=" * 66
    print(line)
    print(f"practical-bench grader | task={args.task} | ws={ws}")
    print(line)
    for c in checks:
        mark = {True: "PASS", False: "FAIL", None: "SKIP"}[c["ok"]]
        print(f"  [{mark}] {c['name']:<34} {c['detail']}")
    print("-" * 66)
    print(f"RESULT: {verdict}  (checks {passed_n}/{total_n})")
    print(line)
    if args.json:
        print(json.dumps({"task": args.task, "ws": str(ws), "result": verdict,
                          "passed": passed_n, "total": total_n, "checks": checks}))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
