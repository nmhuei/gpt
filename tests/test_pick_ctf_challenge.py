"""Tests for scripts/pick_ctf_challenge.py.

Runs selection logic against a synthetic tmp tree only -- never scans
~/Workspace/CTF and never touches the network (probe opener is faked).
"""

import importlib.util
import io
import json
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Literal

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location(
    "pick_ctf_challenge", SCRIPTS / "pick_ctf_challenge.py")
assert spec is not None and spec.loader is not None
pick = importlib.util.module_from_spec(spec)
sys.modules.setdefault("pick_ctf_challenge", pick)
spec.loader.exec_module(pick)


# --------------------------------------------------------------- fixtures

def make_readme(d: Path, text: str = "Decode the string and get the flag.\n"):
    (d / "README.md").write_text(f"# chall\n\n{text}", encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "CTF"
    # 1. solved: has flag.txt -> rejected
    solved = root / "EventA" / "Pwn" / "done_one"
    solved.mkdir(parents=True)
    make_readme(solved, "pwn me")
    (solved / "chall").write_bytes(b"\x7fELF binary")
    (solved / "flag.txt").write_text("flag{solved_already}\n")

    # 2. needs human review -> rejected
    human = root / "EventA" / "Misc" / "broken_one"
    human.mkdir(parents=True)
    make_readme(human)
    (human / "attachment.zip").write_bytes(b"PK")
    (human / "NEEDS_HUMAN_REVIEW.md").write_text("needs help")

    # 3. local-only candidate: README + attachment, category from parent "Web"
    local = root / "EventA" / "Web" / "local_web"
    local.mkdir(parents=True)
    make_readme(local, "Find the XSS.")
    (local / "site_backup.zip").write_bytes(b"PK")

    # 4. remote-only candidate via metadata connection_info (dead URL)
    remote = root / "EventA" / "Crypto" / "remote_crypto"
    remote.mkdir(parents=True)
    (remote / "metadata.json").write_text(json.dumps({
        "name": "rsa_easy", "category": "Crypto", "points": 100,
        "connection_info": "http://127.0.0.1:1/ nope",
        "raw": {"title": "rsa_easy", "description": "Decrypt this RSA.",
                "difficulty": "easy", "points": 100, "solves": 42},
    }))

    # 5. unclear statement: attachment exists but description/README empty
    vague = root / "EventB" / "Rev" / "no_desc"
    vague.mkdir(parents=True)
    (vague / "blob.bin").write_bytes(b"\x00\x01")

    # 6. container dir holding two challenges must not itself be a challenge;
    #    container has its own README so it *would* qualify without leaf rule
    cont = root / "EventB"
    make_readme(cont, "event landing page")
    for name in ("sub_a", "sub_b"):
        sub = cont / name
        sub.mkdir()
        make_readme(sub)
        (sub / "data.tar.gz").write_bytes(b"\x1f\x8b")

    # 7. already used (recorded in sidecar state)
    used = root / "EventB" / "Misc" / "used_before"
    used.mkdir(parents=True)
    make_readme(used)
    (used / "hint.png").write_bytes(b"\x89PNG")
    return root


def run_picker(tree: Path, extra=None):
    out = tree.parent / "out.json"
    rc = pick.main([
        "--root", str(tree), "--output", str(out), "--min-count", "0",
        *(extra or []),
    ])
    data = json.loads(out.read_text())
    return rc, data


def by_name(data, name):
    return next(c for c in data["candidates"] if c["name"] == name)


# ------------------------------------------------------------------ tests

def test_solved_challenge_rejected(tree):
    _rc, data = run_picker(tree)
    paths = [r["path"] for r in data["rejected"]]
    assert any(p.endswith("done_one") for p in paths)
    reason = next(r["reason"] for r in data["rejected"]
                  if r["path"].endswith("done_one"))
    assert reason == "already_solved(flag.txt)"
    assert not any(c["name"] == "done_one" for c in data["candidates"])


def test_needs_human_review_rejected(tree):
    _, data = run_picker(tree)
    reason = next(r["reason"] for r in data["rejected"]
                  if r["path"].endswith("broken_one"))
    assert reason == "needs_human_review"


def test_local_candidate_accepted_with_category(tree):
    _, data = run_picker(tree)
    cand = by_name(data, "local_web")
    assert cand["has_local_files"] is True
    assert cand["remote_url"] is None
    assert cand["remote_alive"] is None
    assert cand["category"] == "web"


def test_remote_only_candidate_accepted_even_if_dead(tree, monkeypatch):
    # fake network: every probe fails
    monkeypatch.setattr(pick, "probe_url",
                        lambda url, timeout=3, _opener=None: False)
    _rc, data = run_picker(tree, ["--probe-remote", "5"])
    cand = by_name(data, "rsa_easy")
    assert cand["has_local_files"] is False
    assert cand["remote_url"] == "http://127.0.0.1:1/"
    assert cand["remote_alive"] is False          # dead but still a candidate
    assert cand["est_difficulty_guess"] == "easy"


def test_probe_budget_respected_and_alive_detected(tree, monkeypatch):
    calls = []

    def fake_probe(url, timeout=3, _opener=None):
        calls.append(url)
        return "live" in url

    monkeypatch.setattr(pick, "probe_url", fake_probe)

    live = tree / "EventC" / "Osint" / "live_osint"
    live.mkdir(parents=True)
    make_readme(live)
    (live / "metadata.json").write_text(json.dumps(
        {"connection_info": "http://live.example.com"}))
    # two more remote candidates so budget binds
    for i in range(2):
        d = tree / "EventC" / "Osint" / f"more_{i}"
        d.mkdir(parents=True)
        make_readme(d)
        (d / "metadata.json").write_text(json.dumps(
            {"connection_info": f"http://dead-{i}.example.com"}))

    _, data = run_picker(tree, ["--probe-remote", "2"])
    assert len(calls) <= 2
    assert by_name(data, "live_osint")["remote_alive"] is True
    assert any(c["remote_alive"] is False for c in data["candidates"])


def test_unclear_statement_rejected(tree):
    _, data = run_picker(tree)
    reason = next(r["reason"] for r in data["rejected"]
                  if r["path"].endswith("no_desc"))
    assert reason.startswith("unclear_statement")


def test_container_dir_not_a_challenge_but_children_are(tree):
    _, data = run_picker(tree)
    names = {c["name"] for c in data["candidates"]}
    assert {"sub_a", "sub_b"} <= names
    assert not any(c["path"].endswith("/EventB") for c in data["candidates"])
    # EventB itself was rejected as a marker dir with nested markers
    assert not any(r["path"].endswith("CTF") for r in data["rejected"])


def test_used_state_filters_candidates(tree, tmp_path):
    used_file = tmp_path / "used.json"
    used_path = str((tree / "EventB" / "Misc" / "used_before").resolve())
    used_file.write_text(json.dumps({used_path: "2026-08-01T00:00:00"}))

    _, data = run_picker(tree, ["--used-file", str(used_file)])
    assert not any(c["name"] == "used_before" for c in data["candidates"])
    assert data["stats"]["used_filtered_out"] == 1

    _, data2 = run_picker(tree, ["--used-file", str(used_file),
                                 "--include-used"])
    cand = by_name(data2, "used_before")
    assert cand["used_at"] == "2026-08-01T00:00:00"


def test_mark_used_writes_sidecar(tree, tmp_path):
    used_file = tmp_path / "used.json"
    run_picker(tree, ["--used-file", str(used_file),
                      "--mark-used", "ALL"])
    state = json.loads(used_file.read_text())
    marked = [k for k, v in state.items() if v]
    assert len(marked) >= 3
    # after marking everything, default run finds nothing
    _rc, data = run_picker(tree, ["--used-file", str(used_file)])
    assert data["candidates"] == []


def test_min_count_gate_fails(tmp_path):
    empty = tmp_path / "empty_root"
    (empty / "EventX" / "Web" / "only_one").mkdir(parents=True)
    make_readme(empty / "EventX" / "Web" / "only_one")
    (empty / "EventX" / "Web" / "only_one" / "a.zip").write_bytes(b"PK")
    rc = pick.main(["--root", str(empty), "--output",
                    str(tmp_path / "o.json"), "--min-count", "5"])
    assert rc == 1


def test_excerpt_length_capped(tree):
    big = tree / "EventD" / "Misc" / "wordy"
    big.mkdir(parents=True)
    make_readme(big, "x" * 500)
    (big / "file.bin").write_bytes(b"z")
    _, data = run_picker(tree)
    cand = by_name(data, "wordy")
    assert len(cand["description_excerpt"]) <= 200


# ------------------------------------------------- v3: flag-in-docker

def _make_docker_chal(tree, name, files):
    d = tree / "EventE" / "Pwn" / name
    d.mkdir(parents=True)
    make_readme(d, "You have everything but not the flag.")
    (d / "chall").write_bytes(b"\x7fELF binary")
    for fname, content in files.items():
        (d / fname).write_text(content, encoding="utf-8")
    return d


def test_flag_in_dockerfile_classified_needs_remote(tree):
    _make_docker_chal(tree, "ptit_dynamic", {
        "Dockerfile": 'FROM ubuntu:22.04\nWORKDIR /srv\n'
                      'COPY flag.txt /srv/app/flag.txt\n'
                      'CMD ["./run"]\n',
    })
    _, data = run_picker(tree, ["--include-remote"])
    cand = by_name(data, "ptit_dynamic")
    assert cand["needs_remote"] is True
    assert cand["remote_reason"].startswith("flag_in_docker(Dockerfile:")
    assert "COPY flag.txt" in cand["remote_reason"]
    # default run must exclude it from the fresh list
    _, data2 = run_picker(tree)
    assert not any(c["name"] == "ptit_dynamic" for c in data2["candidates"])


def test_flag_in_compose_env_and_dotenv_classified_needs_remote(tree):
    _make_docker_chal(tree, "compose_flag_env", {
        "docker-compose.yml": "services:\n  app:\n    build: .\n"
                              "    environment:\n"
                              "      - FLAG=L3AK{FAKE_FLAG_FOR_TESTING}\n"
                              "    ports:\n      - \"13337:3000\"\n",
    })
    _make_docker_chal(tree, "dotenv_flag", {".env": "FLAG=secret{here}\n"})
    _, data = run_picker(tree, ["--include-remote"])
    assert by_name(data, "compose_flag_env")["needs_remote"] is True
    r1 = by_name(data, "compose_flag_env")["remote_reason"]
    assert "flag_in_docker(docker-compose.yml" in r1
    r2 = by_name(data, "dotenv_flag")["remote_reason"]
    assert "flag_in_docker(.env" in r2


def test_benign_dockerfile_not_flagged_remote(tree):
    _make_docker_chal(tree, "benign_local_web", {
        "Dockerfile": "FROM python:3.12-slim\nCOPY app.py /app/\n"
                      "RUN pip install flask\nEXPOSE 5000\n",
    })
    _, data = run_picker(tree, ["--include-remote"])
    cand = by_name(data, "benign_local_web")
    assert cand["needs_remote"] is False
    assert cand["remote_reason"] is None


def test_dynamic_container_metadata_tagged_remote(tree):
    d = tree / "EventE" / "Web" / "gzctf_container"
    d.mkdir(parents=True)
    make_readme(d, "Open the admin panel.")
    (d / "site_backup.zip").write_bytes(b"PK\x03\x04")
    (d / "metadata.json").write_text(json.dumps({
        "name": "gzctf_container", "category": "Web", "points": 100,
        "tags": ["DynamicContainer"],
        "instance_info": {"is_container": True, "type": "gzctf"},
    }))
    _, data = run_picker(tree, ["--include-remote"])
    cand = by_name(data, "gzctf_container")
    assert cand["needs_remote"] is True
    assert cand["remote_reason"].startswith("dynamic_container_metadata")


# ------------------------------------- v3: REDACTED inside nested archives

def _tar_bytes(members: dict[str, bytes], mode: Literal["w", "w:gz", "w:bz2", "w:xz"] = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, payload in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(payload)
            tf.addfile(ti, io.BytesIO(payload))
    return buf.getvalue()


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def test_redacted_in_tar_gz_detected(tree):
    d = tree / "EventF" / "Pwn" / "tar_redacted"
    d.mkdir(parents=True)
    make_readme(d)
    blob = _tar_bytes({"dist/flag.txt": b"brunner{REDACTED}\n",
                       "run.sh": b"./run\n"})
    (d / "pack.tar.gz").write_bytes(blob)
    _, data = run_picker(tree, ["--include-remote"])
    cand = by_name(data, "tar_redacted")
    assert cand["needs_remote"] is True
    assert cand["remote_reason"] == \
        f"redacted_flag_in_archive(pack.tar.gz:{'dist/flag.txt'})"


def test_redacted_in_nested_zip_detected(tree):
    d = tree / "EventF" / "Misc" / "nested_zip_redacted"
    d.mkdir(parents=True)
    make_readme(d)
    inner = _zip_bytes({"README.md": b"flag is brunner{REDACTED}\n"})
    outer = _zip_bytes({"nested/inner.zip": inner,
                        "note.txt": b"nothing here\n"})
    (d / "outer.zip").write_bytes(outer)
    _, data = run_picker(tree, ["--include-remote"])
    cand = by_name(data, "nested_zip_redacted")
    assert cand["needs_remote"] is True
    assert cand["remote_reason"] == ("redacted_flag_in_archive("
                                     "outer.zip/nested/inner.zip:README.md)")


def test_docker_compose_inside_zip_classified_needs_remote(tree):
    # PTIT DynamicContainer pattern: compose with runtime FLAG lives inside
    # the shipped attachment zip, metadata.json sits in the parent dir.
    d = tree / "EventG" / "Pwn" / "ptit_zip_dynamic"
    d.mkdir(parents=True)
    make_readme(d, "You have everything but not the flag.")
    blob = _zip_bytes({
        "public/docker-compose.yml":
            'services:\n  heap:\n    build: .\n'
            '    environment:\n      FLAG: "PTITCTF{local_test_flag}"\n',
        "public/Dockerfile": b"FROM ubuntu:22.04\nCOPY chall /home/ctf/chall\n",
        "public/chall": b"\x7fELF binary",
    })
    (d / "heap_basic_V2.zip").write_bytes(blob)
    _, data = run_picker(tree, ["--include-remote"])
    cand = by_name(data, "ptit_zip_dynamic")
    assert cand["needs_remote"] is True
    assert cand["remote_reason"].startswith(
        "flag_in_docker(heap_basic_V2.zip:public/docker-compose.yml:")
    assert "PTITCTF" in cand["remote_reason"]


def test_zip_bomb_budget_stops_scan_without_explosion(tree):
    d = tree / "EventF" / "Pwn" / "bomb_guard"
    d.mkdir(parents=True)
    make_readme(d)
    # 48 MiB of NULs compresses to ~50 KiB; budget (16 MiB) must stop the
    # read long before the whole member is consumed.
    blob = _tar_bytes({"flag.txt": b"\x00" * (48 << 20)})
    (d / "bomb.tar.gz").write_bytes(blob)
    t0 = time.monotonic()
    res = pick._archive_redacted_entry(d)
    elapsed = time.monotonic() - t0
    assert res is None                       # nothing readable got flagged
    assert elapsed < 30                      # bounded work, no runaway


def test_env_var_with_flag_prefix_but_url_value_not_flagged(tree):
    # FLAG_SERVICE_DEV_URL=http://... is a URL, not flag material
    _make_docker_chal(tree, "flag_service_fp", {
        ".env.example": "FLAG_SERVICE_DEV_URL=http://localhost:3001\n"
                        "PORT=3000\n",
    })
    _, data = run_picker(tree, ["--include-remote"])
    cand = by_name(data, "flag_service_fp")
    assert cand["needs_remote"] is False


def test_plain_zip_redacted_still_detected_regression(tree):
    # old zip-only behavior must survive the v3 rewrite
    d = tree / "EventF" / "Pwn" / "zip_redacted_classic"
    d.mkdir(parents=True)
    make_readme(d)
    blob = _zip_bytes({"flag.txt": b"brunner{REDACTED}\n"})
    (d / "classic.zip").write_bytes(blob)
    _, data = run_picker(tree, ["--include-remote"])
    cand = by_name(data, "zip_redacted_classic")
    assert cand["needs_remote"] is True
    assert "redacted_flag_in_archive(classic.zip:" in cand["remote_reason"]


# ------------------------------------------------------------ v3: --unmark

def test_unmark_removes_entry_logs_reason_and_restores_candidate(
        tree, tmp_path, capsys):
    used_file = tmp_path / "used.json"
    target = str((tree / "EventB" / "Misc" / "used_before").resolve())
    used_file.write_text(json.dumps({target: "2026-08-01T00:00:00"}))

    rc = pick.main(["--unmark", target, "--used-file", str(used_file),
                    "--unmark-reason",
                    "wrongly marked needs_remote; instance now available"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[+] unmarked" in out
    assert "wrongly marked needs_remote; instance now available" in out
    assert json.loads(used_file.read_text()) == {}

    # candidate is available again without --include-used
    _, data = run_picker(tree, ["--used-file", str(used_file)])
    assert by_name(data, "used_before")["used_at"] is None


def test_unmark_missing_entry_returns_nonzero(tmp_path, capsys):
    used_file = tmp_path / "used.json"
    used_file.write_text(json.dumps({}))
    rc = pick.main(["--unmark", "/nonexistent/challenge",
                    "--used-file", str(used_file),
                    "--unmark-reason", "cleanup attempt"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "[!]" in out and "no used_at entry" in out


def test_classifier_risk_scores_binary_rev_high_and_crypto_low(tree):
    rev = tree / "EventRisk" / "Rev" / "android_rev"
    rev.mkdir(parents=True)
    make_readme(rev, "Analyze this Android application locally.")
    (rev / "challenge.apk").write_bytes(b"PK")
    crypto = tree / "EventRisk" / "Crypto" / "math_crypto"
    crypto.mkdir(parents=True)
    make_readme(crypto, "Recover the plaintext from these equations.")
    (crypto / "cipher.txt").write_text("12345", encoding="utf-8")

    _, data = run_picker(tree)
    assert by_name(data, "android_rev")["classifier_risk"] == "high"
    assert by_name(data, "math_crypto")["classifier_risk"] == "low"


def test_max_risk_filters_and_risk_sorting(tree):
    rev = tree / "EventRisk2" / "Rev" / "native_rev"
    rev.mkdir(parents=True)
    make_readme(rev, "Reverse this executable.")
    (rev / "challenge.exe").write_bytes(b"MZ")
    crypto = tree / "EventRisk2" / "Crypto" / "safe_math"
    crypto.mkdir(parents=True)
    make_readme(crypto, "Solve the modular arithmetic.")
    (crypto / "values.txt").write_text("1 2 3", encoding="utf-8")

    _, all_data = run_picker(tree)
    risks = [pick.RISK_RANK[c["classifier_risk"]] for c in all_data["candidates"]]
    assert risks == sorted(risks)

    _, low_data = run_picker(tree, ["--max-risk", "low"])
    assert by_name(low_data, "safe_math")["classifier_risk"] == "low"
    assert not any(c["name"] == "native_rev" for c in low_data["candidates"] )
    assert low_data["stats"]["risk_filtered_out"] >= 1
