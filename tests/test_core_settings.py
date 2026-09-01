from __future__ import annotations

from gpt.core.paths import WebGPTPaths
from gpt.core.settings import Settings


def test_settings_precedence_cli_env_project_user(tmp_path):
    paths = WebGPTPaths(
        config_home=tmp_path / "cfg",
        data_home=tmp_path / "data",
        state_home=tmp_path / "state",
        cache_home=tmp_path / "cache",
        runtime_home=tmp_path / "run",
    )
    paths.config_home.mkdir()
    paths.config_file.write_text('[agent]\nmodel="user"\nmax_rounds=3\n')
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "webgpt.toml").write_text('[agent]\nmodel="project"\nmax_rounds=4\n')

    settings = Settings.load(
        workspace=workspace,
        paths=paths,
        environ={"WEBGPT_DIRECT_MODEL": "env", "WEBGPT_MAX_ROUNDS": "5"},
        overrides={"model": "cli"},
    )
    assert settings.model == "cli"
    assert settings.max_rounds == 5


def test_settings_feature_defaults_match_gateway_defaults(tmp_path):
    settings = Settings.load(workspace=tmp_path, environ={})
    assert settings.image_upload is False
    assert settings.fconv_resume is False
    assert settings.usage_poll_seconds == 0.0


def test_settings_invalid_values_fall_back_safely(tmp_path):
    settings = Settings.load(
        workspace=tmp_path,
        environ={
            "WEBGPT_MAX_ROUNDS": "0",
            "WEBGPT_VERIFY": "nonsense",
            "WEBGPT_USAGE_POLL_SECONDS": "-5",
        },
    )
    assert settings.max_rounds == 1
    assert settings.verify == "auto"
    assert settings.usage_poll_seconds == 0
