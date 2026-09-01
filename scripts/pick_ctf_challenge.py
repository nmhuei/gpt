#!/usr/bin/env python3
"""Pick CTF challenges suitable as verify-loop test subjects.

Scans a CTF library tree (default ~/Workspace/CTF), classifies every
"challenge directory", rejects ones already solved or flagged for human
review, and emits a ranked JSON list of usable candidates.

Challenge-dir detection (leaf-most wins):
  - a directory containing metadata.json, a README*, or >= 1 attachment
    file (any regular file that is not a known solve/tooling artifact),
    AND none of whose descendant directories are themselves marked.

Acceptance criteria:
  REJECT if flag.txt exists anywhere inside the challenge dir   (solved)
  REJECT if NEEDS_HUMAN_REVIEW.md exists                        (needs human)
  REJECT if no clear statement (empty metadata description AND empty README)
  ACCEPT otherwise when solvable locally (>= 1 attachment) OR it has a
         remote URL (metadata connection_info / raw.urls); remote liveness
         probed opportunistically (HEAD, GET fallback, 3s timeout).

NEEDS_REMOTE heuristic: candidates whose sources carry a redacted flag
(REDACTED), whose metadata says connection_info: null on a web challenge,
or whose statement tells you to connect to a remote host are tagged
needs_remote and excluded from the fresh list by default; pass
--include-remote to run them anyway.

v3 additions (blind-spot fixes, see docs/reports/picker-v3-2026-08-26.md):
  - flag-in-docker detection: Dockerfile / docker-compose / .env files are
    scanned for runtime flag injection (ENV/ARG FLAG, COPY/ADD of flag
    material, compose env entries) and metadata.json container hints
    (instance_info.is_container / DynamicContainer tag, e.g. PTIT gzctf);
    such challenges can only yield the real flag from a live instance =>
    classified needs_remote.
  - REDACTED-in-archive detection extended beyond plain .zip to tar/tar.gz/
    tar.bz2/tar.xz and to one level of nested archives (zip-in-zip,
    tar-in-zip, ...), with hard decompressed-size budgets (anti zip-bomb).
  - --unmark PATH removes one used_at entry from the sidecar state
    (--unmark-reason TEXT is logged to stdout alongside it).

Used-challenge state: sidecar JSON (default scripts/.ctf_used_challenges.json).
Mark with --mark-used PATH (or --mark-used ALL against the last output);
candidates already used are excluded unless --include-used.

Usage:
  pick_ctf_challenge.py [--root DIR] [--output FILE] [--probe-remote N]
                        [--min-count N] [--max-depth D]
                        [--used-file FILE] [--include-used]
                        [--mark-used PATH|ALL] [--include-remote]
                        [--max-risk low|medium|high]
                        [--unmark PATH] [--unmark-reason TEXT]
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import re
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path.home() / "Workspace" / "CTF"
DEFAULT_USED_FILE = Path(__file__).resolve().parent / ".ctf_used_challenges.json"
PROBE_TIMEOUT_S = 3

SKIP_DIR_NAMES = {
    "__pycache__", ".git", "node_modules", "venv", ".venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
}

# Files that look like our own solve/tooling output, never "attachments".
ARTIFACT_RE = re.compile(
    r"""^(
        solve.*|exploit.*|test_.*|debug.*|gdb.*|trace.*|input\.bin|payload.*
      | .*\.pyc | flag\.txt | core(\.\d+)? | disasm.* | decompiled.*
      | .*\.log | notes?\.(txt|md) | TODO.*
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

ATTACHMENT_HINT_RE = re.compile(
    r"""\.(zip|tar|gz|tgz|bz2|xz|7z|rar|pcap|pcapng|pdf|png|jpg|jpeg|gif|
          bmp|wav|mp3|mp4|bin|elf|exe|dll|so|dex|apk|img|iso|vhd|raw|db|
          sqlite|sav|nes|gb|rsa|pem|enc|cipher|out|desc|json|csv|txt)\Z""",
    re.IGNORECASE | re.VERBOSE,
)

CATEGORY_MAP = [
    ("osint", "osint"), ("forensic", "forensics"),
    ("rev", "rev"), ("reverse", "rev"), ("reversing", "rev"),
    ("crypto", "crypto"), ("pwn", "pwn"), ("web", "web"),
    ("misc", "misc"), ("jail", "jail"), ("sandbox", "jail"),
    ("boot2root", "boot2root"), ("onboarding", "onboarding"),
    ("warmup", "warmup"), ("blockchain", "blockchain"),
    ("stego", "stego"), ("network", "network"),
]


def is_artifact(name: str) -> bool:
    return bool(ARTIFACT_RE.match(name))


MARKER_FILES = {"metadata.json", "flag.txt"}

def looks_like_attachment(path: Path) -> bool:
    """Heuristic: a regular file we did not generate ourselves."""
    name = path.name
    if is_artifact(name) or name in MARKER_FILES:
        return False
    if path.suffix == ".py":
        return False  # python files here are almost always solve scripts
    if ATTACHMENT_HINT_RE.search(name):
        return True
    # extensionless executable-ish blobs (binaries) count as attachments
    return path.suffix == "" and path.is_file() and not name.startswith(".")


def iter_dirs(root: Path, max_depth: int):
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        yield d, entries
        if depth < max_depth:
            for e in entries:
                if e.is_dir() and e.name not in SKIP_DIR_NAMES:
                    stack.append((e, depth + 1))


def find_challenge_dirs(root: Path, max_depth: int = 4) -> list[Path]:
    """Return leaf-most directories that carry challenge markers."""
    markers: dict[Path, bool] = {}
    for d, entries in iter_dirs(root, max_depth):
        names = {e.name for e in entries}
        has_meta = "metadata.json" in names
        has_readme = any(n.upper().startswith("README") for n in names)
        has_attach = any(looks_like_attachment(e) for e in entries if e.is_file())
        if has_meta or has_readme or has_attach:
            markers[d.resolve()] = True

    def contains_descendant_marker(d: Path) -> bool:
        try:
            children = [p for p in d.rglob("*") if p.is_dir()]
        except OSError:
            return False
        return any(c.resolve() in markers for c in children)

    return sorted(d for d in markers if not contains_descendant_marker(d))


def has_flag_txt(chal: Path) -> bool:
    try:
        return next(chal.rglob("flag.txt"), None) is not None
    except OSError:
        pass
    return False


def read_text_safe(p: Path, limit: int = 65536) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def load_metadata(chal: Path) -> dict[str, Any]:
    meta_path = chal / "metadata.json"
    if meta_path.is_file():
        try:
            data = json.loads(read_text_safe(meta_path))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {}


def _raw_meta(meta: dict[str, Any]) -> dict[str, Any]:
    raw = meta.get("raw")
    return raw if isinstance(raw, dict) else {}


def extract_urls(meta: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    ci = meta.get("connection_info")
    ci_items = ci if isinstance(ci, list) else ([ci] if isinstance(ci, str) else [])
    raw = _raw_meta(meta)
    ru = raw.get("urls")
    ru_items = ru if isinstance(ru, list) else ([ru] if isinstance(ru, str) else [])
    for item in [*ci_items, *ru_items]:
        for m in re.findall(r"https?://[^\s'\")]+", str(item)):
            u = m.rstrip(".,;")
            if u not in urls:
                urls.append(u)
    return urls


def categorize(meta: dict[str, Any], chal: Path) -> str:
    raw = _raw_meta(meta)
    for src in (meta.get("category"), raw.get("category"), chal.parent.name):
        s = str(src or "").strip().lower().replace("-", "").replace("_", "")
        if not s:
            continue
        for key, cat in CATEGORY_MAP:
            if key in s:
                return cat
    return "unknown"


def guess_difficulty(meta: dict[str, Any]) -> str:
    raw = _raw_meta(meta)
    diff = str(raw.get("difficulty") or meta.get("difficulty") or "").strip().lower()
    if diff in ("easy", "medium", "hard"):
        return diff
    try:
        pts = int(raw.get("points") or meta.get("points") or 0)
    except (TypeError, ValueError):
        pts = 0
    if pts <= 0:
        return "unknown"
    if pts < 200:
        return "easy"
    if pts < 400:
        return "medium"
    return "hard"


def description_excerpt(meta: dict[str, Any], chal: Path, limit: int = 200) -> str:
    raw = _raw_meta(meta)
    text = str(raw.get("description") or meta.get("description") or "").strip()
    if not text:
        readme = next((chal / n for n in sorted(p.name for p in chal.glob("README*"))
                       ), None)
        if readme is None:
            candidates = sorted(chal.glob("*"), key=lambda p: p.name.lower())
            readme = next((c for c in candidates
                           if c.is_file() and c.name.upper().startswith("README")), None)
        if readme is not None:
            lines = [ln.strip() for ln in read_text_safe(readme).splitlines()]
            body = [ln for ln in lines
                    if ln and not ln.startswith(("#", "<", "!")) ]
            text = " ".join(body[:8])
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def probe_url(url: str, timeout: int = PROBE_TIMEOUT_S,
              _opener=None) -> bool:
    """HEAD then GET fallback. Returns True only on an HTTP response."""
    opener = _opener or urllib.request.urlopen

    def _try(method: str) -> bool:
        req = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": "ctf-picker/1.0"})
        try:
            with opener(req, timeout=timeout) as resp:
                resp.read(64)
                return True
        except Exception:
            return False

    return _try("HEAD") or _try("GET")


# ------------------------------------------------- needs_remote heuristics

REDACTED_RE = re.compile(r"REDACTED")
REMOTE_HINT_RE = re.compile(
    r"\bconnect to\b|\bnc\s+\S+\s+\d+|\bremote (?:host|server|instance)\b",
    re.IGNORECASE,
)

SCAN_MAX_FILES = 200
SCAN_MAX_BYTES = 1 << 20

# --------------------------------------------------- nested-archive scanning

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz",
                    ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
TEXT_ENTRY_SUFFIXES = (".txt", ".md")
NESTED_MAX_DEPTH = 1                    # archive-in-archive, one level only
TOP_LEVEL_ARCHIVE_MAX_BYTES = 64 << 20   # never slurp attachments beyond this
NESTED_ARCHIVE_MAX_BYTES = 8 << 20       # max inner-archive payload in RAM
# total decompressed bytes allowed per top-level archive (zip-bomb guard):
ARCHIVE_BUDGET_BYTES = 16 << 20


def _text_entry_name(base: str) -> bool:
    b = base.upper()
    return b.startswith("README") or base.lower().endswith(TEXT_ENTRY_SUFFIXES)


def _looks_like_archive_name(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def _member_hit(payload: bytes, name: str, prefix: str, nested: bool,
                depth: int, budget: list[int]) -> tuple[str, str] | None:
    """Handle one extracted member: recurse into nested archives or grep."""
    if nested:
        return _scan_archive_data(payload, f"{prefix}/{name}", depth + 1, budget)
    if b"\x00" in payload[:8192]:
        return None                      # binary entry, not a text blob
    base = name.rsplit("/", 1)[-1]
    text = payload.decode("utf-8", errors="replace")
    if _text_entry_name(base) and REDACTED_RE.search(text):
        return "redacted", f"{prefix}:{name}"
    if _is_dockerish_file(base):
        line = _docker_flag_line(text)
        if line:
            return "docker", f"{prefix}:{name}: {line}"
    return None


def _entry_worth_scanning(name: str, nested: bool) -> bool:
    base = name.rsplit("/", 1)[-1]
    return (_text_entry_name(base) or _is_dockerish_file(base)
            or (nested and _looks_like_archive_name(name)))


def _scan_zip_data(data: bytes, prefix: str, depth: int,
                   budget: list[int]) -> tuple[str, str] | None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return None                      # corrupt/unsupported -> no signal
    with zf:
        for info in zf.infolist()[:SCAN_MAX_FILES]:
            if budget[0] <= 0:
                break                    # decompression budget exhausted
            name = info.filename
            nested = depth < NESTED_MAX_DEPTH and _looks_like_archive_name(name)
            if not _entry_worth_scanning(name, nested):
                continue
            if info.flag_bits & 0x1:     # encrypted entry -> skip
                continue
            want = min(budget[0], NESTED_ARCHIVE_MAX_BYTES if nested
                       else SCAN_MAX_BYTES)
            try:
                with zf.open(info) as fh:
                    payload = fh.read(want)
            except Exception:
                continue                 # CRC/IO error -> ignore this entry
            budget[0] -= len(payload)
            hit = _member_hit(payload, name, prefix, nested, depth, budget)
            if hit:
                return hit
    return None


def _scan_tar_data(data: bytes, prefix: str, depth: int,
                   budget: list[int]) -> tuple[str, str] | None:
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")  # noqa: SIM115 -- corrupt archive is handled before entering context
    except Exception:
        return None
    with tf:
        try:
            members = tf.getmembers()[:SCAN_MAX_FILES]
        except Exception:
            return None
        for m in members:
            if budget[0] <= 0:
                break
            if not m.isfile():
                continue
            nested = depth < NESTED_MAX_DEPTH and _looks_like_archive_name(m.name)
            if not _entry_worth_scanning(m.name, nested):
                continue
            want = min(budget[0], NESTED_ARCHIVE_MAX_BYTES if nested
                       else SCAN_MAX_BYTES)
            try:
                fh = tf.extractfile(m)
                payload = fh.read(want) if fh is not None else b""
            except Exception:
                continue
            budget[0] -= len(payload)
            hit = _member_hit(payload, m.name, prefix, nested, depth, budget)
            if hit:
                return hit
    return None


def _scan_archive_data(data: bytes, prefix: str, depth: int,
                       budget: list[int]) -> tuple[str, str] | None:
    """Dispatch on magic bytes; every read is capped by budget[0].

    Returns ("redacted"|"docker", label) for the first signal found.
    """
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _scan_zip_data(data, prefix, depth, budget)
    return _scan_tar_data(data, prefix, depth, budget)


def _archive_redacted_entry(chal: Path) -> tuple[str, str] | None:
    """Scan zip/tar attachments for hidden needs_remote evidence.

    Extends the old zip-only scan to tar/tar.gz/tar.bz2/tar.xz and to one
    level of nested archives (zip-in-zip, tar-in-zip, ...). Two signals:
      ("redacted", "<archive>:<entry>")  - REDACTED flag inside a text entry
      ("docker",   "<archive>:<entry>: <line>") - container definition
        (Dockerfile/compose/.env) inside the archive injecting the flag at
        runtime (PTIT DynamicContainer pattern)
    Hard size budgets keep hostile (zip-bomb) archives safe: per-entry reads
    are capped by SCAN_MAX_BYTES / NESTED_ARCHIVE_MAX_BYTES and the total
    decompressed volume per top-level archive by ARCHIVE_BUDGET_BYTES;
    oversized top-level files are skipped entirely.

    Corrupt, truncated, or encrypted entries are skipped safely (never
    crash the picker). Returns None when no signal is present.
    """
    try:
        archives = sorted(p for p in chal.rglob("*") if p.is_file()
                          and _looks_like_archive_name(p.name))
    except OSError:
        return None
    scanned = 0
    for apath in archives:
        if scanned >= SCAN_MAX_FILES:
            break
        scanned += 1
        try:
            if apath.stat().st_size > TOP_LEVEL_ARCHIVE_MAX_BYTES:
                continue
            data = apath.read_bytes()
        except OSError:
            continue
        budget = [ARCHIVE_BUDGET_BYTES]
        hit = _scan_archive_data(data, apath.name, 0, budget)
        if hit:
            return hit
    return None


# ------------------------------------------------------ flag-in-docker scan

DOCKER_SCAN_MAX_FILES = 50
DOCKER_SCAN_MAX_BYTES = 256 << 10

# Dockerfile plumbing keyed on names/paths only (ENV/ARG FLAG, COPY/ADD of
# flag-material files):
DOCKERFILE_FLAG_RE = re.compile(
    r"""\s*ENV\s+[A-Za-z0-9_]*FLAG[A-Za-z0-9_]*(\s*=|\s|$)
      | \s*ARG\s+[A-Za-z0-9_]*FLAG[A-Za-z0-9_]*(\s*=|\s|$)
      | \s*(?:COPY|ADD)\s+(?:--[^\s]+\s+)*[^\s#]*flag[^\s#]*
    """,
    re.IGNORECASE | re.VERBOSE,
)
# compose/.env assignments additionally require a flag-shaped or
# placeholder VALUE, so benign vars like FLAG_SERVICE_URL do not match:
COMPOSE_ENV_FLAG_RE = re.compile(
    r"""\s*-\s*[A-Za-z0-9_]*FLAG[A-Za-z0-9_]*\s*[=:]\s*\S
      | \s*[A-Za-z0-9_]*FLAG[A-Za-z0-9_]*\s*:\s*\S
      | \s*[A-Za-z0-9_]*FLAG[A-Za-z0-9_]*=\s*\S
    """,
    re.IGNORECASE | re.VERBOSE,
)
PLACEHOLDER_HINT_RE = re.compile(
    r"REDACTED|FAKE|TEST|CHANGE_?ME|PLACEHOLDER|EXAMPLE", re.IGNORECASE)


def _env_value_flaglike(line: str) -> bool:
    m = re.search(r"[=:]\s*(.*)$", line)
    if not m:
        return False
    val = m.group(1).strip().strip("\"'")
    if "{" in val and "}" in val:
        return True              # flag-format placeholder, e.g. PTITCTF{...}
    return bool(PLACEHOLDER_HINT_RE.search(val))


def _is_dockerish_file(name: str) -> bool:
    low = name.lower()
    if low == "dockerfile" or low.startswith("dockerfile.") \
            or low.endswith(".dockerfile"):
        return True
    if low.startswith(("docker-compose.", "compose.")) \
            and low.endswith((".yml", ".yaml")):
        return True
    return low == ".env" or low.startswith(".env.")


def _docker_flag_line(text: str) -> str | None:
    """First line of `text` matching a runtime-flag-injection pattern."""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if DOCKERFILE_FLAG_RE.search(line):
            return line.strip()[:80]
        if COMPOSE_ENV_FLAG_RE.search(line) and _env_value_flaglike(line):
            return line.strip()[:80]
    return None


def _docker_flag_signal(chal: Path) -> str | None:
    """Detect runtime flag injection in Dockerfile/docker-compose/.env.

    PTIT-style DynamicContainer challenges ship the container definition but
    inject the real flag when the instance starts, so nothing local can
    yield it => such a challenge is needs_remote.
    """
    try:
        hits = sorted(p for p in chal.rglob("*")
                      if p.is_file() and _is_dockerish_file(p.name))
    except OSError:
        return None
    for p in hits[:DOCKER_SCAN_MAX_FILES]:
        try:
            if p.stat().st_size > DOCKER_SCAN_MAX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        line = _docker_flag_line(text)
        if line:
            rel = p.relative_to(chal)
            return f"flag_in_docker({rel}: {line})"
    return None


def _metadata_container_signal(meta: dict[str, Any]) -> str | None:
    """metadata.json hints that the flag lives in a started container."""
    inst = meta.get("instance_info")
    if isinstance(inst, dict) and inst.get("is_container"):
        return "dynamic_container_metadata(instance_info.is_container)"
    raw = _raw_meta(meta)
    tags: list[str] = []
    for src in (meta.get("tags"), raw.get("tags")):
        if isinstance(src, str):
            tags.append(src)
        elif isinstance(src, list):
            tags.extend(str(t) for t in src)
    if any("dynamiccontainer" in t.replace("_", "").replace("-", "").lower()
           for t in tags):
        return "dynamic_container_metadata(tags)"
    return None


def _scannable_texts(chal: Path):
    """Yield (path, text) for small text-ish files under the challenge dir.

    Binary blobs (NUL byte in first 8 KiB) and oversized files are skipped.
    """
    count = 0
    try:
        for p in sorted(chal.rglob("*")):
            if count >= SCAN_MAX_FILES:
                return
            if not p.is_file():
                continue
            try:
                if p.stat().st_size > SCAN_MAX_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            count += 1
            yield p, data.decode("utf-8", errors="replace")
    except OSError:
        return


def detect_needs_remote(chal: Path, meta: dict, excerpt: str) -> tuple[bool, str]:
    """Grep-based NEEDS_REMOTE detection.

    Returns (needs_remote, reason). Signals:
      1. a shipped source file contains a REDACTED flag placeholder
         (the real flag only exists on the remote instance);
      1b. a REDACTED flag hides inside an archive attachment's text entries,
         or a Dockerfile/compose/.env entry inside the archive injects the
         flag at container runtime (plain grep over the tree cannot see
         either) — covers .zip and tar* plus one level of nesting;
      1c. a Dockerfile / docker-compose / .env file injects the flag at
         container runtime (ENV/ARG FLAG, COPY of flag material, compose
         env entries);
      1d. metadata.json marks the challenge as container-backed
         (instance_info.is_container or DynamicContainer tag);
      2. metadata has connection_info: null on a web challenge;
      3. the statement tells you to connect to / nc to a remote host.
    """
    # 1. redacted flag baked into shipped sources
    for p, text in _scannable_texts(chal):
        if REDACTED_RE.search(text):
            return True, f"redacted_flag_in_source({p.name}: contains REDACTED)"

    # 1b. redacted flag or runtime-flag container definition hidden inside
    #     an archive attachment (plain grep over the tree cannot see it)
    hit = _archive_redacted_entry(chal)
    if hit:
        kind, label = hit
        if kind == "docker":
            return True, f"flag_in_docker({label})"
        return True, f"redacted_flag_in_archive({label})"

    # 1c. flag injected by the container image at runtime
    docker_hit = _docker_flag_signal(chal)
    if docker_hit:
        return True, docker_hit

    # 1d. metadata says a live container instance holds the flag
    meta_hit = _metadata_container_signal(meta)
    if meta_hit:
        return True, meta_hit

    # 2. explicit null connection_info on a web challenge
    raw = _raw_meta(meta)
    category = categorize(meta, chal)
    if ("connection_info" in meta and meta["connection_info"] is None
            and category == "web"):
        return True, "connection_info_null(web)"

    # 3. statement mentions connecting to something remote
    blob = " ".join([str(raw.get("description") or ""),
                     str(meta.get("description") or ""), excerpt])
    m = REMOTE_HINT_RE.search(blob)
    if m:
        return True, f"remote_hint_in_statement({m.group(0)!r})"

    return False, ""



# ------------------------------------------------ classifier-risk ranking

RISK_RANK = {"low": 0, "medium": 1, "high": 2}
HIGH_RISK_BINARY_SUFFIXES = {".apk", ".dex", ".exe", ".dll", ".elf"}
HIGH_RISK_CATEGORIES = {"pwn", "jail"}
MEDIUM_RISK_CATEGORIES = {"rev", "web", "boot2root"}
LOW_RISK_CATEGORIES = {"crypto", "osint", "forensics", "stego", "network"}


def classifier_risk(category: str, attachments: list[str], title: str, excerpt: str) -> tuple[str, int, list[str]]:
    """Estimate false-positive classifier risk from category and local artifacts.

    This is prioritisation only: it never changes the authorized CTF task or
    attempts to bypass a refusal. Lower-risk local challenges are simply tried
    first so automation spends fewer turns on categories empirically prone to
    classifier blocks.
    """
    cat = (category or "unknown").lower()
    suffixes = {Path(name).suffix.lower() for name in attachments}
    blob = f"{title} {excerpt}".casefold()
    signals: list[str] = []
    score = 0
    if cat in HIGH_RISK_CATEGORIES:
        score += 4
        signals.append(f"category:{cat}")
    elif cat in MEDIUM_RISK_CATEGORIES:
        score += 2
        signals.append(f"category:{cat}")
    elif cat in LOW_RISK_CATEGORIES:
        signals.append(f"category:{cat}:low")
    if suffixes & HIGH_RISK_BINARY_SUFFIXES:
        score += 4
        signals.append("binary:" + ",".join(sorted(suffixes & HIGH_RISK_BINARY_SUFFIXES)))
    elif cat == "rev" and attachments:
        score += 1
        signals.append("rev:nonbinary")
    if any(word in blob for word in ("android", "mobile", "apk", "exploit", "shellcode", "binary exploitation")):
        score += 2
        signals.append("statement:risky-keyword")
    tier = "high" if score >= 4 else ("medium" if score >= 2 else "low")
    return tier, score, signals

# ---------------------------------------------------------------- evaluation

def evaluate_challenge(chal: Path) -> tuple[dict | None, str]:
    """Returns (candidate_dict_or_None, rejection_reason_if_any)."""
    meta = load_metadata(chal)
    if has_flag_txt(chal):
        return None, "already_solved(flag.txt)"
    if (chal / "NEEDS_HUMAN_REVIEW.md").is_file():
        return None, "needs_human_review"

    attachments = sorted(p.name for p in chal.iterdir()
                         if p.is_file() and looks_like_attachment(p))
    urls = extract_urls(meta)
    excerpt = description_excerpt(meta, chal)

    if not excerpt:
        return None, "unclear_statement(empty description & README)"

    if not attachments and not urls:
        return None, "not_solvable(no local attachments, no remote url)"

    needs_remote, remote_reason = detect_needs_remote(chal, meta, excerpt)

    name = str(meta.get("name") or raw_name(meta) or chal.name)
    category = categorize(meta, chal)
    risk_tier, risk_score, risk_signals = classifier_risk(category, attachments, name, excerpt)
    cand = {
        "path": str(chal),
        "name": name,
        "category": category,
        "classifier_risk": risk_tier,
        "classifier_risk_score": risk_score,
        "classifier_risk_signals": risk_signals,
        "has_local_files": bool(attachments),
        "attachments": attachments[:10],
        "remote_url": urls[0] if urls else None,
        "remote_urls": urls,
        "remote_alive": None,       # filled later if budget allows
        "description_excerpt": excerpt,
        "est_difficulty_guess": guess_difficulty(meta),
        "points": safe_int(meta.get("points")
                           or _raw_meta(meta).get("points")),
        "solves_count": safe_int(meta.get("solves_count")
                                  or _raw_meta(meta).get("solves")),
        "needs_remote": needs_remote,
        "remote_reason": remote_reason if needs_remote else None,
        "used_at": None,
    }
    return cand, ""


def raw_name(meta: dict[str, Any]) -> str:
    raw = _raw_meta(meta)
    return str(raw.get("title") or "")


def safe_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def ease_score(cand: dict) -> tuple:
    diff_rank = {"easy": 0, "medium": 1, "hard": 2}.get(
        cand["est_difficulty_guess"], 3)
    # locally solvable first, then fewer/no remote dependence, low points,
    # high solves => likely quick wins
    return (
        RISK_RANK.get(cand.get("classifier_risk", "high"), 2),
        0 if cand["has_local_files"] else 1,
        diff_rank,
        cand["points"] if cand["points"] is not None else 10**9,
        -(cand["solves_count"] or 0),
    )


# ------------------------------------------------------------------ state io

def load_used_state(used_file: Path) -> dict:
    try:
        data = json.loads(read_text_safe(used_file))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def mark_used(path_key: str, used_file: Path) -> None:
    state = load_used_state(used_file)
    state[path_key] = datetime.datetime.now().isoformat(timespec="seconds")
    used_file.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")


def unmark_path(path_key: str, used_file: Path,
                reason: str = "manual unmark") -> int:
    """Remove one used_at entry; logs what happened (and why) to stdout.

    Returns 0 on success, 1 when the path has no entry in the sidecar.
    """
    key = str(Path(path_key).expanduser().resolve())
    state = load_used_state(used_file)
    if key not in state:
        print(f"[!] cannot unmark {key}: no used_at entry in {used_file} "
              f"(requested reason: {reason})")
        return 1
    removed_at = state.pop(key)
    used_file.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    print(f"[+] unmarked {key} (was used_at={removed_at}) "
          f"reason: {reason} -> {used_file}")
    return 0


# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--probe-remote", type=int, default=0,
                    help="max network probes (0 = no probing)")
    ap.add_argument("--min-count", type=int, default=0,
                    help="exit non-zero if fewer candidates survive")
    ap.add_argument("--max-depth", type=int, default=4)
    ap.add_argument("--used-file", type=Path, default=DEFAULT_USED_FILE)
    ap.add_argument("--include-used", action="store_true")
    ap.add_argument("--include-remote", action="store_true",
                    help="keep NEEDS_REMOTE candidates (default: excluded)")
    ap.add_argument("--max-risk", choices=("low", "medium", "high"), default="high",
                    help="exclude candidates above this classifier-risk tier")
    ap.add_argument("--mark-used", default=None, metavar="PATH|ALL",
                    help="record PATH (or ALL = every current candidate) "
                         "in the used-state file")
    ap.add_argument("--unmark", default=None, metavar="PATH",
                    help="remove PATH's used_at entry from the used-state "
                         "file (performs only this action, then exits)")
    ap.add_argument("--unmark-reason", default="manual unmark", metavar="TEXT",
                    help="reason logged to stdout together with --unmark")
    args = ap.parse_args(argv)

    if args.unmark is not None:
        return unmark_path(args.unmark, args.used_file, args.unmark_reason)

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"[!] root not a directory: {root}", file=sys.stderr)
        return 2

    all_dirs = find_challenge_dirs(root, args.max_depth)
    candidates, rejected = [], []
    for chal in all_dirs:
        cand, reason = evaluate_challenge(chal)
        if cand is None:
            rejected.append({"path": str(chal), "reason": reason})
        else:
            candidates.append(cand)

    used_state = load_used_state(args.used_file)
    fresh = []
    for c in candidates:
        ts = used_state.get(c["path"])
        c["used_at"] = ts
        if ts and not args.include_used:
            continue
        fresh.append(c)
    n_used_filtered = len(candidates) - len(fresh)

    # NEEDS_REMOTE candidates are dropped unless explicitly included
    remote_filtered: list[dict] = []
    if not args.include_remote:
        kept = []
        for c in fresh:
            if c.get("needs_remote"):
                remote_filtered.append(c)
            else:
                kept.append(c)
        fresh = kept
    risk_filtered: list[dict] = []
    max_risk_rank = RISK_RANK[args.max_risk]
    kept = []
    for c in fresh:
        if RISK_RANK.get(c.get("classifier_risk", "high"), 2) > max_risk_rank:
            risk_filtered.append(c)
        else:
            kept.append(c)
    fresh = kept

    remote_reason_counts: dict[str, int] = {}
    for c in remote_filtered:
        key = c.get("remote_reason") or "unknown"
        remote_reason_counts[key] = remote_reason_counts.get(key, 0) + 1

    fresh.sort(key=ease_score)

    # opportunistic remote probing within budget
    budget = max(args.probe_remote, 0)
    # probe remote-dependent candidates first so the budget goes where it matters
    probe_order = sorted(fresh, key=lambda c: c["has_local_files"])
    for c in probe_order:
        if budget <= 0:
            break
        if not c["remote_urls"]:
            continue
        c["remote_alive"] = any(probe_url(u) for u in c["remote_urls"][:3])
        budget -= 1
        time.sleep(0.05)

    if args.mark_used:
        targets = ([c["path"] for c in fresh] if args.mark_used.upper() == "ALL"
                   else [str(Path(args.mark_used).expanduser().resolve())])
        for t in targets:
            mark_used(t, args.used_file)
        print(f"[+] marked used: {len(targets)} -> {args.used_file}",
              file=sys.stderr)

    result: dict[str, Any] = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "stats": {
            "challenge_dirs_scanned": len(all_dirs),
            "accepted": len(candidates),
            "rejected": len(rejected),
            "rejected_by_reason": {},
            "used_filtered_out": n_used_filtered,
            "needs_remote_filtered_out": len(remote_filtered),
            "needs_remote_by_reason": remote_reason_counts,
            "risk_filtered_out": len(risk_filtered),
            "max_risk": args.max_risk,
            "candidates_final": len(fresh),
            "probes_performed": max(args.probe_remote, 0) - budget,
        },
        "candidates": fresh,
        "rejected": rejected,
    }
    for r in rejected:
        result["stats"]["rejected_by_reason"][r["reason"]] = \
            result["stats"]["rejected_by_reason"].get(r["reason"], 0) + 1

    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"[+] wrote {len(fresh)} candidates -> {args.output}",
              file=sys.stderr)

    summary = ", ".join(f"{k}={v}" for k, v in
                        result["stats"]["rejected_by_reason"].items())
    print(f"[i] scanned={len(all_dirs)} accepted={len(candidates)} "
          f"rejected={len(rejected)} ({summary}) "
          f"needs_remote_filtered={len(remote_filtered)} risk_filtered={len(risk_filtered)} "
          f"final={len(fresh)}",
          file=sys.stderr)
    if len(fresh) < args.min_count:
        print(f"[!] FAIL: only {len(fresh)} candidates, need "
              f"{args.min_count}", file=sys.stderr)
        return 1
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
