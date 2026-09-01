import pytest


@pytest.fixture(autouse=True)
def _scrub_host_env(monkeypatch):
    # Shell máy thật export credential/model config; precedence environ > .env
    # của load_config sẽ đè .env của test nếu không scrub. Bổ sung cho nhóm
    # CHATGPT_*/config đã scrub riêng trong test_config_settings.py và
    # test_debug_login.py.
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "CLAUDE_DEFAULT_MODEL",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "WEBGPT_DEFAULT_ACCOUNT",
        # LIMIT-SIGNATURE-TAXONOMY tick (2026-08-26): registry paths phải bị
        # scrub nữa để test không bao giờ chạm accounts.json / profile thật
        # của máy ngay cả khi env set lạ (gpt/auth/accounts.py đọc 2 biến
        # này trước khi rơi về default dưới repo).
        "WEBGPT_ACCOUNTS_FILE",
        "WEBGPT_PROFILES_ROOT",
        # XDG layout migrate (2026-08-26): runtime root bị đọc import-time
        # trong gpt/utils/runtime_paths.py nên một export lạ là test chạm
        # ngay thư mục thật; PROFILE_DIR cùng nhóm (settings + preflight).
        "WEBGPT_RUNTIME_ROOT",
        "PROFILE_DIR",
        # Tránh test dump vào logs thật / đụng auth codex thật.
        "WEBGPT_PROMPT_DEBUG_DIR",
        "WEBGPT_CODEX_AUTH_JSON",
    ):
        monkeypatch.delenv(name, raising=False)
