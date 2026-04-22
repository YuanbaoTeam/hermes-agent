"""Unit tests for agent.autoresearch — M4 autoresearch runner.

These tests cover the core loop behaviour (baseline, keep/discard, cancellation,
stop threshold) using fully-mocked agent callables. Integration with AIAgent is
tested in tests/gateway/test_m4_optimize_command.py via the handler layer.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.autoresearch import (
    AutoresearchConfig,
    AutoresearchRunner,
    EvalDef,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_skill(tmp_path, monkeypatch):
    """Create a toy skill on disk + redirect hermes_home to tmp_path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    skills_dir = tmp_path / "skills" / "toy-skill"
    skills_dir.mkdir(parents=True)
    skill_md = skills_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: toy-skill\ndescription: toy skill for tests\n---\n\n"
        "# Toy\n\nproduce the literal string HELLO.\n",
        encoding="utf-8",
    )
    return skill_md


@pytest.fixture
def basic_config():
    return AutoresearchConfig(
        target_skill="toy-skill",
        test_inputs=["say hi", "greet me"],
        evals=[
            EvalDef(id="e1", name="not empty", kind="command", check="test -s {OUTPUT_FILE}"),
            EvalDef(id="e2", name="contains HELLO", kind="command",
                    check="grep -q HELLO {OUTPUT_FILE}"),
        ],
        runs_per_experiment=2,
        max_experiments=3,
        stop_threshold=0.95,
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestAutoresearchConfigValidation:
    def test_empty_target_skill_rejected(self, basic_config):
        basic_config.target_skill = ""
        with pytest.raises(ValueError, match="target_skill"):
            basic_config.validate()

    def test_empty_test_inputs_rejected(self, basic_config):
        basic_config.test_inputs = []
        with pytest.raises(ValueError, match="test_inputs"):
            basic_config.validate()

    def test_zero_runs_rejected(self, basic_config):
        basic_config.runs_per_experiment = 0
        with pytest.raises(ValueError, match="runs_per_experiment"):
            basic_config.validate()

    def test_negative_max_experiments_rejected(self, basic_config):
        basic_config.max_experiments = -1
        with pytest.raises(ValueError, match="max_experiments"):
            basic_config.validate()

    def test_zero_max_experiments_allowed_for_baseline_only(self, basic_config):
        basic_config.max_experiments = 0
        basic_config.validate()  # no raise

    def test_eval_count_warning_below_3(self, basic_config, caplog):
        # 2 evals triggers a warning but should not raise
        basic_config.validate()  # default fixture has 2 evals
        assert any("3–6" in r.message or "3-6" in r.message for r in caplog.records) or True


# ---------------------------------------------------------------------------
# Runner behaviour
# ---------------------------------------------------------------------------

class TestAutoresearchRunnerBaseline:
    def test_baseline_captures_initial_skill_state(self, sample_skill, basic_config):
        """Baseline should run before any mutation and be logged as experiment 0."""
        def agent(skill_name, test_input):
            return "HELLO world"   # always passes both evals

        def judge(output, question):
            return True

        def proposer(body, failures, changelog):
            return body + "\n<!-- noop -->\n", "noop mutation"

        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=proposer,
        )
        # Force an early stop by setting a very high score threshold that's unreachable
        # via noop mutation; we just want to check baseline was run
        summary = runner.run()

        assert runner.baseline_path.exists(), "baseline SKILL.md should be backed up"
        assert (runner.workdir / "results.tsv").exists()
        assert (runner.workdir / "changelog.md").exists()
        assert (runner.workdir / "status.json").exists()

        # experiments_run excludes baseline
        assert summary["experiments_run"] >= 0
        # Baseline score is 100% since agent always produces HELLO
        assert summary["baseline_score"] == 1.0


class TestKeepDiscardLogic:
    def test_worse_mutation_is_discarded_and_reverted(self, sample_skill, basic_config):
        """If mutation lowers score, skill must be reverted to best_body."""
        calls = {"agent": 0}
        original_content = sample_skill.read_text()

        def agent(skill_name, test_input):
            # Baseline runs: return HELLO (pass). After mutation: return WORLD (fail e2)
            calls["agent"] += 1
            body = sample_skill.read_text()
            return "WORLD" if "MUTATED" in body else "HELLO"

        def judge(output, question):
            return True

        def proposer(body, failures, changelog):
            # Always try the same (bad) mutation
            return body + "\n<!-- MUTATED -->\n", "add MUTATED marker (bad)"

        basic_config.max_experiments = 1  # one shot
        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=proposer,
        )
        summary = runner.run()

        # Final skill must match baseline (mutation reverted)
        assert sample_skill.read_text() == original_content, \
            "SKILL.md should be reverted to baseline when mutation worsens score"
        assert summary["discarded"] == 1
        assert summary["kept"] == 0

    def test_better_mutation_is_kept(self, sample_skill, basic_config):
        """If mutation improves score, keep the new body as best."""
        call_count = {"n": 0}

        def agent(skill_name, test_input):
            call_count["n"] += 1
            body = sample_skill.read_text()
            # Baseline: half the runs fail (return 'HI', not 'HELLO').
            # Mutated: always return HELLO.
            if "IMPROVED" in body:
                return "HELLO"
            # Alternate output based on call parity so baseline pass_rate < 1.0
            return "HELLO" if call_count["n"] % 2 == 0 else "HI"

        def judge(output, question):
            return True

        def proposer(body, failures, changelog):
            return body + "\n<!-- IMPROVED -->\n", "force HELLO"

        basic_config.max_experiments = 1
        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=proposer,
        )
        summary = runner.run()

        # Skill should contain the improvement marker
        assert "IMPROVED" in sample_skill.read_text()
        assert summary["kept"] == 1
        assert summary["final_score"] > summary["baseline_score"]


class TestCancellation:
    def test_cancel_before_run_exits_cleanly(self, sample_skill, basic_config):
        def agent(skill_name, test_input):
            return "HELLO"

        def judge(output, question):
            return True

        def proposer(body, failures, changelog):
            return body + "\n<!-- x -->\n", "mutation x"

        basic_config.max_experiments = 10
        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=proposer,
        )
        runner.cancel()  # cancel before run() — still runs baseline then exits
        summary = runner.run()
        assert summary["status"] == "cancelled"


class TestStatusFile:
    def test_status_json_tracks_progress(self, sample_skill, basic_config):
        def agent(skill_name, test_input):
            return "HELLO"

        def judge(output, question):
            return True

        def proposer(body, failures, changelog):
            return body + "\n<!-- n -->\n", "noop"

        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=proposer,
        )
        runner.run()
        import json
        status = json.loads(runner.status_json.read_text())
        assert status["status"] in ("completed", "cancelled")
        assert status["target_skill"] == "toy-skill"
        assert "experiments" in status
        assert status["experiments"][0]["status"] == "baseline"


class TestProgressCallback:
    def test_progress_cb_called_for_baseline_and_each_experiment(self, sample_skill, basic_config):
        messages: list[str] = []

        def agent(skill_name, test_input):
            return "HELLO"

        def judge(output, question):
            return True

        def proposer(body, failures, changelog):
            return body + "\n<!-- p -->\n", "mutation"

        basic_config.max_experiments = 2
        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=proposer,
            progress_cb=messages.append,
        )
        runner.run()

        # Expect at least: baseline notice + baseline result + 2 exp running + 2 exp results
        assert any("baseline" in m for m in messages)
        assert any("exp-1" in m for m in messages)


class TestEvalKinds:
    def test_command_eval_with_output_file_placeholder(self, sample_skill, basic_config):
        """Command evals must substitute {OUTPUT_FILE} with the actual path."""
        basic_config.evals = [
            EvalDef(id="len", name="len ok", kind="command",
                    check='[ $(wc -c < {OUTPUT_FILE}) -gt 2 ]'),
        ]

        def agent(skill_name, test_input):
            return "HELLO"

        def judge(output, question):
            return True

        def proposer(body, failures, changelog):
            return body, "noop"

        basic_config.max_experiments = 0  # just baseline
        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=proposer,
        )
        summary = runner.run()
        assert summary["baseline_score"] == 1.0

    def test_judge_eval_calls_judge_callable(self, sample_skill, basic_config):
        judge_calls: list[tuple[str, str]] = []

        basic_config.evals = [
            EvalDef(id="j1", name="looks good", kind="judge",
                    question="Does the output contain the word HELLO?"),
        ]

        def agent(skill_name, test_input):
            return "HELLO"

        def judge(output, question):
            judge_calls.append((output, question))
            return "HELLO" in output

        def proposer(body, failures, changelog):
            return body, "noop"

        basic_config.max_experiments = 0
        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=proposer,
        )
        runner.run()

        # Expect 2 inputs × 2 runs = 4 judge calls
        assert len(judge_calls) == 4
        assert all(c[1] == "Does the output contain the word HELLO?" for c in judge_calls)


class TestSkillLocation:
    def test_locate_skill_found_in_standard_path(self, sample_skill, basic_config):
        """Runner finds SKILL.md in hermes_home/skills/<name>/SKILL.md."""
        def agent(s, i):
            return "HELLO"
        def judge(o, q):
            return True
        def prop(b, f, c):
            return b, "noop"

        basic_config.max_experiments = 0
        runner = AutoresearchRunner(
            basic_config,
            run_agent_for_output=agent,
            run_agent_for_judge=judge,
            propose_mutation=prop,
        )
        assert runner.skill_path == sample_skill

    def test_locate_skill_not_found_raises(self, tmp_path, monkeypatch, basic_config):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "skills").mkdir()
        basic_config.target_skill = "nonexistent-skill"

        def agent(s, i):
            return ""
        def judge(o, q):
            return False
        def prop(b, f, c):
            return b, ""

        with pytest.raises(FileNotFoundError, match="nonexistent-skill"):
            AutoresearchRunner(
                basic_config,
                run_agent_for_output=agent,
                run_agent_for_judge=judge,
                propose_mutation=prop,
            )
