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


PRICE = {"input": 1.0, "cache_read": 0.1, "cache_write": 1.25, "output": 2.0}
PAST = [{"steps": 4, "input_tokens": 8000, "cached_tokens": 1000, "output_tokens": 500}] * 2


def test_expected_cost_of_the_profile_model_uses_its_own_tokens():
    # 7000 fresh * 1.0 + 1000 cached * 0.1 + 500 out * 2.0, per 1M
    assert ev.expected_cost(PRICE, PAST) == pytest.approx(8100 / 1e6)


def test_expected_cost_scales_input_by_the_tokenizer_ratio_and_caches_the_prefix():
    # ratio 2: 16000 input; no cache: 16000 * 1.0 + 500 * 2.0
    assert ev.expected_cost(PRICE, PAST, ratio=2.0) == pytest.approx(17000 / 1e6)
    # prefix 3000 read on each of 4 steps: 12000 cached * 0.1 + 4000 fresh * 1.0 + 1000
    assert ev.expected_cost(PRICE, PAST, ratio=2.0, prefix=3000) == pytest.approx(6200 / 1e6)
    # the cache can't cover more than the whole prompt
    assert ev.expected_cost(PRICE, PAST, ratio=2.0, prefix=9000) == pytest.approx(2600 / 1e6)


STOP = {"cost_factor": 2.0, "api_error_rate": 0.1, "min_runs": 10}


def rec(cost=0.01, category=None):
    return {"cost_usd": cost, "category": category}


def test_stop_when_cost_passes_twice_the_expected_total():
    assert ev.stop_reason([rec()] * 10, 0.06, STOP) is None  # $0.10 of 2 x $0.06 = $0.12
    assert "2x the expected" in ev.stop_reason([rec()] * 13, 0.06, STOP)


def test_stop_when_more_than_a_tenth_of_runs_are_api_errors():
    cheap = 0.0001
    assert ev.stop_reason([rec(cheap, "api error")] * 3, 1.0, STOP) is None  # too few runs yet
    ten = [rec(cheap, "api error")] + [rec(cheap)] * 9
    assert ev.stop_reason(ten, 1.0, STOP) is None  # exactly 10%
    assert "api errors" in ev.stop_reason(
        [rec(cheap, "api error")] * 2 + [rec(cheap)] * 8, 1.0, STOP
    )


def test_api_errors_are_counted_on_the_first_attempt_and_after_the_rerun():
    first = [rec(0.0, "api error") | {"task": "a", "repeat": 0}, rec() | {"task": "b", "repeat": 0}]
    first += [rec(0.0, "api error") | {"task": "c", "repeat": 0}]
    assert ev.api_error_counts(first) == (2, 2)
    rerun_a = rec() | {"task": "a", "repeat": 0, "rerun_of": {"run_id": "x"}}
    rerun_c = rec(0.0, "api error") | {"task": "c", "repeat": 0, "rerun_of": {"run_id": "y"}}
    merged = ev.replace_reruns(first, [rerun_a, rerun_c])
    assert [r["task"] for r in merged] == ["a", "b", "c"]  # same order
    assert merged[0] is rerun_a and merged[2] is rerun_c and merged[1] is first[1]
    assert ev.api_error_counts(merged) == (2, 1)


def test_summary_reports_the_api_error_baseline():
    meta = {"model": "m", "date": "d", "config": {"seed": 0}, "tasks": "evals/tasks.yaml",
            "tasks_sha256": "0" * 64, "versions": {"qdk": "1"},
            "hardware": {"processor": "cpu", "platform": "os", "python": "3.11"}}  # fmt: skip
    rs = records([[1, 1, 1, 1]])
    err = {
        "category": "api error",
        "correct": False,
        "failures": [{"category": "api error", "reason": "502"}],
    }
    rs[0] = rs[0] | {"rerun_of": {"run_id": "x"}}  # rerun succeeded
    rs[1] = rs[1] | err
    text = ev.summarize(rs, meta)
    assert "API errors (no retries): 2 of 4 runs on the first attempt, 1 after rerunning" in text


def test_record_leaves_the_pacer_wait_out_of_the_latencies():
    from types import SimpleNamespace

    run = SimpleNamespace(
        run_id="x", stop_reason="answered", steps=3, verify_passed_first_try=True,
        verify_passed_after_retry=False, totals={"llm_s": 12.0, "tool_s": 1.0, "cost_usd": 0.1},
    )  # fmt: skip
    task = {"id": "t", "type": "standard", "difficulty": "easy"}
    grading = {"correct": True, "category": None, "failures": []}
    r = ev.record(
        task, SimpleNamespace(outputs=[]), run, grading, "m", 0, 0, wall_s=20.0, paced=8.0
    )
    assert (r["latency_s"], r["llm_s"], r["paced_wait_s"]) == (12.0, 4.0, 8.0)
    r = ev.record(task, SimpleNamespace(outputs=[]), run, grading, "m", 0, 0, wall_s=20.0)
    assert (r["latency_s"], r["llm_s"], r["paced_wait_s"]) == (20.0, 12.0, 0.0)
