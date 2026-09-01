"""Tests for NEEDS_REMOTE filtering in scripts/pick_ctf_challenge.py.

Synthetic tmp trees only -- never scans ~/Workspace/CTF, never touches
the network.
"""

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "pick_ctf_challenge", SCRIPTS / "pick_ctf_challenge.py")
assert spec is not None and spec.loader is not None
pick = importlib.util.module_from_spec(spec)
sys.modules.setdefault("pick_ctf_challenge", pick)
spec.loader.exec_module(pick)


# --------------------------------------------------------------- fixtures

def build_tree(root: Path) -> None:
    # 1. clean local challenge -> accepted, never flagged needs_remote
    clean = root / "EventA" / "Misc" / "clean_local"
    clean.mkdir(parents=True)
    (clean / "README.md").write_text("# chall\n\nDecode the string.\n",
                                     encoding="utf-8")
    (clean / "metadata.json").write_text(json.dumps({
        "name": "clean_local", "category": "misc",
        "description": "Decode the string.",
    }), encoding="utf-8")
    (clean / "chall.zip").write_bytes(b"PK")

    # 2. redacted flag shipped in source -> NEEDS_REMOTE
    redact = root / "EventA" / "Web" / "fair_gambling"
    redact.mkdir(parents=True)
    (redact / "README.md").write_text("Exploit the casino app.\n",
                                      encoding="utf-8")
    (redact / "server.js").write_text(
        'const FLAG = "brunner{REDACTED}";\nconsole.log("hi");\n',
        encoding="utf-8")
    (redact / "app.zip").write_bytes(b"PK")

    # 3. web challenge with connection_info: null -> NEEDS_REMOTE
    webnull = root / "EventB" / "Web" / "shop_null_conn"
    webnull.mkdir(parents=True)
    (webnull / "metadata.json").write_text(json.dumps({
        "name": "shop_null_conn", "category": "web",
        "connection_info": None,
        "raw": {"title": "shop_null_conn",
                "description": "Break the cart logic."},
    }), encoding="utf-8")
    (webnull / "shop.zip").write_bytes(b"PK")


def run_pick(root: Path, *extra_args: str) -> tuple[int, dict]:
    buf = io.StringIO()
    argv = ["--root", str(root),
            "--used-file", str(root.parent / "used_state.json"),
            "--max-depth", "4", *extra_args]
    with contextlib.redirect_stdout(buf):
        rc = pick.main(argv)
    return rc, json.loads(buf.getvalue())


# ------------------------------------------------------------------ tests

def test_clean_challenge_not_flagged_and_kept(tmp_path):
    root = tmp_path / "tree"
    build_tree(root)
    rc, data = run_pick(root)
    assert rc == 0

    cands = {c["name"]: c for c in data["candidates"]}
    assert set(cands) == {"clean_local"}
    assert cands["clean_local"]["needs_remote"] is False
    assert cands["clean_local"]["remote_reason"] is None
    assert data["stats"]["candidates_final"] == 1


def test_redacted_flag_excluded_by_default(tmp_path):
    root = tmp_path / "tree"
    build_tree(root)
    _, data = run_pick(root)

    assert data["stats"]["needs_remote_filtered_out"] == 2
    reasons = data["stats"]["needs_remote_by_reason"]
    assert sum(1 for k in reasons if k.startswith("redacted_flag_in_source")) == 1
    assert reasons.get("connection_info_null(web)") == 1
    names = [c["name"] for c in data["candidates"]]
    assert "fair_gambling" not in names
    assert "shop_null_conn" not in names


def test_include_remote_keeps_flagged_candidates(tmp_path):
    root = tmp_path / "tree"
    build_tree(root)
    rc, data = run_pick(root, "--include-remote")

    assert rc == 0
    by_name = {c["name"]: c for c in data["candidates"]}
    assert set(by_name) == {"clean_local", "fair_gambling", "shop_null_conn"}
    assert by_name["fair_gambling"]["needs_remote"] is True
    assert by_name["fair_gambling"]["remote_reason"].startswith(
        "redacted_flag_in_source")
    assert by_name["shop_null_conn"]["needs_remote"] is True
    assert by_name["shop_null_conn"]["remote_reason"] == "connection_info_null(web)"
    assert data["stats"]["needs_remote_filtered_out"] == 0


def test_detect_needs_remote_statement_hint(tmp_path):
    chal = tmp_path / "lonely"
    chal.mkdir()
    hit, reason = pick.detect_needs_remote(
        chal, {"description": "Connect to 10.0.0.1:1337 and pwn it."}, "")
    assert hit is True
    assert reason.startswith("remote_hint_in_statement")

    miss, _ = pick.detect_needs_remote(chal, {"description": "Unzip me."}, "")
    assert miss is False


# ---------------------------------------------- zip-attachment flag scanning

import zipfile  # noqa: E402  (kept here so existing tests stay untouched)


def _make_zip(path: Path, entries: dict[str, str]) -> None:
    """Deflated on purpose: real CTF zips are compressed, and compression
    keeps the literal REDACTED token out of the raw file bytes so only the
    zip-entry scan can see it."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, text in entries.items():
            zf.writestr(name, text)


def build_zip_tree(root: Path) -> None:
    meta = {"category": "pwn"}
    for name, desc, zentries in [
        ("zip_redacted", "Pwn the stack.",
         {"src/flag.txt": "brunner{REDACTED}\n",
          "src/main.c": "int main(void){return 0;}\n"}),
        ("zip_clean", "Overflow the buffer.",
         {"src/flag.txt": "brunner{fake_local_placeholder_flag}\n",
          "README": "Build with make.\n"}),
        ("zip_corrupt", "Pwn the service.", {}),
    ]:
        chal = root / "EventZ" / "Pwn" / name
        chal.mkdir(parents=True)
        (chal / "README.md").write_text(desc + "\n", encoding="utf-8")
        (chal / "metadata.json").write_text(
            json.dumps({"name": name, "description": desc, **meta}),
            encoding="utf-8")
        if zentries:
            _make_zip(chal / "chall.zip", zentries)
        else:
            (chal / "chall.zip").write_bytes(b"PK\x03\x04not-a-real-zip")


def test_zip_with_redacted_flag_excluded(tmp_path):
    root = tmp_path / "tree"
    build_zip_tree(root)
    _, data = run_pick(root)

    names = [c["name"] for c in data["candidates"]]
    assert "zip_redacted" not in names
    reasons = data["stats"]["needs_remote_by_reason"]
    hits = {k: v for k, v in reasons.items()
            if k.startswith("redacted_flag_in_archive")}
    assert sum(hits.values()) == 1


def test_zip_redacted_reason_string(tmp_path):
    root = tmp_path / "tree"
    build_zip_tree(root)
    rc, data = run_pick(root, "--include-remote")

    assert rc == 0
    by_name = {c["name"]: c for c in data["candidates"]}
    assert by_name["zip_redacted"]["needs_remote"] is True
    reason = by_name["zip_redacted"]["remote_reason"]
    assert reason.startswith("redacted_flag_in_archive(")
    assert "chall.zip" in reason and "flag.txt" in reason


def test_clean_zip_not_flagged(tmp_path):
    root = tmp_path / "tree"
    build_zip_tree(root)
    _, data = run_pick(root, "--include-remote")

    by_name = {c["name"]: c for c in data["candidates"]}
    assert by_name["zip_clean"]["needs_remote"] is False
    assert by_name["zip_clean"]["remote_reason"] is None


def test_corrupt_zip_skipped_without_crash(tmp_path):
    root = tmp_path / "tree"
    build_zip_tree(root)
    rc, data = run_pick(root)  # must not raise

    by_name = {c["name"]: c for c in data["candidates"]}
    assert rc == 0
    assert by_name["zip_corrupt"]["needs_remote"] is False
