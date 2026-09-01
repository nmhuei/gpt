import argparse
import asyncio

import pytest

from gpt.debug import _configured_profile_dir, _login_credentials_from_args, cmd_login


@pytest.fixture(autouse=True)
def _isolate_ambient_env(monkeypatch):
    # Shell máy thật export credential; scrub để test hermetic.
    for name in (
        "CHATGPT_EMAIL", "CHATGPT_PASSWORD", "CHATGPT_TOTP_KEY",
        "CHATGPT_USERNAME", "CHATGPT_2FA", "CHATGPT_2FA_SECRET",
        "PROFILE_DIR",
        "CDP_PORT", "API_PORT", "BROWSER_HEADLESS",
        "DEFAULT_MODEL", "DEFAULT_EFFORT", "MAX_WORKERS",
    ):
        monkeypatch.delenv(name, raising=False)


def _args(**overrides):
    values = {
        "cred": None,
        "stdin": False,
        "username": None,
        "password": None,
        "two_factor": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_login_credentials_from_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CHATGPT_EMAIL=user@example.com\n"
        "CHATGPT_PASSWORD=secret_pass\n"
        "CHATGPT_TOTP_KEY=ABCDEF123456\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CHATGPT_USERNAME", raising=False)
    monkeypatch.delenv("CHATGPT_PASSWORD", raising=False)
    monkeypatch.delenv("CHATGPT_2FA_SECRET", raising=False)
    monkeypatch.delenv("CHATGPT_2FA", raising=False)

    creds = _login_credentials_from_args(_args())

    assert creds.username == "user@example.com"
    assert creds.password == "secret_pass"
    assert creds.totp_secret_or_code == "ABCDEF123456"


def test_login_credentials_cli_overrides_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "CHATGPT_EMAIL=dotenv@example.com\n"
        "CHATGPT_PASSWORD=dotenv_pass\n"
        "CHATGPT_TOTP_KEY=DOTENVTOTP\n"
    )
    monkeypatch.chdir(tmp_path)

    creds = _login_credentials_from_args(
        _args(
            username="cli@example.com",
            password="cli_pass",
            two_factor="123456",
        )
    )

    assert creds.username == "cli@example.com"
    assert creds.password == "cli_pass"
    assert creds.totp_secret_or_code == "123456"


def test_login_credentials_legacy_env_aliases(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHATGPT_USERNAME", "legacy@example.com")
    monkeypatch.setenv("CHATGPT_PASSWORD", "legacy_pass")
    monkeypatch.setenv("CHATGPT_2FA_SECRET", "LEGACYTOTP")

    creds = _login_credentials_from_args(_args())

    assert creds.username == "legacy@example.com"
    assert creds.password == "legacy_pass"
    assert creds.totp_secret_or_code == "LEGACYTOTP"


async def test_cmd_login_uses_dotenv_profile_when_cli_omits_profile(tmp_path, monkeypatch):
    profile = tmp_path / "configured-profile"
    (tmp_path / ".env").write_text(
        "CHATGPT_EMAIL=user@example.com\n"
        "CHATGPT_PASSWORD=secret_pass\n"
        "CHATGPT_TOTP_KEY=ABCDEF123456\n"
        f"PROFILE_DIR={profile}\n"
        "BROWSER_HEADLESS=true\n"
    )
    monkeypatch.chdir(tmp_path)

    captured = {}

    class FakeLoginManager:
        def __init__(self, *, profile_dir, headless, cdp_url):
            captured["profile_dir"] = profile_dir
            captured["headless"] = headless
            captured["cdp_url"] = cdp_url

        async def login(self, credentials, timeout_seconds):
            captured["username"] = credentials.username
            captured["timeout"] = timeout_seconds
            return True

    monkeypatch.setattr("gpt.auth.AutoLoginManager", FakeLoginManager)
    args = _args(
        profile_dir=None,
        headless=True,
        cdp_url=None,
        timeout=15,
    )

    await cmd_login(args)

    assert captured["profile_dir"] == profile
    assert captured["headless"] is True
    assert captured["timeout"] == 15


async def test_security_challenge_detection_is_read_only(tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from gpt.auth import AutoLoginManager

    manager = AutoLoginManager(profile_dir=tmp_path / "profile", headless=True)
    page = MagicMock()
    challenge_locator = MagicMock()
    challenge_locator.count = AsyncMock(return_value=1)
    page.locator = MagicMock(return_value=challenge_locator)
    page.title = AsyncMock(return_value="Just a moment...")
    page.mouse = MagicMock()

    assert await manager._has_security_challenge(page) is True
    assert not page.mouse.method_calls


def test_configured_profile_dir_comes_from_dotenv(tmp_path, monkeypatch):
    profile = tmp_path / "browser-profile"
    (tmp_path / ".env").write_text(f"PROFILE_DIR={profile}\n")
    monkeypatch.chdir(tmp_path)

    assert _configured_profile_dir() == str(profile)


async def test_cmd_login_enforces_outer_deadline(tmp_path, monkeypatch, capsys):
    """Login vượt tổng deadline → SystemExit(2) kèm thông báo rõ, không treo."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WEBGPT_LOGIN_DEADLINE_SECONDS", "0.2")

    class HangingLoginManager:
        def __init__(self, *, profile_dir, headless, cdp_url):
            pass

        async def login(self, credentials, timeout_seconds):
            await asyncio.sleep(30)  # mô phỏng hang: chỉ outer deadline cứu được

    monkeypatch.setattr("gpt.auth.AutoLoginManager", HangingLoginManager)
    args = _args(
        cred="user@example.com|pass|TOTP",
        profile_dir=str(tmp_path / "profile"),
        headless=True,
        cdp_url=None,
        timeout=15,
    )

    with pytest.raises(SystemExit) as excinfo:
        await cmd_login(args)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "deadline" in err.lower()
    assert "WEBGPT_LOGIN_DEADLINE_SECONDS" in err


async def test_autologin_browser_fallback_stops_on_new_page_failure(tmp_path, monkeypatch):
    """Fallback BrowserManager: new_page() nổ trước try/finally vẫn phải stop()."""
    import gpt.auth.authenticator as authenticator_module

    class ExplodingBrowserManager:
        launch_backend = "chromium-fallback"
        instances = []  # noqa: RUF012

        def __init__(self, **kwargs):
            self.stop_calls = 0
            type(self).instances.append(self)

        async def new_page(self):
            raise RuntimeError("launch exploded")

        async def stop(self):
            self.stop_calls += 1

    monkeypatch.setattr(authenticator_module, "CLOAK_AVAILABLE", False)
    monkeypatch.setattr(authenticator_module, "BrowserManager", ExplodingBrowserManager)

    manager = authenticator_module.AutoLoginManager(
        profile_dir=tmp_path / "profile", headless=True
    )
    creds = authenticator_module.LoginCredentials(username="u@example.com", password="pw")

    with pytest.raises(RuntimeError, match="launch exploded"):
        await manager.login(creds, timeout_seconds=5)

    [instance] = ExplodingBrowserManager.instances
    assert instance.stop_calls == 1


async def test_autologin_browser_fallback_stops_when_login_flow_raises(tmp_path, monkeypatch):
    """Fallback BrowserManager: exception giữa flow → stop() chạy trong finally."""
    import gpt.auth.authenticator as authenticator_module

    class FakePage:
        url = "https://chatgpt.com/"

        async def goto(self, *args, **kwargs):
            raise RuntimeError("goto blew up")

    class FakeBrowserManager:
        instances = []  # noqa: RUF012

        def __init__(self, **kwargs):
            self.stop_calls = 0
            self.page = FakePage()
            type(self).instances.append(self)

        async def new_page(self):
            return self.page

        async def stop(self):
            self.stop_calls += 1

    monkeypatch.setattr(authenticator_module, "CLOAK_AVAILABLE", False)
    monkeypatch.setattr(authenticator_module, "BrowserManager", FakeBrowserManager)

    manager = authenticator_module.AutoLoginManager(
        profile_dir=tmp_path / "profile", headless=True
    )
    creds = authenticator_module.LoginCredentials(username="u@example.com", password="pw")

    with pytest.raises(RuntimeError, match="goto blew up"):
        await manager.login(creds, timeout_seconds=5)

    [instance] = FakeBrowserManager.instances
    assert instance.stop_calls == 1


async def test_cmd_account_login_auto_uses_grace_deadline(tmp_path, monkeypatch, capsys):
    """account login --auto: outer deadline = --timeout + grace, như cmd_login.

    Slow-but-valid logins (email/password/MFA waits cộng dồn) không được cắt
    ở đúng --timeout nữa; chỉ tổng deadline (WEBGPT_LOGIN_DEADLINE_SECONDS)
    mới huỷ flow.
    """
    import gpt.debug as debug_module
    from gpt.debug import cmd_account_login

    monkeypatch.setenv(
        "WEBGPT_ACCOUNTS_FILE", str(tmp_path / "accounts" / "accounts.json")
    )
    monkeypatch.setenv("WEBGPT_PROFILES_ROOT", str(tmp_path / "profiles"))
    # Deadline override để test chạy nhanh thay vì chờ timeout+grace thật.
    monkeypatch.setenv("WEBGPT_LOGIN_DEADLINE_SECONDS", "0.2")

    captured = {}

    class HangingAutoManager:
        def __init__(self, *, profile_dir, headless, executable_path):
            captured["profile_dir"] = profile_dir

        async def login(self, credentials, timeout_seconds):
            captured["timeout_seconds"] = timeout_seconds
            await asyncio.sleep(30)  # mô phỏng slow flow; chỉ deadline cứu được

    monkeypatch.setattr("gpt.auth.AutoLoginManager", HangingAutoManager)
    monkeypatch.setattr(
        "gpt.auth.accounts.find_cloak_executable", lambda: str(tmp_path / "cloak")
    )

    args = _args(
        name="slowpoke",
        auto=True,
        url="https://chatgpt.com/",
        wait_seconds=300,
        headful_auto=False,
        use_saved=False,
        save_credentials=False,
        cred="user@example.com|pass|TOTP",
        timeout=15,
    )

    with pytest.raises(SystemExit) as excinfo:
        await cmd_account_login(args)

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "deadline" in err.lower()
    assert "WEBGPT_LOGIN_DEADLINE_SECONDS" in err
    # Ngân sách trong flow vẫn giữ nguyên --timeout.
    assert captured["timeout_seconds"] == 15
    # Helper grace phải là nguồn của deadline (không còn max(1, timeout)).
    monkeypatch.delenv("WEBGPT_LOGIN_DEADLINE_SECONDS")
    assert debug_module._login_flow_deadline_seconds(15) == 195.0


async def test_cmd_account_login_passes_default_deadline_to_wait_for(
    tmp_path, monkeypatch
):
    """Không env override: asyncio.wait_for nhận đúng default --timeout + grace.

    Test cũ chỉ chạy nhánh env-override 0.2; test này chặn asyncio.wait_for
    để bắt giá trị timeout cmd_account_login thực sự truyền vào runtime.
    """
    from gpt.debug import LOGIN_FLOW_GRACE_SECONDS, cmd_account_login

    monkeypatch.setenv(
        "WEBGPT_ACCOUNTS_FILE", str(tmp_path / "accounts" / "accounts.json")
    )
    monkeypatch.setenv("WEBGPT_PROFILES_ROOT", str(tmp_path / "profiles"))
    # Bắt buộc không có override: deadline phải đến từ timeout + grace.
    monkeypatch.delenv("WEBGPT_LOGIN_DEADLINE_SECONDS", raising=False)

    captured = {}

    class InstantAutoManager:
        def __init__(self, *, profile_dir, headless, executable_path):
            pass

        async def login(self, credentials, timeout_seconds):
            return True

    async def spy_wait_for(awaitable, timeout):
        captured["timeout"] = timeout
        awaitable.close()  # không chạy coroutine thật, tránh cảnh báo leak
        return True

    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)
    monkeypatch.setattr("gpt.auth.AutoLoginManager", InstantAutoManager)
    monkeypatch.setattr(
        "gpt.auth.accounts.find_cloak_executable", lambda: str(tmp_path / "cloak")
    )

    args = _args(
        name="default-deadline",
        auto=True,
        url="https://chatgpt.com/",
        wait_seconds=300,
        headful_auto=False,
        use_saved=False,
        save_credentials=False,
        cred="user@example.com|pass|TOTP",
        timeout=15,
    )

    await cmd_account_login(args)

    assert captured["timeout"] == 15 + LOGIN_FLOW_GRACE_SECONDS


async def test_autologin_cloak_context_closed_when_new_page_fails(tmp_path, monkeypatch):
    """Cloak path: new_page() nổ sau khi context đã launch → context.close()."""
    import gpt.auth.authenticator as authenticator_module

    class ExplodingCloakContext:
        instances = []  # noqa: RUF012

        def __init__(self):
            self.pages = []
            self.close_calls = 0
            type(self).instances.append(self)

        async def new_page(self):
            raise RuntimeError("cloak page exploded")

        async def close(self):
            self.close_calls += 1

    async def fake_launch(**kwargs):
        return ExplodingCloakContext()

    monkeypatch.setattr(authenticator_module, "CLOAK_AVAILABLE", True)
    monkeypatch.setattr(
        authenticator_module, "launch_persistent_context_async", fake_launch
    )

    manager = authenticator_module.AutoLoginManager(
        profile_dir=tmp_path / "profile", headless=True
    )
    creds = authenticator_module.LoginCredentials(username="u@example.com", password="pw")

    with pytest.raises(RuntimeError, match="cloak page exploded"):
        await manager.login(creds, timeout_seconds=5)

    [context] = ExplodingCloakContext.instances
    assert context.close_calls == 1


async def test_autologin_cdp_playwright_stopped_when_new_page_fails(tmp_path, monkeypatch):
    """CDP path: new_page() nổ giữa acquisition → context tự tạo đóng TRƯỚC,
    Playwright driver shutdown SAU (đúng thứ tự cleanup)."""
    events: list[str] = []

    class ExplodingCdpContext:
        instances = []  # noqa: RUF012

        def __init__(self):
            self.pages = []
            self.close_calls = 0
            type(self).instances.append(self)

        async def new_page(self):
            raise RuntimeError("cdp page exploded")

        async def close(self):
            self.close_calls += 1
            events.append("close")

    class FakeCdpBrowser:
        def __init__(self):
            self.contexts = []

        async def new_context(self):
            return ExplodingCdpContext()

    class FakeChromium:
        async def connect_over_cdp(self, url):
            return FakeCdpBrowser()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    class RecordingPlaywrightCM:
        instances = []  # noqa: RUF012

        def __init__(self):
            self.exit_calls = 0
            type(self).instances.append(self)

        async def start(self):
            return FakePlaywright()

        async def __aexit__(self, exc_type, exc, tb):
            self.exit_calls += 1
            events.append("exit")

    import gpt.auth.authenticator as authenticator_module

    monkeypatch.setattr(authenticator_module, "async_playwright", RecordingPlaywrightCM)

    manager = authenticator_module.AutoLoginManager(
        profile_dir=tmp_path / "profile",
        headless=True,
        cdp_url="http://127.0.0.1:9222",
    )
    creds = authenticator_module.LoginCredentials(username="u@example.com", password="pw")

    with pytest.raises(RuntimeError, match="cdp page exploded"):
        await manager.login(creds, timeout_seconds=5)

    [cm] = RecordingPlaywrightCM.instances
    [context] = ExplodingCdpContext.instances
    assert cm.exit_calls == 1
    assert context.close_calls == 1
    assert events == ["close", "exit"]


async def test_autologin_cdp_scratch_context_closed_after_flow(tmp_path, monkeypatch):
    """CDP path: context do ta tự tạo (browser không có context sẵn) phải được
    đóng trong finally sau khi flow kết thúc — kể cả khi flow lỗi giữa chừng —
    không để mồi lại trong browser của user; driver vẫn exit đúng một lần."""
    events: list[str] = []

    class ScratchContext:
        instances = []  # noqa: RUF012

        def __init__(self):
            self.pages = []
            self.close_calls = 0
            type(self).instances.append(self)

        async def new_page(self):
            return BoomGotoPage()

        async def close(self):
            self.close_calls += 1
            events.append("close")

    class BoomGotoPage:
        async def goto(self, url, **kwargs):
            raise RuntimeError("boom-goto")

    class FakeCdpBrowser:
        def __init__(self):
            self.contexts = []

        async def new_context(self):
            return ScratchContext()

    class FakeChromium:
        async def connect_over_cdp(self, url):
            return FakeCdpBrowser()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

    class RecordingPlaywrightCM:
        instances = []  # noqa: RUF012

        def __init__(self):
            self.exit_calls = 0
            type(self).instances.append(self)

        async def start(self):
            return FakePlaywright()

        async def __aexit__(self, exc_type, exc, tb):
            self.exit_calls += 1
            events.append("exit")

    import gpt.auth.authenticator as authenticator_module

    monkeypatch.setattr(authenticator_module, "async_playwright", RecordingPlaywrightCM)

    manager = authenticator_module.AutoLoginManager(
        profile_dir=tmp_path / "profile",
        headless=True,
        cdp_url="http://127.0.0.1:9222",
    )
    creds = authenticator_module.LoginCredentials(username="u@example.com", password="pw")

    with pytest.raises(RuntimeError, match="boom-goto"):
        await manager.login(creds, timeout_seconds=5)

    [cm] = RecordingPlaywrightCM.instances
    [scratch] = ScratchContext.instances
    assert cm.exit_calls == 1
    assert scratch.close_calls == 1
    assert events == ["close", "exit"]
