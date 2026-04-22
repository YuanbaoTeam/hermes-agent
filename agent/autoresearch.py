"""Autoresearch runner — M4 autonomous skill optimization.

Implements Karpathy's autoresearch methodology adapted for Hermes skills:
baseline → hypothesis → experiment → score → keep/discard → log → repeat.

Entry point: `AutoresearchRunner.run()`.

Architecture:
  - Each run gets an isolated workdir under `<hermes_home>/autoresearch/<skill>-<ts>/`
  - Target skill file is backed up to `SKILL.md.baseline` before any mutation
  - Every experiment is logged to `results.tsv` (score log) and `changelog.md` (rationale)
  - Status is written to `status.json` for `/optimize status <run-id>` inspection
  - On each experiment: spawn child AIAgent that loads the target skill and produces
    output for each test input; evaluate via command evals (exit code) and judge evals
    (short-running AIAgent that answers YES/NO).

This is the Alpha MVP:
  - Command + Judge eval modes
  - LLM-based mutation proposer (reads changelog, suggests one change)
  - Keep/discard with atomic SKILL.md revert
  - No HTML dashboard (use tail -f changelog.md)
  - No cron auto-trigger (CLI/gateway only)

See ~/knowledge/14-planning/autoresearch-feasibility-report.md for full design.
See ~/knowledge/hermes-skills/autoresearch/SKILL.md for the methodology spec.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class EvalDef:
    """A single binary evaluation criterion.

    kind='command': `check` is a bash script; exit code 0 = pass.
                    `{OUTPUT_FILE}` in the script is replaced with the path
                    to the run's output.
    kind='judge':   `question` is a yes/no question posed to a short-lived
                    judge agent that sees only (output, question).
    """
    id: str
    name: str
    kind: str  # 'command' | 'judge'
    check: str = ""     # for kind='command'
    question: str = ""  # for kind='judge'


@dataclasses.dataclass
class ExperimentResult:
    """Outcome of one autoresearch experiment (N runs × M evals)."""
    experiment_id: int
    pass_count: int
    max_score: int
    pass_rate: float   # 0.0–1.0
    status: str        # 'baseline' | 'keep' | 'discard'
    description: str
    per_eval_pass: dict[str, int]  # eval_id -> pass count
    duration_s: float


@dataclasses.dataclass
class AutoresearchConfig:
    target_skill: str                  # skill name (must exist under hermes_home/skills/)
    test_inputs: list[str]             # diverse prompts to exercise the skill
    evals: list[EvalDef]               # binary yes/no checks
    runs_per_experiment: int = 5
    max_experiments: int = 20
    stop_threshold: float = 0.95       # stop if 3 consecutive ≥ this
    mode: str = "autonomous"           # 'interactive' (ask before each mutation) | 'autonomous'

    def validate(self) -> None:
        if not self.target_skill:
            raise ValueError("target_skill is required")
        if not self.test_inputs or len(self.test_inputs) < 1:
            raise ValueError("test_inputs must have at least 1 entry (3–5 recommended)")
        if not self.evals or not (3 <= len(self.evals) <= 6):
            # Per Karpathy: >6 causes skill to game evals; <3 is under-signal
            logger.warning(
                "eval count = %d; recommended range is 3–6", len(self.evals)
            )
        if self.runs_per_experiment < 1:
            raise ValueError("runs_per_experiment must be >= 1")
        if self.max_experiments < 0:
            raise ValueError("max_experiments must be >= 0 (0 means baseline-only)")


# ---------------------------------------------------------------------------
# AutoresearchRunner
# ---------------------------------------------------------------------------

class AutoresearchRunner:
    """Orchestrates the autoresearch loop for one target skill.

    Typical usage (from a gateway handler):

        cfg = AutoresearchConfig(target_skill="obsidian-enhanced", test_inputs=[...], evals=[...])
        runner = AutoresearchRunner(
            cfg,
            run_agent_for_output=my_agent_callable,
            run_agent_for_judge=my_judge_callable,
            propose_mutation=my_proposer_callable,
            progress_cb=progress_handler,
        )
        result = runner.run()

    All model-call callables are injected so this module stays decoupled from
    run_agent.AIAgent (easier to test, easier to stub).
    """

    def __init__(
        self,
        cfg: AutoresearchConfig,
        *,
        run_agent_for_output: Callable[[str, str], str],
        run_agent_for_judge: Callable[[str, str], bool],
        propose_mutation: Callable[[str, str, list[dict]], tuple[str, str]],
        progress_cb: Optional[Callable[[str], None]] = None,
        run_id: Optional[str] = None,
    ):
        """
        Args:
            run_agent_for_output: (skill_name, test_input) -> output string.
                The callable should spawn a child agent loaded with the target
                skill and return whatever the skill produces.
            run_agent_for_judge: (output, question) -> bool (True = YES/pass).
                Stub for now; caller wires to a short AIAgent.
            propose_mutation: (skill_body, failure_summary, changelog) -> (new_skill_body, description).
                Returns the full replacement SKILL.md body and a 1-sentence describing the change.
            progress_cb: optional callback for status messages (e.g. send to chat).
            run_id: if None, auto-generated from timestamp.
        """
        cfg.validate()
        self.cfg = cfg
        self.run_agent_for_output = run_agent_for_output
        self.run_agent_for_judge = run_agent_for_judge
        self.propose_mutation = propose_mutation
        self.progress_cb = progress_cb or (lambda _msg: None)

        self.run_id = run_id or f"{cfg.target_skill}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.workdir = get_hermes_home() / "autoresearch" / self.run_id
        self.workdir.mkdir(parents=True, exist_ok=True)

        self.skill_path = self._locate_skill_file(cfg.target_skill)
        self.baseline_path = self.workdir / "SKILL.md.baseline"
        self.results_tsv = self.workdir / "results.tsv"
        self.changelog_md = self.workdir / "changelog.md"
        self.status_json = self.workdir / "status.json"
        self.outputs_dir = self.workdir / "outputs"
        self.outputs_dir.mkdir(exist_ok=True)

        self.results: list[ExperimentResult] = []
        self.best_body: str = ""   # SKILL.md body that holds the current best score
        self.cancelled: bool = False

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _locate_skill_file(skill_name: str) -> Path:
        """Find <skill_name>/SKILL.md in hermes_home/skills or configured external dirs.

        Looks up standard locations in priority order:
          1. <hermes_home>/skills/<name>/SKILL.md
          2. <hermes_home>/skills/<category>/<name>/SKILL.md (scan subdirs)
          3. external_dirs from config.yaml (e.g. ~/knowledge/hermes-skills/<name>/SKILL.md)
        """
        hh = get_hermes_home()
        candidates: list[Path] = [hh / "skills" / skill_name / "SKILL.md"]
        # Category-nested layout
        skills_root = hh / "skills"
        if skills_root.is_dir():
            for cat_dir in skills_root.iterdir():
                if cat_dir.is_dir():
                    candidates.append(cat_dir / skill_name / "SKILL.md")
        # External dirs
        try:
            import yaml  # type: ignore
            cfg_path = hh / "config.yaml"
            if cfg_path.exists():
                user_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
                ext_dirs = (user_cfg.get("skills") or {}).get("external_dirs") or []
                for d in ext_dirs:
                    p = Path(d).expanduser() / skill_name / "SKILL.md"
                    candidates.append(p)
        except Exception as e:
            logger.debug("Failed to scan external_dirs: %s", e)

        for c in candidates:
            if c.exists():
                return c
        raise FileNotFoundError(
            f"Could not locate SKILL.md for '{skill_name}'. Tried: "
            + ", ".join(str(p) for p in candidates[:5]) + (" ..." if len(candidates) > 5 else "")
        )

    def _backup_skill(self) -> None:
        """Copy the target SKILL.md to workdir/SKILL.md.baseline before mutations."""
        if not self.baseline_path.exists():
            shutil.copy2(self.skill_path, self.baseline_path)
            logger.info("autoresearch: backed up %s → %s", self.skill_path, self.baseline_path)

    def _read_skill(self) -> str:
        return self.skill_path.read_text(encoding="utf-8")

    def _write_skill(self, body: str) -> None:
        self.skill_path.write_text(body, encoding="utf-8")

    def _append_tsv(self, result: ExperimentResult) -> None:
        header = "experiment\tscore\tmax_score\tpass_rate\tstatus\tdescription\n"
        if not self.results_tsv.exists():
            self.results_tsv.write_text(header, encoding="utf-8")
        with self.results_tsv.open("a", encoding="utf-8") as f:
            f.write(
                f"{result.experiment_id}\t{result.pass_count}\t{result.max_score}\t"
                f"{result.pass_rate*100:.1f}%\t{result.status}\t{result.description}\n"
            )

    def _append_changelog(
        self,
        exp_n: int,
        status: str,
        mutation_desc: str,
        result: ExperimentResult,
        reasoning: str = "",
    ) -> None:
        entry = (
            f"\n## Experiment {exp_n} — {status}\n\n"
            f"**Score:** {result.pass_count}/{result.max_score} ({result.pass_rate*100:.1f}%)\n"
            f"**Change:** {mutation_desc}\n"
        )
        if reasoning:
            entry += f"**Reasoning:** {reasoning}\n"
        per_eval_lines = "\n".join(
            f"- {eid}: {cnt}/{self.cfg.runs_per_experiment * len(self.cfg.test_inputs)}"
            for eid, cnt in result.per_eval_pass.items()
        )
        entry += f"**Per-eval:**\n{per_eval_lines}\n"
        with self.changelog_md.open("a", encoding="utf-8") as f:
            f.write(entry)

    def _write_status(self, status: str, current_exp: int) -> None:
        """Write status.json for /optimize status inspection."""
        data = {
            "run_id": self.run_id,
            "target_skill": self.cfg.target_skill,
            "skill_path": str(self.skill_path),
            "workdir": str(self.workdir),
            "status": status,  # 'running' | 'completed' | 'cancelled' | 'failed'
            "current_experiment": current_exp,
            "max_experiments": self.cfg.max_experiments,
            "baseline_score": self.results[0].pass_rate if self.results else None,
            "best_score": max((r.pass_rate for r in self.results), default=0.0),
            "kept_count": sum(1 for r in self.results if r.status == "keep"),
            "discarded_count": sum(1 for r in self.results if r.status == "discard"),
            "experiments": [
                {
                    "id": r.experiment_id,
                    "score": r.pass_count,
                    "max_score": r.max_score,
                    "pass_rate": r.pass_rate,
                    "status": r.status,
                    "description": r.description,
                }
                for r in self.results
            ],
            "updated_at": datetime.now().isoformat(),
        }
        self.status_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def _evaluate(self, output: str, output_file: Path) -> dict[str, bool]:
        """Run every eval against a single output. Returns {eval_id: passed}."""
        output_file.write_text(output, encoding="utf-8")
        results: dict[str, bool] = {}
        for ev in self.cfg.evals:
            try:
                if ev.kind == "command":
                    script = ev.check.replace("{OUTPUT_FILE}", str(output_file))
                    proc = subprocess.run(
                        ["bash", "-c", script],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    results[ev.id] = proc.returncode == 0
                elif ev.kind == "judge":
                    results[ev.id] = bool(self.run_agent_for_judge(output, ev.question))
                else:
                    logger.warning("unknown eval kind: %s (eval=%s)", ev.kind, ev.id)
                    results[ev.id] = False
            except subprocess.TimeoutExpired:
                logger.warning("eval %s timed out", ev.id)
                results[ev.id] = False
            except Exception as e:
                logger.warning("eval %s failed: %s", ev.id, e)
                results[ev.id] = False
        return results

    def _run_experiment(self, exp_n: int) -> ExperimentResult:
        """Run the target skill N times × M inputs, score everything, aggregate."""
        t0 = time.time()
        per_eval_pass: dict[str, int] = {e.id: 0 for e in self.cfg.evals}
        pass_count = 0
        total_evals = 0

        exp_dir = self.outputs_dir / f"exp-{exp_n}"
        exp_dir.mkdir(exist_ok=True)

        for i, test_input in enumerate(self.cfg.test_inputs):
            for run_idx in range(self.cfg.runs_per_experiment):
                if self.cancelled:
                    break
                try:
                    output = self.run_agent_for_output(self.cfg.target_skill, test_input)
                except Exception as e:
                    logger.warning("skill run failed (input=%d run=%d): %s", i, run_idx, e)
                    output = f"<<skill-run-error: {e}>>"
                out_file = exp_dir / f"in{i}-run{run_idx}.txt"
                scores = self._evaluate(output, out_file)
                for eid, passed in scores.items():
                    if passed:
                        per_eval_pass[eid] += 1
                        pass_count += 1
                    total_evals += 1
            if self.cancelled:
                break

        pass_rate = (pass_count / total_evals) if total_evals else 0.0

        return ExperimentResult(
            experiment_id=exp_n,
            pass_count=pass_count,
            max_score=total_evals,
            pass_rate=pass_rate,
            status="pending",          # overwritten by caller
            description="",            # overwritten by caller
            per_eval_pass=per_eval_pass,
            duration_s=time.time() - t0,
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Run the full autoresearch loop. Returns a summary dict."""
        self._backup_skill()
        original_body = self._read_skill()
        self.best_body = original_body

        # Initialize changelog
        self.changelog_md.write_text(
            f"# Autoresearch run: {self.run_id}\n\n"
            f"**Target skill:** `{self.cfg.target_skill}`\n"
            f"**Skill path:** `{self.skill_path}`\n"
            f"**Workdir:** `{self.workdir}`\n"
            f"**Config:** runs_per_experiment={self.cfg.runs_per_experiment}, "
            f"max_experiments={self.cfg.max_experiments}, "
            f"stop_threshold={self.cfg.stop_threshold*100:.0f}%\n"
            f"**Test inputs:** {len(self.cfg.test_inputs)}\n"
            f"**Evals:** {len(self.cfg.evals)} "
            f"(command={sum(1 for e in self.cfg.evals if e.kind=='command')}, "
            f"judge={sum(1 for e in self.cfg.evals if e.kind=='judge')})\n",
            encoding="utf-8",
        )

        # --- Baseline (experiment 0) ---
        self.progress_cb(f"🔬 baseline [{self.cfg.target_skill}] — running...")
        self._write_status("running", 0)
        baseline = self._run_experiment(0)
        baseline.status = "baseline"
        baseline.description = "original skill — no changes"
        self.results.append(baseline)
        self._append_tsv(baseline)
        self._append_changelog(0, "baseline", "—", baseline)
        self._write_status("running", 0)
        self.progress_cb(
            f"📊 baseline = {baseline.pass_rate*100:.1f}% "
            f"({baseline.pass_count}/{baseline.max_score})"
        )

        # --- Experiment loop ---
        consecutive_above = 0
        current_score = baseline.pass_rate

        for exp_n in range(1, self.cfg.max_experiments + 1):
            if self.cancelled:
                self.progress_cb(f"🛑 cancelled at exp {exp_n}")
                self._write_status("cancelled", exp_n)
                break

            # Analyse failures + propose mutation
            failure_summary = self._summarise_failures(self.results[-1])
            changelog_entries = [
                {
                    "exp": r.experiment_id,
                    "status": r.status,
                    "description": r.description,
                    "pass_rate": r.pass_rate,
                }
                for r in self.results
            ]
            try:
                new_body, mutation_desc = self.propose_mutation(
                    self._read_skill(),
                    failure_summary,
                    changelog_entries,
                )
            except Exception as e:
                logger.exception("propose_mutation failed at exp %d: %s", exp_n, e)
                self.progress_cb(f"⚠️  proposer failed at exp {exp_n}: {e} — stopping")
                break

            # Apply mutation
            self._write_skill(new_body)

            # Experiment
            self.progress_cb(f"🔬 exp-{exp_n} running — change: {mutation_desc[:80]}")
            result = self._run_experiment(exp_n)

            # Keep or discard
            if result.pass_rate > current_score + 1e-9:
                result.status = "keep"
                result.description = mutation_desc
                current_score = result.pass_rate
                self.best_body = new_body
                decision = "✅ KEEP"
            else:
                result.status = "discard"
                result.description = mutation_desc
                self._write_skill(self.best_body)  # revert
                decision = "↩️  DISCARD"

            self.results.append(result)
            self._append_tsv(result)
            self._append_changelog(exp_n, result.status, mutation_desc, result)
            self._write_status("running", exp_n)

            self.progress_cb(
                f"{decision} exp-{exp_n} = {result.pass_rate*100:.1f}% "
                f"({result.pass_count}/{result.max_score}) — {mutation_desc[:80]}"
            )

            # Stop threshold (3 consecutive above)
            if current_score >= self.cfg.stop_threshold:
                consecutive_above += 1
                if consecutive_above >= 3:
                    self.progress_cb(
                        f"🎯 hit {self.cfg.stop_threshold*100:.0f}% × 3 consecutive — stopping"
                    )
                    break
            else:
                consecutive_above = 0

        # --- Finalize ---
        # Ensure skill file holds best_body on exit (in case of cancel mid-experiment)
        self._write_skill(self.best_body)

        final_status = "cancelled" if self.cancelled else "completed"
        self._write_status(final_status, self.results[-1].experiment_id if self.results else 0)

        kept = sum(1 for r in self.results if r.status == "keep")
        discarded = sum(1 for r in self.results if r.status == "discard")
        final_score = max((r.pass_rate for r in self.results), default=0.0)

        summary = {
            "run_id": self.run_id,
            "workdir": str(self.workdir),
            "target_skill": self.cfg.target_skill,
            "status": final_status,
            "baseline_score": baseline.pass_rate,
            "final_score": final_score,
            "improvement_pct": (final_score - baseline.pass_rate) * 100,
            "experiments_run": len(self.results) - 1,  # exclude baseline
            "kept": kept,
            "discarded": discarded,
            "results_tsv": str(self.results_tsv),
            "changelog_md": str(self.changelog_md),
        }
        return summary

    def cancel(self) -> None:
        """Request cancellation; loop will exit at next safe point."""
        self.cancelled = True

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _summarise_failures(self, result: ExperimentResult) -> str:
        """Build a short failure summary for the mutation proposer."""
        total_runs = self.cfg.runs_per_experiment * len(self.cfg.test_inputs)
        lines = [
            f"Current pass rate: {result.pass_rate*100:.1f}% ({result.pass_count}/{result.max_score})",
            "Per-eval breakdown:",
        ]
        for ev in self.cfg.evals:
            passed = result.per_eval_pass.get(ev.id, 0)
            fail_rate_pct = (1 - passed / total_runs) * 100 if total_runs else 0
            lines.append(f"  - [{ev.id}] {ev.name}: {passed}/{total_runs} pass ({fail_rate_pct:.0f}% fail)")
        return "\n".join(lines)


__all__ = [
    "AutoresearchConfig",
    "AutoresearchRunner",
    "EvalDef",
    "ExperimentResult",
]
