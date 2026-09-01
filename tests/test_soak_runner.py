"""Unit tests for scripts/bench/soak_runner.py — no real network, no browser.

HTTP is mocked via an injected fake client; process discovery / RSS reads are
exercised only through the pure parsing helpers with synthetic text.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "bench" / "soak_runner.py"

spec = importlib.util.spec_from_file_location("soak_runner", SCRIPT_PATH)
assert spec is not None and spec.loader is not None
soak = importlib.util.module_from_spec(spec)
sys.modules["soak_runner"] = soak  # required: dataclasses resolves __module__
spec.loader.exec_module(soak)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeClient:
    """Records calls; returns queued responses or a canned status."""

    def __init__(self, status: int = 200, latency: float = 0.0):
        self.status = status
        self.latency = latency
        self.calls: list[tuple[str, dict]] = []

    def post_json(self, url: str, payload: dict):
        self.calls.append((url, payload))
        if self.latency:
            import time
            time.sleep(self.latency)
        return self.status, "{}"


class ExplodingClient(FakeClient):
    def post_json(self, url: str, payload: dict):  # pragma: no cover - must not run
        raise AssertionError("no HTTP request may be sent in this test")


def make_turns(n: int, ok: bool = True, latency: float = 5.0) -> list:
    return [
        soak.TurnResult(index=i, ok=ok, status=200 if ok else 500,
                        latency_s=latency)
        for i in range(n)
    ]


def make_samples(baseline_kb: int, final_kb: int, count: int = 10) -> list:
    step = (final_kb - baseline_kb) / max(1, count - 1)
    return [
        soak.Sample(ts=f"t{i}", gateway_rss_kb=int(baseline_kb * 0.3),
                    chrome_rss_kb=int(baseline_kb * 0.7 + step * i))
        for i in range(count)
    ]


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

class TestParsers:
    def test_parse_pgrep_output_basic(self):
        assert soak.parse_pgrep_output("123\n456\n\n789\n") == [123, 456, 789]

    def test_parse_pgrep_output_empty_and_garbage(self):
        assert soak.parse_pgrep_output("") == []
        assert soak.parse_pgrep_output("not-a-pid\n42\n") == [42]

    def test_parse_ss_pids(self):
        out = (
            "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            "LISTEN 0      128      127.0.0.1:18000     0.0.0.0:*"
            " users:((\"python\",pid=4242,fd=7))\n"
            "users:((\"uvicorn\",pid=99,fd=5))\n"
        )
        assert soak.parse_ss_pids(out) == [4242, 99]

    def test_parse_ps_lines_rss_comm_format(self):
        # realistic `ps -o rss=,comm=` output: comm is the binary name
        out = "  20480 chrome\n   1024 cloak-browser\n  512 chrome-sandbox\n"
        rows = soak.parse_ps_lines(out)
        assert rows == [(20480, "chrome"), (1024, "cloak-browser"),
                        (512, "chrome-sandbox")]

    def test_parse_ps_lines_skips_bad_rows(self):
        assert soak.parse_ps_lines("garbage\n\n") == []

    def test_sum_chrome_rss_filters_non_chrome(self):
        rows = [(1000, "chrome"), (500, "python3"), (250, "chromium --type=renderer")]
        assert soak.sum_chrome_rss(rows) == 1250


class TestStats:
    def test_percentile_known_values(self):
        values = [float(x) for x in range(1, 101)]  # 1..100
        assert soak.percentile(values, 50) == pytest.approx(50.5)
        assert soak.percentile(values, 95) == pytest.approx(95.05)
        assert soak.percentile([], 95) == 0.0
        assert soak.percentile([7.0], 95) == 7.0

    def test_rss_growth_flat_is_zero(self):
        samples = make_samples(100_000, 100_000)
        growth, baseline, final = soak.rss_growth_pct(samples)
        assert growth == pytest.approx(0.0, abs=0.01)
        assert baseline == pytest.approx(100_000)
        assert final == pytest.approx(100_000)

    def test_rss_growth_detects_leak(self):
        samples = make_samples(100_000, 200_000, count=20)
        growth, _, _ = soak.rss_growth_pct(samples)
        assert growth > 80.0  # far above the 20% threshold

    def test_rss_growth_too_few_samples(self):
        growth, baseline, final = soak.rss_growth_pct(make_samples(1, 2, count=2))
        assert (growth, baseline, final) == (0.0, 0.0, 0.0)

    def test_sample_total_rss(self):
        s = soak.Sample(ts="t", gateway_rss_kb=300, chrome_rss_kb=700)
        assert s.total_rss_kb == 1000

    def test_leak_tail_ratio(self):
        samples = make_samples(100_000, 130_000, count=12)
        ratio = soak.leak_tail_ratio(samples)
        assert 1.0 < ratio <= 1.31
        assert soak.leak_tail_ratio([]) == 1.0


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #

class TestVerdict:
    def test_pass_on_healthy_run(self):
        turns = make_turns(100, ok=True, latency=5.0)
        samples = make_samples(100_000, 110_000)
        verdict = soak.compute_verdict("stable", turns, samples)
        assert verdict.passed is True
        names = {c["name"] for c in verdict.checks}
        assert {"error_rate_pct", "max_turn_latency_s", "p95_latency_s",
                "rss_growth_pct"} == names

    def test_fail_on_error_rate(self):
        turns = make_turns(100, ok=True)
        for t in turns[:6]:  # 6% errors > 2% threshold
            t.ok = False
        verdict = soak.compute_verdict("stable", turns, [])
        check = next(c for c in verdict.checks if c["name"] == "error_rate_pct")
        assert check["ok"] is False and check["value"] == pytest.approx(6.0)
        assert verdict.passed is False

    def test_fail_on_hung_turn_over_120s(self):
        turns = make_turns(10, ok=True, latency=5.0)
        turns[3].latency_s = 150.0
        verdict = soak.compute_verdict("stable", turns, [])
        check = next(c for c in verdict.checks if c["name"] == "max_turn_latency_s")
        assert check["ok"] is False
        assert verdict.passed is False

    def test_fail_on_rss_growth_over_threshold(self):
        turns = make_turns(50, ok=True, latency=2.0)
        samples = make_samples(100_000, 200_000)  # +100% >> 20%
        verdict = soak.compute_verdict("stable", turns, samples)
        rss = next(c for c in verdict.checks if c["name"] == "rss_growth_pct")
        assert rss["ok"] is False and rss["threshold"] == 20.0
        assert verdict.passed is False

    def test_recovery_requires_recovered_flag(self):
        turns = make_turns(20, ok=True, latency=3.0)
        not_recovered = soak.compute_verdict("recovery", turns, [], recovered=False)
        recovered = soak.compute_verdict("recovery", turns, [], recovered=True)
        def rec_check(v):
            return next(
                    c for c in v.checks if c["name"] == "recovered_after_kill")
        assert rec_check(not_recovered)["ok"] is False
        assert not_recovered.passed is False
        assert rec_check(recovered)["ok"] is True
        assert recovered.passed is True

    def test_burst_thresholds_relaxed(self):
        th = soak.thresholds_for("burst")
        assert th.max_p95_latency_s == 120.0
        assert th.max_rss_growth_pct == 25.0
        recovery_th = soak.thresholds_for("recovery")
        assert recovery_th.max_error_rate_pct == 15.0
        assert recovery_th.require_recovered is True


# --------------------------------------------------------------------------- #
# Markdown report
# --------------------------------------------------------------------------- #

class TestTraceSummary:
    def test_summarize_trace_uses_only_new_completed_requests(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        rows = [
            {"sequence": 1, "kind": "request_completed", "metadata": {"correction_count": 9}},
            {"sequence": 2, "kind": "tool_correction", "metadata": {}},
            {"sequence": 3, "kind": "request_completed", "metadata": {"correction_count": 2}},
            {"sequence": 4, "kind": "request_completed", "metadata": {"correction_count": 0}},
        ]
        trace.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

        assert soak.trace_last_sequence(trace) == 4
        summary = soak.summarize_trace(trace, after_sequence=1)
        assert summary.request_count == 2
        assert summary.corrected_requests == 1
        assert summary.correction_count == 2
        assert summary.max_corrections_per_request == 2

    def test_summarize_trace_skips_malformed_and_invalid_counts(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        trace.write_text(
            '{bad json}\n'
            + json.dumps({"sequence": 1, "kind": "request_completed", "metadata": {"correction_count": "2"}})
            + "\n",
            encoding="utf-8",
        )
        summary = soak.summarize_trace(trace)
        assert summary.request_count == 1
        assert summary.correction_count == 0
        assert summary.malformed_lines == 1


class TestMarkdownReport:
    def _render(self, scenario="stable", recovered=None, killed_pid=None,
                events=None):
        turns = make_turns(30, ok=True, latency=4.0)
        if scenario == "recovery":
            for i, t in enumerate(turns):
                t.phase = "run" if i < 15 else "post-kill"
        samples = make_samples(100_000, 105_000)
        verdict = soak.compute_verdict(scenario, turns, samples,
                                       recovered=recovered)
        return soak.render_markdown(
            scenario=scenario, target="http://127.0.0.1:18000", turns=turns,
            samples=samples, verdict=verdict, events=events or [],
            started_at="2026-08-24T00:00:00+00:00",
            finished_at="2026-08-24T01:00:00+00:00",
            interval_s=10, concurrency=1, killed_pid=killed_pid,
        )

    def test_structure_contains_required_sections(self):
        md = self._render()
        assert md.startswith("# Soak Report: stable")
        for heading in ("## Latency", "## Memory (gateway + chrome children)",
                        "## Verdict:", "## Per-turn detail"):
            assert heading in md
        assert "| p50 |" in md and "| p95 |" in md
        assert "`http://127.0.0.1:18000`" in md

    def test_verdict_line_reflects_result(self):
        passing = self._render()
        assert "## Verdict: PASS" in passing

        turns = make_turns(5, ok=False, latency=999.0)
        verdict = soak.compute_verdict("stable", turns, [])
        from_context = soak.render_markdown(
            scenario="stable", target="x", turns=turns,
            samples=make_samples(10, 11), verdict=verdict, events=[],
            started_at="a", finished_at="b", interval_s=1, concurrency=1)
        assert "## Verdict: FAIL" in from_context

    def test_recovery_section_present_with_kill_info(self):
        md = self._render(scenario="recovery", recovered=True, killed_pid=777,
                          events=["killed chrome child pid 777"])
        assert "## Recovery" in md
        assert "777" in md
        assert "Recovered: yes" in md
        assert "- killed chrome child pid 777" in md

    def test_trace_summary_section_is_rendered(self):
        turns = make_turns(3, ok=True, latency=1.0)
        verdict = soak.compute_verdict("stable", turns, [])
        md = soak.render_markdown(
            scenario="stable", target="x", turns=turns, samples=[], verdict=verdict,
            events=[], started_at="a", finished_at="b", interval_s=1, concurrency=1,
            trace_summary=soak.TraceSummary(
                request_count=3, corrected_requests=1, correction_count=2,
                max_corrections_per_request=2,
            ),
        )
        assert "## Gateway correction telemetry" in md
        assert "Requests requiring correction: 1" in md
        assert "Corrections sent: 2" in md

    def test_long_turn_table_truncated(self):
        turns = make_turns(60, ok=True, latency=1.0)
        verdict = soak.compute_verdict("stable", turns, [])
        md = soak.render_markdown(
            scenario="stable", target="x", turns=turns,
            samples=[], verdict=verdict, events=[],
            started_at="a", finished_at="b", interval_s=1, concurrency=1)
        assert "showing first 20 and last 20 of 60 turns" in md
        # turn index 30 (middle) must be omitted
        body_rows = [line for line in md.splitlines() if line.startswith("| 30 ")]
        assert body_rows == []


# --------------------------------------------------------------------------- #
# Turn request format
# --------------------------------------------------------------------------- #

class TestTurnExecution:
    def test_run_turn_hits_chat_completions_with_small_prompt(self):
        client = FakeClient(status=200)
        result = soak.run_turn(client, "http://127.0.0.1:18000", index=7)
        assert len(client.calls) == 1
        url, payload = client.calls[0]
        assert url == "http://127.0.0.1:18000/v1/chat/completions"
        assert payload["messages"][0]["content"] == soak.PROMPT
        assert payload["stream"] is False
        assert result.ok is True and result.status == 200 and result.index == 7

    def test_run_turn_marks_non_200_as_failure(self):
        client = FakeClient(status=429)
        result = soak.run_turn(client, "http://h", index=0)
        assert result.ok is False
        assert "429" in result.error

    def test_turn_dict_jsonl_shape(self):
        d = soak.run_turn(FakeClient(), "http://h", index=1).to_dict()
        assert d["kind"] == "turn"
        json.dumps(d)  # must be JSON-serializable


# --------------------------------------------------------------------------- #
# CLI behaviour: dry-run, safety cap
# --------------------------------------------------------------------------- #

BASE_ARGS = ["--scenario", "stable", "--port", "18000"]


class TestCliSafety:
    def test_dry_run_sends_nothing(self, tmp_path, capsys):
        rc = soak.main([*BASE_ARGS, "--turns", "5", "--interval", "1", "--dry-run", "--report-dir", str(tmp_path)],
                       client=ExplodingClient())
        out = capsys.readouterr().out
        assert rc == 0
        assert "DRY-RUN" in out
        plan = json.loads(out[:out.index("\n}") + 2])
        assert plan["scenario"] == "stable"
        assert plan["target"] == "http://127.0.0.1:18000"
        assert plan["mode"].startswith("DRY-RUN")

    def test_turn_cap_without_long_flag_refused(self, capsys):
        rc = soak.main(["--scenario", "stable", "--turns", "501"],
                       client=ExplodingClient())
        assert rc == 2
        err = capsys.readouterr().err
        assert "--i-know-this-is-long" in err

    def test_invalid_turn_count_refused(self):
        assert soak.main([*BASE_ARGS, "--turns", "0"]) == 2

    def test_build_plan_reports_cap_violation(self, tmp_path):
        ns = soak.parse_args(["--scenario", "leak", "--turns", "600"])
        ns.report_dir = tmp_path
        plan = soak.build_plan(ns)
        assert plan["would_refuse_without_long_flag"] is True
        ns_ok = soak.parse_args(["--scenario", "leak", "--turns", "600",
                                 "--i-know-this-is-long"])
        ns_ok.report_dir = tmp_path
        assert soak.build_plan(ns_ok)["would_refuse_without_long_flag"] is False


# --------------------------------------------------------------------------- #
# JSONL writer
# --------------------------------------------------------------------------- #

class TestJsonlWriter:
    def test_append_jsonl_writes_one_record_per_call(self, tmp_path):
        path = tmp_path / "metrics.jsonl"
        soak.append_jsonl(path, {"kind": "sample", "v": 1})
        soak.append_jsonl(path, {"kind": "turn", "v": 2})
        lines = path.read_text().strip().splitlines()
        assert [json.loads(x) for x in lines] == [
            {"kind": "sample", "v": 1}, {"kind": "turn", "v": 2}]

    def test_append_jsonl_none_path_is_noop(self):
        soak.append_jsonl(None, {"kind": "sample"})  # must not raise
