from gpt.profile import ensure_profile_dir


def test_ensure_profile_dir_keeps_existing_browser_singleton_files(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    singleton = profile / "SingletonLock"
    singleton.write_text("active browser marker", encoding="utf-8")

    result = ensure_profile_dir(profile)

    assert result == profile.resolve()
    assert singleton.exists()
    assert result.stat().st_mode & 0o777 == 0o700

