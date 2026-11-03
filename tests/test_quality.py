"""Real-LLM quality test. Skipped unless RUN_LLM_TESTS=1.

    RUN_LLM_TESTS=1 pytest tests/test_quality.py -s
"""
import json
import os
import re
import time
from pathlib import Path

import pytest

from app import clinics
from app.services import llm, safety

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to call the real LLM"
)

CASES = json.loads((Path(__file__).parent / "test_leads.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def env():
    """Override the autouse fixture from conftest: keep real LLM_* env vars for this file."""
    yield


def test_quality_report():
    clinic = clinics.load_clinic("demo_clinic")
    rows, correct, total, violations = [], 0, 0, 0

    for case in CASES:
        if case.get("skip_quality"):
            continue
        lead = {"name": "Test Lead", "message": case["message"], "service": case.get("service")}
        start = time.perf_counter()
        result = llm.qualify(lead, clinic)
        latency = time.perf_counter() - start

        score_ok = result.score in case["expected_scores"]
        problems = []
        if case["expected_urgent"] is not None and result.urgent != case["expected_urgent"]:
            problems.append("urgent flag")
        if case["expect_needs_human"] is not None and result.needs_human != case["expect_needs_human"]:
            problems.append("needs_human flag")
        if result.score != "spam":
            ok, reasons = safety.check_reply(result.reply, clinic)
            if not ok:
                problems.append("safety: " + "; ".join(reasons))
        for pattern in case["forbidden_patterns"]:
            if re.search(pattern, result.reply, re.I):
                problems.append(f"forbidden: {pattern}")

        total += 1
        correct += score_ok
        violations += bool(problems)
        rows.append((case["id"], result.score, "/".join(case["expected_scores"]),
                     "OK" if score_ok else "MISS", f"{latency:.1f}s", "; ".join(problems) or "-"))

    print(f"\n{'#':>2} {'got':6} {'expected':14} {'score':5} {'time':6} problems")
    for r in rows:
        print(f"{r[0]:>2} {r[1]:6} {r[2]:14} {r[3]:5} {r[4]:6} {r[5]}")
    accuracy = correct / total
    print(f"\nScore accuracy: {accuracy:.0%} | cases with violations: {violations}")

    assert violations == 0, "rule violations found (see table)"
    assert accuracy >= 0.85, f"score accuracy {accuracy:.0%} below 85% target"
