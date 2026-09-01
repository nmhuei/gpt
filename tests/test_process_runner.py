from __future__ import annotations

from pathlib import Path

from gpt.tools.patch import ApplyPatchTool
from gpt.tools.process import ProcessRunner


def test_process_runner_decodes_non_utf8_without_crashing(tmp_path: Path):
    runner = ProcessRunner(default_timeout_seconds=5, max_output_chars=1000)
    result = runner.run(
        "python -c 'import os; os.write(1, bytes([0x41, 0x99, 0x42]))'",
        cwd=tmp_path,
    )
    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.stdout.startswith("A")
    assert result.stdout.endswith("B")
    assert "\ufffd" in result.stdout


def test_process_runner_timeout_returns_partial_result(tmp_path: Path):
    runner = ProcessRunner(default_timeout_seconds=0.1, terminate_grace_seconds=0.05)
    result = runner.run(
        "printf before; sleep 5; printf after",
        cwd=tmp_path,
    )
    assert result.status == "timeout"
    assert result.exit_code == 124
    assert result.timed_out
    assert "before" in result.stdout
    assert "after" not in result.stdout


def test_apply_patch_tool_applies_workspace_relative_diff(tmp_path: Path):
    target = tmp_path / "a.txt"
    target.write_text("old\n", encoding="utf-8")
    tool = ApplyPatchTool(tmp_path, ProcessRunner())
    result = tool.execute(
        {
            "patch": (
                "--- a.txt\n"
                "+++ a.txt\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n"
            )
        }
    )
    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "new\n"
    assert result.changed_files == ("a.txt",)


def test_apply_patch_rejects_parent_escape(tmp_path: Path):
    tool = ApplyPatchTool(tmp_path, ProcessRunner())
    result = tool.execute(
        {
            "patch": (
                "--- ../outside.txt\n"
                "+++ ../outside.txt\n"
                "@@ -0,0 +1 @@\n"
                "+nope\n"
            )
        }
    )
    assert result.is_error
    assert "escapes" in (result.error or "")
