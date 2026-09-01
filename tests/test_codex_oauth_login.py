"""CODEX-OAUTH-LOGIN-HELPER tests (scripts/auth/codex_oauth_login.py).

Fully offline: the HTTP exchange is always injected — the real network path
(``default_exchange_post``) is never invoked, and no live PKCE flow runs.
Covers: S256 challenge correctness, authorize-URL shape, paste-back parsing,
monkeypatched exchange writing a schema-correct 0600 auth.json via
``save_auth_json``, clean errors (no raw tracebacks), ``--force`` guard,
``--help`` with zero side effects, and the new
``codex_auth.bundle_from_token_payload`` validator.
"""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import time
from pathlib import Path

import pytest

from gpt.transport.codex_auth import (
    DEFAULT_CLIENT_ID,
    CodexAuthInvalid,
    bundle_from_token_payload,
)
from scripts.auth.codex_oauth_login import (
    DEFAULT_REDIRECT_URI,
    DEFAULT_SCOPE,
    MintError,
    build_authorize_url,
    build_parser,
    extract_code_and_state,
    generate_pkce,
    generate_state,
    main,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_jwt(exp: float) -> str:
    """Minimal three-segment JWT carrying only an ``exp`` claim."""
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"exp": exp}).encode())
    return f"{header}.{payload}.{_b64url(b'signature')}"


def fake_token_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "access_token": make_jwt(time.time() + 3600),
        "refresh_token": "rt-initial-opaque",
        "id_token": make_jwt(time.time() + 3600),
        "expires_in": 3600,
        "account_id": "acc-123",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _scrub_codex_env(monkeypatch):
    """Isolate from shell-exported flags; every test opts in explicitly."""
    monkeypatch.delenv("WEBGPT_CODEX_AUTH_JSON", raising=False)
    monkeypatch.delenv("WEBGPT_CODEX_CLIENT_ID", raising=False)


@pytest.fixture()
def auth_path(tmp_path: Path) -> Path:
    return tmp_path / "codex-mint" / "auth.json"


class RecordingPost:
    def __init__(self) -> None:
        self.form: dict[str, str] = {}

    def __call__(self, url: str, form: dict[str, str]) -> tuple[int, object]:
        assert url == "https://auth.openai.com/oauth/token"
        self.form = form
        return 200, fake_token_payload()


@pytest.fixture()
def ok_post() -> RecordingPost:
    """Injectable success exchange capturing the form it was called with."""
    return RecordingPost()


def run_ok(auth_path: Path, post, extra: list[str] | None = None) -> int:
    # Pasted URL carries no state param (owner may paste just the code part);
    # state-mismatch enforcement has its own dedicated test.
    argv = [
        "--auth-json",
        str(auth_path),
        "--code",
        "http://localhost:1455/auth/callback?code=abc123",
    ]
    return main(argv + (extra or []), post_fn=post)


# ---------------------------------------------------------------------------
# PKCE + URL + parsing units
# ---------------------------------------------------------------------------


class TestPkce:
    def test_verifier_shape(self):
        verifier, _ = generate_pkce()
        assert 43 <= len(verifier) <= 128  # RFC 7636 bounds
        assert "=" not in verifier and "+" not in verifier and "/" not in verifier
        assert verifier.isalnum() or all(c.isalnum() or c in "-_" for c in verifier)

    def test_challenge_is_s256_of_verifier(self):
        verifier, challenge = generate_pkce()
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        assert challenge == expected  # independent recomputation of S256
        assert len(challenge) == 43  # sha256 digest → fixed 43-char b64url

    def test_randomness(self):
        assert generate_pkce()[0] != generate_pkce()[0]
        assert generate_state() != generate_state()


class TestAuthorizeUrl:
    def test_full_param_shape(self):
        url = build_authorize_url(
            issuer="https://auth.openai.com",
            client_id=DEFAULT_CLIENT_ID,
            redirect_uri=DEFAULT_REDIRECT_URI,
            scope=DEFAULT_SCOPE,
            code_challenge="CHALLENGE",
            state="STATE",
        )
        assert url.startswith("https://auth.openai.com/oauth/authorize?")
        query = dict(pair.split("=", 1) for pair in url.split("?", 1)[1].split("&"))
        assert query["response_type"] == "code"
        assert query["client_id"] == DEFAULT_CLIENT_ID
        assert query["redirect_uri"] == "http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback"
        assert "offline_access" in query["scope"]
        assert query["code_challenge"] == "CHALLENGE"
        assert query["code_challenge_method"] == "S256"
        assert query["state"] == "STATE"
        assert query["originator"] == "codex_cli_rs"
        assert query["codex_cli_simplified_flow"] == "true"
        assert query["id_token_add_organizations"] == "true"


class TestPasteBackParsing:
    def test_full_callback_url(self):
        code, state = extract_code_and_state(
            "http://localhost:1455/auth/callback?code=X1&state=S1"
        )
        assert code == "X1" and state == "S1"

    def test_bare_query_string(self):
        assert extract_code_and_state("code=B2&state=S2") == ("B2", "S2")

    def test_raw_code(self):
        assert extract_code_and_state("  RAWCODE ") == ("RAWCODE", None)

    def test_empty_raises_clean(self):
        with pytest.raises(MintError):
            extract_code_and_state("   ")

    def test_url_without_code_raises_clean(self):
        with pytest.raises(MintError):
            extract_code_and_state("http://localhost:1455/auth/callback?state=only")

    def test_whitespace_blob_raises_clean(self):
        with pytest.raises(MintError):
            extract_code_and_state("not a code")


# ---------------------------------------------------------------------------
# End-to-end through main() with monkeypatched HTTP
# ---------------------------------------------------------------------------


class TestMainMintSuccess:
    def test_writes_schema_correct_auth_json_0600(self, auth_path, ok_post):
        rc = run_ok(auth_path, ok_post)
        assert rc == 0
        assert auth_path.exists()
        mode = stat.S_IMODE(auth_path.stat().st_mode)
        assert mode == 0o600
        doc = json.loads(auth_path.read_text(encoding="utf-8"))
        assert doc["auth_mode"] == "chatgpt"
        tokens = doc["tokens"]
        assert tokens["access_token"].count(".") == 2  # header.payload.signature
        assert tokens["refresh_token"] == "rt-initial-opaque"
        assert tokens["account_id"] == "acc-123"
        assert doc["last_refresh"].endswith("Z")
        # codex-rs nullable companions preserved from the seed document.
        assert doc["OPENAI_API_KEY"] is None
        assert doc["agent_identity"] is None
        # No temp leftovers from the atomic write.
        assert list(auth_path.parent.glob("*.tmp")) == []

    def test_exchange_form_fields(self, auth_path, ok_post):
        assert run_ok(auth_path, ok_post) == 0
        form = ok_post.form
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == "abc123"
        assert form["redirect_uri"] == DEFAULT_REDIRECT_URI
        assert form["client_id"] == DEFAULT_CLIENT_ID
        verifier = form["code_verifier"]
        assert 43 <= len(verifier) <= 128  # RFC 7636 shape; S256 pairing proven above

    def test_printed_challenge_pairs_with_sent_verifier(
        self, auth_path, ok_post, capsys
    ):
        """End-to-end PKCE binding: challenge echoed to the owner must be the
        S256 of the verifier the injected exchange actually sends."""
        assert run_ok(auth_path, ok_post) == 0
        out = capsys.readouterr().out
        challenge_line = next(
            line for line in out.splitlines() if "/oauth/authorize?" in line
        )
        query = dict(
            pair.split("=", 1)
            for pair in challenge_line.split("?", 1)[1].split("&")
        )
        expected = _b64url(hashlib.sha256(ok_post.form["code_verifier"].encode()).digest())
        assert query["code_challenge"] == expected

    def test_prints_url_not_tokens(self, auth_path, ok_post, capsys):
        assert run_ok(auth_path, ok_post) == 0
        out = capsys.readouterr().out
        assert "/oauth/authorize?response_type=code" in out
        assert "rt-initial-opaque" not in out
        assert fake_token_payload()["access_token"] not in out

    def test_env_client_id_used(self, auth_path, ok_post, monkeypatch):
        monkeypatch.setenv("WEBGPT_CODEX_CLIENT_ID", "app_FROM_ENV")
        assert run_ok(auth_path, ok_post) == 0
        assert ok_post.form["client_id"] == "app_FROM_ENV"

    def test_state_mismatch_rejected(self, auth_path, ok_post):
        argv = [
            "--auth-json",
            str(auth_path),
            "--code",
            "http://localhost:1455/auth/callback?code=x&state=WRONG",
        ]
        rc = main(argv, post_fn=ok_post)
        assert rc == 1
        assert not auth_path.exists()


class TestMainFailurePaths:
    def test_existing_file_requires_force(self, auth_path, ok_post):
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text('{"keep": true}', encoding="utf-8")
        original = auth_path.read_text(encoding="utf-8")
        rc = run_ok(auth_path, ok_post)
        assert rc == 1
        assert auth_path.read_text(encoding="utf-8") == original  # untouched
        assert run_ok(auth_path, ok_post, extra=["--force"]) == 0

    def test_http_400_clean_error(self, auth_path, capsys):
        def bad_post(url, form):
            return 400, {"error": "invalid_grant"}

        rc = run_ok(auth_path, bad_post)
        captured = capsys.readouterr()
        assert rc == 1
        assert "invalid_grant" in captured.err
        assert "Traceback" not in captured.err
        assert not auth_path.exists()

    def test_transport_error_clean(self, auth_path, capsys):
        def dead_post(url, form):
            raise ConnectionError("dns failure")

        rc = run_ok(auth_path, dead_post)
        captured = capsys.readouterr()
        assert rc == 1
        assert "unreachable" in captured.err
        assert "Traceback" not in captured.err

    def test_missing_refresh_token_clean(self, auth_path, capsys):
        def incomplete_post(url, form):
            return 200, {"access_token": make_jwt(time.time() + 60)}

        rc = run_ok(auth_path, incomplete_post)
        captured = capsys.readouterr()
        assert rc == 1
        assert "refresh_token" in captured.err
        assert "Traceback" not in captured.err
        assert not auth_path.exists()

    def test_keyboard_interrupt_exit_130(self, auth_path, capsys):
        def interrupted_input(prompt):
            raise KeyboardInterrupt

        rc = main(["--auth-json", str(auth_path)], post_fn=ok_post_stub,
                  input_fn=interrupted_input)
        captured = capsys.readouterr()
        assert rc == 130
        assert "Traceback" not in captured.err

    def test_eof_prompt_clean_hint(self, auth_path, capsys):
        def eof_input(prompt):
            raise EOFError

        rc = main(["--auth-json", str(auth_path)], post_fn=ok_post_stub,
                  input_fn=eof_input)
        captured = capsys.readouterr()
        assert rc == 1
        assert "--code" in captured.err


def ok_post_stub(url: str, form: dict[str, str]) -> tuple[int, object]:  # pragma: no cover
    raise AssertionError("network hook must not fire before/without a code")


# ---------------------------------------------------------------------------
# --help safety (FAILURES 2026-08-25 lesson) + parser surface
# ---------------------------------------------------------------------------


class TestHelpSafety:
    def test_help_zero_side_effects(self, tmp_path, capsys):
        def poisoned_input(prompt):  # pragma: no cover - must never run
            raise AssertionError("no stdin on --help")

        target = tmp_path / "auth.json"
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"], post_fn=ok_post_stub, input_fn=poisoned_input)
        assert excinfo.value.code == 0
        assert "usage:" in capsys.readouterr().out
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_parser_defaults_documented(self):
        parser = build_parser()
        help_text = parser.format_help()
        for flag in ("--auth-json", "--client-id", "--issuer", "--redirect-uri",
                     "--scope", "--code", "--force"):
            assert flag in help_text
        defaults = {a.dest: a.default for a in parser._actions}
        assert defaults["issuer"] == "https://auth.openai.com"
        assert defaults["token_url"] == "https://auth.openai.com/oauth/token"
        assert defaults["force"] is False


# ---------------------------------------------------------------------------
# codex_auth.bundle_from_token_payload (the minimal module addition)
# ---------------------------------------------------------------------------


class TestBundleFromTokenPayload:
    def test_expires_in_wins_over_jwt_exp(self):
        now = 1_000_000.0
        jwt_exp = now + 9999  # deliberately different from expires_in
        bundle = bundle_from_token_payload(
            {
                "access_token": make_jwt(jwt_exp),
                "refresh_token": "rt",
                "expires_in": 600,
            },
            now=now,
        )
        assert bundle.expires_at == now + 600
        assert bundle.last_refresh_epoch == now

    def test_jwt_exp_fallback_when_no_expires_in(self):
        now = 2_000_000.0
        bundle = bundle_from_token_payload(
            {"access_token": make_jwt(now + 55), "refresh_token": "rt"}, now=now
        )
        assert bundle.expires_at == now + 55

    def test_account_id_alias_and_optionals(self):
        bundle = bundle_from_token_payload(
            {
                "access_token": make_jwt(5),
                "refresh_token": "rt",
                "chatgpt_account_id": "acc-alias",
                "id_token": "not-a-jwt-but-string",
            },
            now=0.0,
        )
        assert bundle.account_id == "acc-alias"
        assert bundle.id_token == "not-a-jwt-but-string"

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "string",
            [],
            {},
            {"refresh_token": "rt"},  # no access_token
            {"access_token": "at"},  # no refresh_token
            {"access_token": "", "refresh_token": "rt"},
        ],
    )
    def test_invalid_shapes_raise_codexauthinvalid(self, payload):
        with pytest.raises(CodexAuthInvalid):
            bundle_from_token_payload(payload, now=0.0)
