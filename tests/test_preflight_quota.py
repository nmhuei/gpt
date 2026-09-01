"""QUOTA-PREFLIGHT CLI tests (ROADMAP row M).

Covers the coordinator-facing exit-code contract of
``scripts/preflight_quota.py``:

  0 = quota OK (primary <70% AND secondary <50%),
  2 = defer batch (primary >=70% OR secondary >=50%),
  3 = unknown (no bearer, transport error, 401/403, other non-200,
      unparseable payload, nothing measurable).

Every HTTP call is a fake; the token-cache cases use tmp_path files.  Nothing
here ever touches the network or the real browser profile.  A single
subprocess test proves ``--help`` exits cleanly with zero side effects (the
FAILURES lesson); everything else stays in-process.
"""

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "preflight_quota.py"
_spec = importlib.util.spec_from_file_location("preflight_quota", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
pf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pf)

URL = "https://chatgpt.com/backend-api/wham/usage"


class FakeHttp:
    """Injectable blocking GET returning one canned response."""

    def __init__(
        self,
        status: int = 200,
        payload: Any = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, Any]:
        self.calls.append((url, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.status, self.payload


def usage_payload(
    primary: tuple[float, int, int] | None = (10, 18000, 0),
    secondary: tuple[float, int, int] | None = (5, 604800, 0),
) -> dict[str, Any]:
    """Canonical RateLimitStatusDetails shape (research §A1)."""

    def window(spec: tuple[float, int, int] | None) -> Any:
        if spec is None:
            return None
        percent, seconds, reset_at = spec
        return {
            "used_percent": percent,
            "limit_window_seconds": seconds,
            "reset_at": reset_at,
        }

    return {
        "rate_limit": {
            "primary_window": window(primary),
            "secondary_window": window(secondary),
        }
    }


def run(http: FakeHttp, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    kwargs.setdefault("url", URL)
    kwargs.setdefault("http_get", http)
    return pf.run_preflight(**kwargs)


# ---------------------------------------------------------------------------
# Exit-code contract
# ---------------------------------------------------------------------------


def test_exit_code_constants_match_contract():
    assert pf.EXIT_OK == 0
    assert pf.EXIT_DEFER == 2
    assert pf.EXIT_UNKNOWN == 3


@pytest.mark.parametrize(
    ("primary", "secondary", "expected"),
    [
        ((69.9, 18000, 0), (49.9, 604800, 0), pf.EXIT_OK),
        ((0, 18000, 0), (0, 604800, 0), pf.EXIT_OK),
        ((70, 18000, 0), (0, 604800, 0), pf.EXIT_DEFER),  # exact >=70 blocks
        ((100, 18000, 0), (0, 604800, 0), pf.EXIT_DEFER),
        ((10, 18000, 0), (50, 604800, 0), pf.EXIT_DEFER),  # exact >=50 blocks
        ((69.9, 18000, 0), (50.1, 604800, 0), pf.EXIT_DEFER),
    ],
)
def test_threshold_decisions(primary, secondary, expected):
    http = FakeHttp(status=200, payload=usage_payload(primary, secondary))

    code, summary = run(http, token="tok")

    assert code == expected
    assert summary["blocked"] is (expected == pf.EXIT_DEFER)
    assert len(http.calls) == 1


def test_ok_summary_carries_windows_and_thresholds():
    http = FakeHttp(status=200, payload=usage_payload())

    code, summary = run(http, token="tok")

    assert code == pf.EXIT_OK
    assert summary["primary"]["used_percent"] == 10
    assert summary["secondary"]["limit_window_seconds"] == 604800
    assert summary["thresholds"] == {
        "primary_block_percent": 70.0,
        "secondary_block_percent": 50.0,
    }


@pytest.mark.parametrize("percent", [250, -5])
def test_garbled_percent_is_clamped_before_deciding(percent):
    # 250 clamps to 100 => defer; -5 clamps to 0 => ok.
    expected = pf.EXIT_DEFER if percent > 0 else pf.EXIT_OK
    http = FakeHttp(
        status=200,
        payload=usage_payload((percent, 18000, 0), (0, 604800, 0)),
    )

    code, summary = run(http, token="tok")

    assert code == expected
    assert summary["primary"]["used_percent"] == (100.0 if percent > 0 else 0.0)


# ---------------------------------------------------------------------------
# Unknown paths (exit 3) — coordinator decides, script never guesses
# ---------------------------------------------------------------------------


def test_transport_error_is_unknown():
    http = FakeHttp(error=RuntimeError("conn reset"))

    code, summary = run(http, token="tok")

    assert code == pf.EXIT_UNKNOWN
    assert summary["error"] == "transport"
    assert len(http.calls) == 1


@pytest.mark.parametrize(("status", "reason"), [(401, "auth_rejected"), (403, "auth_rejected"), (404, "http_status"), (500, "http_status")])
def test_non_200_is_unknown(status, reason):
    http = FakeHttp(status=status, payload={"error": "nope"})

    code, summary = run(http, token="tok")

    assert code == pf.EXIT_UNKNOWN
    assert summary["error"] == reason
    assert summary["http_status"] == status


@pytest.mark.parametrize(
    "payload",
    [None, {}, [1, 2], "nope", {"rate_limit": "x"}, {"rate_limit": {}}],
)
def test_unparseable_or_unmeasurable_payload_is_unknown(payload):
    http = FakeHttp(status=200, payload=payload)

    code, summary = run(http, token="tok")

    assert code == pf.EXIT_UNKNOWN
    assert summary["blocked"] is None
    assert summary["error"] in ("unparseable_payload", "no_measurable_window")


def test_null_windows_are_representable_but_unknown():
    # Some accounts lack a tier entirely (research §B8): parseable shape,
    # but with no measurable percent the gate cannot vouch for quota.
    http = FakeHttp(status=200, payload=usage_payload(None, None))

    code, summary = run(http, token="tok")

    assert code == pf.EXIT_UNKNOWN
    assert summary["error"] == "no_measurable_window"
    assert summary["primary"] is None and summary["secondary"] is None


def test_no_bearer_never_touches_the_network(tmp_path):
    http = FakeHttp(status=200, payload=usage_payload())

    code, summary = run(http, token=None, profile_dir=tmp_path / "absent")

    assert code == pf.EXIT_UNKNOWN
    assert summary["error"] == "no_bearer"
    assert summary["bearer_source"] is None
    assert http.calls == []  # exit 3 costs zero requests


# ---------------------------------------------------------------------------
# TokenBundle disk-cache bearer source (read-only replication)
# ---------------------------------------------------------------------------


def write_cache(path: Path, *, stored_at: float, version: int = 1, raw: str | None = None) -> Path:
    if raw is None:
        raw = json.dumps(
            {
                "version": version,
                "stored_at": stored_at,
                "access_token": "cached-bearer",
                "cookies": {"cf_clearance": "c", "oai-device-id": "d"},
                "cf_clearance": "c",
                "oai_device_id": "d",
            }
        )
    path.write_text(raw, encoding="utf-8")
    return path


def test_fresh_disk_cache_bearer_is_used(tmp_path):
    write_cache(tmp_path / pf.TOKEN_CACHE_FILENAME, stored_at=time.time())
    http = FakeHttp(status=200, payload=usage_payload())

    code, summary = run(http, profile_dir=tmp_path)

    assert code == pf.EXIT_OK
    assert summary["bearer_source"] == "token-cache"
    url, headers = http.calls[0]
    assert url == URL
    assert headers["authorization"] == "Bearer cached-bearer"


@pytest.mark.parametrize(
    "cache_kwargs",
    [
        {"stored_at": time.time() - 10_000},  # older than max-token-age
        {"stored_at": time.time(), "version": 99},
        {"stored_at": time.time(), "raw": "{not json"},
        {"stored_at": time.time() + 9999},  # future stamp = clock skew
    ],
)
def test_stale_or_corrupt_cache_falls_back_to_unknown(tmp_path, cache_kwargs):
    write_cache(tmp_path / pf.TOKEN_CACHE_FILENAME, **cache_kwargs)
    http = FakeHttp(status=200, payload=usage_payload())

    code, summary = run(
        http,
        profile_dir=tmp_path,
        max_token_age=1800.0,
    )

    assert code == pf.EXIT_UNKNOWN
    assert summary["error"] == "no_bearer"
    assert http.calls == []


def test_explicit_token_wins_over_disk_cache(tmp_path):
    write_cache(tmp_path / pf.TOKEN_CACHE_FILENAME, stored_at=time.time())
    http = FakeHttp(status=200, payload=usage_payload())

    code, summary = run(http, token="explicit-bearer", profile_dir=tmp_path)

    assert code == pf.EXIT_OK
    assert summary["bearer_source"] == "cli"
    assert http.calls[0][1]["authorization"] == "Bearer explicit-bearer"


# ---------------------------------------------------------------------------
# Header construction
# ---------------------------------------------------------------------------


def test_headers_minimal_by_default():
    http = FakeHttp(status=200, payload=usage_payload())

    run(http, token="tok")

    headers = http.calls[0][1]
    assert set(headers) == {"authorization", "accept"}
    assert headers["accept"] == "application/json"


def test_optional_account_and_user_agent_headers_added():
    http = FakeHttp(status=200, payload=usage_payload())

    run(http, token="tok", account_id="acct-1", user_agent="codex_cli_rs")

    headers = http.calls[0][1]
    assert headers["chatgpt-account-id"] == "acct-1"
    assert headers["user-agent"] == "codex_cli_rs"


# ---------------------------------------------------------------------------
# CLI surface: argparse + main-guard (FAILURES lesson: --help side-effect free)
# ---------------------------------------------------------------------------


def test_main_prints_json_and_returns_code(monkeypatch, capsys):
    http = FakeHttp(status=200, payload=usage_payload())
    monkeypatch.setattr(pf, "default_http_get", http)

    code = pf.main(["--token", "cli-tok", "--url", URL])

    assert code == pf.EXIT_OK
    printed = json.loads(capsys.readouterr().out.strip())
    assert printed["blocked"] is False
    assert printed["bearer_source"] == "cli"
    assert len(http.calls) == 1


def test_parser_help_raises_systemexit_zero_without_http(monkeypatch, capsys):
    http = FakeHttp(status=200, payload=usage_payload())
    monkeypatch.setattr(pf, "default_http_get", http)

    with pytest.raises(SystemExit) as excinfo:
        pf.build_parser().parse_args(["--help"])

    assert excinfo.value.code == 0
    assert "usage:" in capsys.readouterr().out
    assert http.calls == []


def test_script_help_subprocess_is_side_effect_free():
    """End-to-end proof: running the file itself with --help exits 0."""
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout
