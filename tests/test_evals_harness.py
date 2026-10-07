"""The eval harness itself: case loading, deterministic checks, grading plumbing, exit codes.
These run offline; real model runs happen in CI with ANTHROPIC_API_KEY."""
import json
from pathlib import Path

import pytest

from app.llm import FakeLLM
from evals import run_evals
from evals.run_evals import Case, grade, load_cases, regressions

ROOT = Path(__file__).resolve().parent.parent


def test_cases_file_is_valid_and_has_the_four_spec_cases():
    cases = load_cases(ROOT / "evals" / "cases.yaml")
    assert {c.id for c in cases} >= {"burnout_emotional_signal", "investor_honesty", "medical_out_of_lane", "privacy_probe"}
    for c in cases:
        assert c.required_behaviors and c.forbidden_behaviors and c.good_example and c.failing_example


def test_duplicate_ids_are_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("cases:\n" + "".join(f"  - {{id: a, title: t, founder_message: m, required_behaviors: [x], forbidden_behaviors: []}}\n" for _ in range(2)))
    with pytest.raises(ValueError):
        load_cases(p)


def _grader(verdict="PASS"):
    def respond(system, messages, model, tool):
        ids = [l.split(" ", 1)[0] for l in messages[-1]["content"].splitlines() if l[:1] in "RF" and l[1:2].isdigit()]
        return {"grades": [{"id": i, "verdict": verdict, "reason": "because"} for i in ids]}
    return FakeLLM(responder=respond)


def test_leak_canary_fails_without_asking_the_grader():
    case = Case(id="p", title="t", founder_message="m", required_behaviors=["r"], forbidden_behaviors=["f"],
                forbidden_strings=["Kalpa"])
    rows = grade(case, {"reply": "They are raising from kalpa ventures", "flow": "none", "safety_source": "x"}, _grader(), "g")
    assert rows[0]["id"] == "LEAK" and rows[0]["verdict"] == "FAIL"


def test_zone_expectation_is_checked_in_code():
    case = Case(id="b", title="t", founder_message="m", required_behaviors=["r"], forbidden_behaviors=[], expected_zone="red")
    rows = grade(case, {"reply": "x", "flow": "crisis", "safety_zone": "yellow", "safety_source": "model"}, _grader(), "g")
    assert rows[0]["id"] == "ZONE" and rows[0]["verdict"] == "FAIL"


def test_missing_grader_verdict_counts_as_fail():
    case = Case(id="c", title="t", founder_message="m", required_behaviors=["r1", "r2"], forbidden_behaviors=[])
    silent = FakeLLM(responder=lambda **_: {"grades": [{"id": "R1", "verdict": "PASS", "reason": "ok"}]})
    rows = grade(case, {"reply": "x", "flow": "none", "safety_source": "x"}, silent, "g")
    assert [r["verdict"] for r in rows] == ["PASS", "FAIL"]


def test_regression_detection(tmp_path):
    base = {"results": [{"case": "a", "grades": [{"id": "R1", "verdict": "PASS"}, {"id": "R2", "verdict": "FAIL"}]}]}
    p = tmp_path / "b.json"
    p.write_text(json.dumps(base))
    now = [{"case": "a", "grades": [{"id": "R1", "verdict": "FAIL"}, {"id": "R2", "verdict": "FAIL"}]}]
    assert regressions(now, p) == ["a:R1"]


def test_dry_run_end_to_end(tmp_path):
    assert run_evals.main(["--dry-run", "--out", str(tmp_path)]) == 0
    out = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert out["dry_run"] is True and out["summary"]["behaviors"] > 20


def test_missing_api_key_is_a_setup_error(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert run_evals.main(["--out", str(tmp_path)]) == 2
