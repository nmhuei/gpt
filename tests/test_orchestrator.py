import pytest
from pathlib import Path
from gpt.orchestrator.types import ChallengeTask, ChallengeStatus, SolvingStrategy
from gpt.orchestrator.master_agent import MasterAgentOrchestrator

def test_challenge_task_initial_state(tmp_path: Path):
    task = ChallengeTask(
        directory=tmp_path,
        name="Test Challenge",
        category="Web",
        points=100
    )
    assert task.status == ChallengeStatus.PENDING
    assert not task.is_finished
    assert task.attempt == 0
    assert task.current_strategy == SolvingStrategy.STANDARD_TRIAGE

def test_discover_challenges_single(tmp_path: Path):
    (tmp_path / "metadata.json").write_text('{"name": "Web 1", "category": "Web", "points": 150}')
    orchestrator = MasterAgentOrchestrator(concurrency=2)
    tasks = orchestrator.discover_challenges(tmp_path)
    assert len(tasks) == 1
    assert tasks[0].name == "Web 1"
    assert tasks[0].category == "Web"
    assert tasks[0].points == 150

def test_discover_challenges_recursive(tmp_path: Path):
    c1 = tmp_path / "Web" / "Web_1"
    c2 = tmp_path / "Crypto" / "Crypto_1"
    c1.mkdir(parents=True)
    c2.mkdir(parents=True)
    (c1 / "metadata.json").write_text('{"name": "Web 1", "category": "Web", "points": 100}')
    (c2 / "metadata.json").write_text('{"name": "Crypto 1", "category": "Crypto", "points": 200}')

    orchestrator = MasterAgentOrchestrator(concurrency=4)
    tasks = orchestrator.discover_challenges(tmp_path)
    assert len(tasks) == 2
    names = {t.name for t in tasks}
    assert "Web 1" in names
    assert "Crypto 1" in names
