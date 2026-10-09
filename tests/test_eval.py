import pytest

from qre_agent import eval as ev
from qre_agent.agent import Run

SUITE = ev.load_suite()
TASKS = {t["id"]: t for t in SUITE["tasks"]}


def test_suite_shape():
    assert len(TASKS) == len(SUITE["tasks"]) == 40
    by_type = [t["type"] for t in SUITE["tasks"]]
    assert [by_type.count(t) for t in ev.TYPES] == [25, 10, 5]
    for t in SUITE["tasks"]:
        assert t["difficulty"] in ev.DIFFICULTIES
        assert (t["difficulty"] == "ambiguous") == (t["type"] == "ambiguous")
        assert t["params"].get("p", 1e-3) in ev.P_STRINGS
        assert t["params"].get("code", "gross") in ev.CODE_WRITTEN


def test_expected_numbers_come_from_results_json():
    fails = 0
    for t in SUITE["tasks"]:
        if t["type"] == "free-form":
            assert ev.reference_path(SUITE, t["id"]).exists()
            assert ev.reference_path(SUITE, t["id"], wrong=True).exists()
            continue
        for choice in ("reference", "alternative"):
            settings = t["params"] | t.get(choice, {})
            rows = ev.expected_rows(SUITE, **settings)
            assert rows, (t["id"], choice)
        if t["type"] == "standard":
            fails += not ev.expected_rows(SUITE, **t["params"])["bicycle"]["passes"]
    assert fails >= 3  # some tasks must make the agent say the bicycle estimate fails


@pytest.mark.parametrize(
    "summary, p",
    [
        ("assuming a physical error rate of 0.001 and a total logical error budget of 0.001", 1e-3),
        ("These estimates assume a physical error rate of 1e-3.", 1e-3),
        ("at p = 1e-4 on both", 1e-4),
        ("with p=10^-4", 1e-4),
        ("at a 0.1% physical error rate", 1e-3),
        ("a 1.0e-04 physical error rate", 1e-4),
        ("a physical error rate (p) of 1e-3", 1e-3),
    ],
)
def test_stated_p(summary, p):
    assert ev.stated_p(summary) == {p}


def test_error_budget_is_not_a_stated_p():
    assert ev.stated_p("assuming an error budget of 0.001 and the two-gross code") == set()
    assert ev.stated_p("p = 1e-4 and an error budget of 0.001") == {1e-4}


def test_states_code_and_size():
    assert ev.states("code", "two-gross", "on the two-gross code")
    assert not ev.states("code", "gross", "on the two-gross code")
    assert ev.states("code", "gross", "IBM's gross code and the two-gross code")
    assert ev.states("n", 8, "an 8-qubit QFT")
    assert ev.states("n", 4, "with 4 counting qubits")
    assert not ev.states("n", 4, "a 14-qubit circuit")


@pytest.mark.parametrize(
    "summary",
    [
        "The gross estimate exceeds the error budget.",
        "its error of 0.925 is above the budget",
        "the bicycle result fails the 1e-3 budget",
        "it does not meet the error budget (passes is false)",
        "the error, 0.925, is larger than the error budget",
    ],
)
def test_budget_fail_statements(summary):
    assert ev.FAILS.search(summary)


def test_no_budget_fail_statement():
    assert not ev.FAILS.search("Surface needs 140,015 physical qubits, two-gross 1,226.")


def no_answer(stop_reason, steps=12):
    return Run("x", stop_reason, None, None, steps, {}, None)


@pytest.mark.parametrize(
    "stop_reason, last, category",
    [
        ("step_limit", {"content": "", "tool_calls": [{"id": "1"}]}, "budget/step limit"),
        ("step_limit", {"content": "I can't build this circuit."}, "gave up"),
        ("step_limit", {"content": '{"summary": "x", "estimates": ['}, "unit/format"),
        ("error: BudgetError: phase eval: worst case ...", None, "budget/step limit"),
        ("error: RuntimeError: OpenRouter HTTP 502: ...", None, "api error"),
    ],
)
def test_runs_without_an_answer(stop_reason, last, category):
    g = ev.grade(TASKS["std-qft4-1e3-twogross"], ev.Toolbox(), no_answer(stop_reason), SUITE, last)
    assert not g["correct"] and g["category"] == category


def test_unit_slip():
    results = {
        "r1": {"physical_qubits": 10, "runtime_ns": 2_000_000.0, "physical_qubit_seconds": 0.02}
    }
    estimate = {"result_id": "r1", "physical_qubits": 10, "physical_qubit_seconds": 0.02}
    assert ev._unit_slip({"estimates": [estimate | {"runtime_ns": 2.0}]}, results)  # ms
    assert not ev._unit_slip({"estimates": [estimate | {"runtime_ns": 2.5e6}]}, results)


def records(correct_by_repeat):
    return [
        {"task": f"t{i}", "type": "standard", "difficulty": "easy", "repeat": r, "seed": r,
         "correct": c, "category": None if c else "made-up numbers",
         "failures": [] if c else [{"category": "made-up numbers", "reason": "ratio"}],
         "verify_first_try": c, "verify_after_retry": False, "steps": 5, "tool_errors": 0,
         "cost_usd": 0.004, "reported_cost_usd": 0.004, "input_tokens": 1000, "cached_tokens": 0,
         "output_tokens": 100, "latency_s": 10.0, "llm_s": 8.0, "tool_s": 2.0}
        for r, run in enumerate(correct_by_repeat)
        for i, c in enumerate(run)
    ]  # fmt: skip


def test_summary_reports_spread_across_repeats():
    meta = {"model": "m", "date": "d", "config": {"seed": 0}, "tasks": "evals/tasks.yaml",
            "tasks_sha256": "0" * 64, "versions": {"qdk": "1"},
            "hardware": {"processor": "cpu", "platform": "os", "python": "3.11"}}  # fmt: skip
    text = ev.summarize(records([[1, 1], [1, 0], [0, 0]]), meta)
    assert "| all | 2 | 50% ± 50 (0–100) |" in text
    assert "| made-up numbers | 3 | 0 | 0 | 3 |" in text
    assert "| t1 | standard | easy | 1/3 |" in text


@pytest.mark.compiler
@pytest.mark.parametrize("task_id", list(TASKS))
def test_grader_self_test(task_id):
    """Reference answers pass and corrupted ones fail, with the right category."""
    rows = ev.self_test(TASKS[task_id], SUITE)
    assert all(r["ok"] for r in rows), [r for r in rows if not r["ok"]]
