import pytest

from gpt.gateway.runtime import _fanout_requested, _looks_like_tool_directed_task

AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "Agent",
        "description": "Launch a new agent for an independent task.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["description", "prompt"],
        },
    },
}

REPO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object"},
        },
    }
    for name in ("Bash", "Read", "Edit")
]


@pytest.mark.parametrize(
    "prompt",
    [
        "fan out subagents research cách làm 1 bài osint",
        "spawn 5 general-purpose subagents in one message to research five angles",
        "create 7 sub-agents each which return numbers 1-7",
        "assign these work packages to individual agents. initialize all agents at the same time",
        "use sub-agents for any steps that can be parallelized",
        "launch 3 agents simultaneously to inspect unrelated modules",
        "spin up a subagent for each file in this folder",
        "Use the task tool to create 10 parallel tasks",
    ],
)
def test_common_parallel_agent_prompts_are_detected(prompt):
    assert _fanout_requested([{"role": "user", "content": prompt}]) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "spawn a subagent to research OSINT methodology",
        "use the Agent tool to inspect the authentication flow",
        "ask one agent to review this module",
    ],
)
def test_single_agent_prompts_are_not_misclassified_as_fanout(prompt):
    assert _fanout_requested([{"role": "user", "content": prompt}]) is False
    assert _looks_like_tool_directed_task(
        [], [{"role": "user", "content": prompt}], [AGENT_TOOL]
    ) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "debug the failing build in this repo and find the root cause",
        "review the current git diff for bugs",
        "inspect this repo and explain where authentication happens",
        "find and fix the issue causing 500 errors in this project",
        "refactor src/auth.py without changing behavior and run the tests",
        "fix the failing test in tests/test_auth.py and run pytest",
    ],
)
def test_common_repo_action_prompts_require_controller_tools(prompt):
    assert _looks_like_tool_directed_task(
        [], [{"role": "user", "content": prompt}], REPO_TOOLS
    ) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "explain what a Python TypeError means",
        "what is dependency injection?",
        "how would you approach refactoring a large service?",
    ],
)
def test_conceptual_prompts_do_not_force_repo_tools(prompt):
    assert _looks_like_tool_directed_task(
        [], [{"role": "user", "content": prompt}], REPO_TOOLS
    ) is False


@pytest.mark.parametrize(
    "prompt",
    [
        "spawn as many exploring agents as you need to explore and scan everything needed for this task",
        "spin up a subagent for each task that can run in parallel",
        "assign work packages A, B, and C to individual agents and initialize all 3 at the same time",
    ],
)
def test_additional_real_world_fanout_phrasings(prompt):
    assert _fanout_requested([{"role": "user", "content": prompt}]) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "Use the debugger subagent to find the root cause in src/payment/processor.ts",
        "Use the test-writer subagent to create tests for the user profile feature",
        "Use one refactor subagent to execute step 1 of the plan",
    ],
)
def test_real_world_single_subagent_prompts_require_one_agent(prompt):
    assert _fanout_requested([{"role": "user", "content": prompt}]) is False
    assert _looks_like_tool_directed_task(
        [], [{"role": "user", "content": prompt}], [AGENT_TOOL]
    ) is True


@pytest.mark.parametrize(
    "prompt",
    [
        "review this PR, add tests, and update docs",
        "diagnose and fix the CI failure in this branch",
        "audit the execution flow in this repo and identify where data is lost",
        "check the current branch for uncommitted changes",
        "explain how this codebase implements authentication",
        "where is authentication implemented in this repo?",
        "find all API endpoints in this codebase",
        "search this repo for TODO comments",
    ],
)
def test_additional_common_repo_prompts_require_tools(prompt):
    assert _looks_like_tool_directed_task(
        [], [{"role": "user", "content": prompt}], REPO_TOOLS
    ) is True


def test_conceptual_git_diff_question_does_not_force_tools():
    prompt = "explain what a git diff is"
    assert _looks_like_tool_directed_task(
        [], [{"role": "user", "content": prompt}], REPO_TOOLS
    ) is False
