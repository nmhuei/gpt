from gpt.config.settings import AppConfig, load_config


def test_load_config_standard_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CHATGPT_EMAIL=user@example.com\n"
        "CHATGPT_PASSWORD=secret_pass\n"
        "CHATGPT_TOTP_KEY=ABCDEF123456\n"
        "CDP_PORT=9333\n"
        "API_PORT=8111\n"
        "BROWSER_HEADLESS=false\n"
        "DEFAULT_MODEL=gpt-5-5-thinking\n"
        "DEFAULT_EFFORT=high\n"
        "MAX_WORKERS=5\n"
    )
    config = load_config(env_file)
    assert config.email == "user@example.com"
    assert config.password == "secret_pass"
    assert config.totp_key == "ABCDEF123456"
    assert config.cdp_port == 9333
    assert config.api_port == 8111
    assert config.headless is False
    assert config.default_model == "gpt-5-5-thinking"
    assert config.default_effort == "high"
    assert config.max_workers == 5

def test_load_config_legacy_pipe_fallback(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("my_email@icloud.com|P@ss123|TOTPSECRET999\n")
    config = load_config(env_file)
    assert config.email == "my_email@icloud.com"
    assert config.password == "P@ss123"
    assert config.totp_key == "TOTPSECRET999"
    # Should maintain default ports
    assert config.cdp_port == 9222
    assert config.api_port == 8000
    assert config.headless is True

def test_config_masked_credentials():
    config = AppConfig(
        email="test_user@gmail.com",
        password="MySuperSecretPassword",
        totp_key="JBSWY3DPEHPK3PXP"
    )
    summary = config.masked_summary()
    assert "MySuperSecretPassword" not in summary
    assert "JBSWY3DPEHPK3PXP" not in summary
    assert "test_user@gmail.com" in summary
    assert "password_set=True" in summary
