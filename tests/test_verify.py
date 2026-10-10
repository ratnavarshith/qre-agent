import json
from decimal import Decimal

import pytest

from qre_agent.verify import (
    NS,
    fields_match,
    grows_with_size,
    numbers_match,
    parse_answer,
    physical_bounds,
    same_circuit,
    verify,
)


def result(rid, arch, qubits, runtime_ns, logical, circuit_id="c1", num_qubits=4, t=9, rotations=9):
    return {
        "result_id": rid,
        "architecture": arch,
        "circuit": {"circuit_id": circuit_id, "num_qubits": num_qubits},
        "t_count": t,
        "rotation_count": rotations,
        "physical_qubits": qubits,
        "runtime_ns": runtime_ns,
        "physical_qubit_seconds": qubits * runtime_ns * NS,
        "logical_qubits": logical,
    }


# QFT on 4 qubits at p = 1e-3 (results/comparison): surface and two-gross.
SURFACE = result("r1", "surface", 140_015, 1_934_400.0, 15)
BICYCLE = result("r2", "bicycle", 1_226, 75_294_213.0, 12)


def answer(summary, *estimates):
    """Final-answer JSON; estimate values are JSON literals, so the precision is as written."""
    rows = [
        f'{{"result_id": "{r}", "architecture": "{a}", "physical_qubits": {q}, '
        f'"runtime_ns": {t}, "physical_qubit_seconds": {qs}}}'
        for r, a, q, t, qs in estimates
    ]
    return f'{{"summary": {json.dumps(summary)}, "estimates": [{", ".join(rows)}]}}'


S_EST = ("r1", "surface", "140015", "1.93e6", "271")
B_EST = ("r2", "bicycle", "1226", "7.53e7", "92.3")
GOOD = answer("Surface needs 140,015 physical qubits, two-gross 1,226.", S_EST, B_EST)
RESULTS = {"r1": SURFACE, "r2": BICYCLE}


def test_same_circuit_passes():
    assert same_circuit(SURFACE, BICYCLE) is None


@pytest.mark.parametrize(
    "change, key",
    [
        ({"t": 8}, "t_count"),
        ({"rotations": 10}, "rotation_count"),
        ({"circuit_id": "c2"}, "circuit"),
    ],
)
def test_same_circuit_fails_on_any_mismatch(change, key):
    other = result("r2", "bicycle", 1_226, 75_294_213.0, 12, **change)
    assert key in same_circuit(SURFACE, other)


def test_same_circuit_ignores_the_layouts_logical_qubits():
    assert SURFACE["logical_qubits"] != BICYCLE["logical_qubits"]
    assert same_circuit(SURFACE, BICYCLE) is None


def test_physical_bounds_pass():
    assert physical_bounds(SURFACE) is None
    assert physical_bounds(BICYCLE) is None


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"runtime_ns": -1.0}, "not positive"),
        ({"physical_qubit_seconds": 1.0}, "!= qubits"),
        ({"physical_qubits": 10, "physical_qubit_seconds": 10 * 1_934_400.0 * NS}, "logical"),
        ({"physical_qubits": 2 * 10**9, "physical_qubit_seconds": 2 * 1_934_400.0}, "sanity"),
    ],
)
def test_physical_bounds_fail(change, reason):
    bad = {**SURFACE, **change}
    if "runtime_ns" in change:
        bad["physical_qubit_seconds"] = bad["physical_qubits"] * bad["runtime_ns"] * NS
    assert reason in physical_bounds(bad)


def test_numbers_match_at_the_precision_written():
    answer = (
        "Surface needs 140,015 physical qubits for 271 qubit-seconds (2.71e2, 2.708 × 10^2); "
        "two-gross needs 1,226 qubits and 92.3 qubit-seconds at p = 1e-3 and p = 10^-3."
    )
    assert numbers_match(answer, [SURFACE, BICYCLE, {"p": 0.001}]) is None


@pytest.mark.parametrize("made_up", ["5000 qubits", "270 qubit-seconds", "3x fewer"])
def test_numbers_match_fails_on_a_number_no_tool_produced(made_up):
    reason = numbers_match(f"Surface needs 140,015 qubits; {made_up}.", [SURFACE, BICYCLE])
    assert made_up.split()[0].rstrip("x") in reason


# SURFACE runs 1,934,400 ns and BICYCLE 75,294,213 ns
@pytest.mark.parametrize(
    "written",
    ["1.9344 ms", "1.9344ms", "1.93 ms", "1,934.4 µs", "1,934.4 μs", "1934.4 us", "0.0019344 s",
     "75.294213 ms", "0.075294213 s", "1.9344 ms."],
)  # fmt: skip
def test_numbers_match_accepts_a_runtime_converted_to_the_unit_written(written):
    assert numbers_match(f"Surface runs {written}", [SURFACE, BICYCLE]) is None


@pytest.mark.parametrize(
    "written",
    ["1.9344 s", "1.9344 µs", "1.94 ms", "1.9344", "1.9344 msec", "1.9344 steps", "1.9344, ms",
     "1.9344 and 2 ms", "1.9344 seconds", "19.344 ms", "1,934,400 ms", "1934400 s", "1934400 µs"],
)  # fmt: skip
def test_numbers_match_rejects_a_missing_or_wrong_unit(written):
    assert numbers_match(f"Surface runs {written}", [SURFACE, BICYCLE])


def test_the_unit_conversion_stays_in_the_prose_layer():
    text = answer(
        "Surface needs 140,015 qubits.", ("r1", "surface", "140015", "1.9344", "271"), B_EST
    )
    result = verify(SURFACE, BICYCLE, text)
    assert [f["check"] for f in result["failures"]] == ["fields_match"]
    assert "runtime_ns reported 1.9344" in result["failures"][0]["reason"]
    assert verify(
        SURFACE, BICYCLE, answer("Surface runs 1.9344 ms, two-gross 75.294213 ms.", S_EST, B_EST)
    )["passed"]


def test_numbers_match_ignores_digits_inside_ids():
    assert numbers_match("Results r1 and r2 for circuit c1.", [SURFACE]) is None


def estimates(text):
    return parse_answer(text)[0]["estimates"]


def test_fields_match_passes_at_the_precision_written():
    assert fields_match(estimates(GOOD), RESULTS, ["r1", "r2"]) is None


def test_fields_match_fails_when_surface_and_bicycle_numbers_are_swapped():
    swapped = answer("", ("r1", "surface", *B_EST[2:]), ("r2", "bicycle", *S_EST[2:]))
    assert "r1: physical_qubits reported 1226" in fields_match(estimates(swapped), RESULTS, [])


def test_fields_match_fails_when_the_architecture_labels_are_swapped():
    swapped = answer("", ("r1", "bicycle", *S_EST[2:]), ("r2", "surface", *B_EST[2:]))
    assert "r1 is surface, reported as 'bicycle'" in fields_match(estimates(swapped), RESULTS, [])


@pytest.mark.parametrize(
    "estimate, reason",
    [
        (("r1", "surface", "140015", "1.93e6", "270"), "physical_qubit_seconds reported 270"),
        (("r1", "surface", "140000", "1.93e6", "271"), "physical_qubits reported 140000"),
        (("r9", "surface", "140015", "1.93e6", "271"), "unknown result 'r9'"),
        (("r1", "surface", "140015", "1.93e6", '"271"'), "not a number"),
    ],
)
def test_fields_match_fails_on_a_wrong_or_unciteable_value(estimate, reason):
    assert reason in fields_match(estimates(answer("", estimate, B_EST)), RESULTS, [])


def test_fields_match_fails_when_a_result_is_not_reported():
    assert "['r2']" in fields_match(estimates(answer("", S_EST)), RESULTS, ["r1", "r2"])


@pytest.mark.parametrize(
    "text", ["Surface: 140,015 qubits.", '{"summary": "x"}', '[{"summary": "x", "estimates": []}]']
)
def test_answer_must_be_structured(text):
    checks = [f["check"] for f in verify(SURFACE, BICYCLE, text)["failures"]]
    assert checks == ["answer_format"]


# Adder at p = 1e-3 on surface (results/comparison): raw qubits dip from n=4 to n=8, QS grows.
ADDER = [
    (4, result("s4", "surface", 82_110, 237_600.0, 30), result("b4", "bicycle", 1_226, 24.3e6, 12)),
    (8, result("s8", "surface", 76_209, 572_100.0, 30), result("b8", "bicycle", 1_994, 40.1e6, 24)),
    (16, result("s16", "surface", 80_502, 1.34e6, 60), result("b16", "bicycle", 3_530, 68.4e6, 36)),
]
ADDER_4 = answer(
    "", ("s4", "surface", "82110", "237600", "19.5"), ("b4", "bicycle", "1226", "2.43e7", "29.8")
)


def sweep(points):
    return [{"size": n, "surface": s, "bicycle": b} for n, s, b in points]


def test_grows_with_size_passes_when_raw_qubits_dip():
    assert ADDER[1][1]["physical_qubits"] < ADDER[0][1]["physical_qubits"]
    assert grows_with_size(sweep(reversed(ADDER))) is None  # order of entries does not matter


def test_grows_with_size_fails_when_qubit_seconds_drop():
    shrinking = [ADDER[0], (8, result("s8", "surface", 82_110, 200_000.0, 30), ADDER[1][2])]
    assert "surface" in grows_with_size(sweep(shrinking))


def test_grows_with_size_fails_on_repeated_sizes():
    assert "repeated" in grows_with_size(sweep([ADDER[0], (4, *ADDER[1][1:])]))


def test_verify_passes_and_reports_each_failed_check():
    assert verify(SURFACE, BICYCLE, GOOD) == {"passed": True, "failures": []}
    bad = verify(SURFACE, {**BICYCLE, "t_count": 8}, answer("About 5000 qubits.", S_EST))
    assert not bad["passed"]
    checks = [f["check"] for f in bad["failures"]]
    assert checks == ["same_circuit", "fields_match", "numbers_match"]


def test_prose_is_checked_even_when_the_fields_are_right():
    text = answer("Two-gross saves about 5000 qubits.", S_EST, B_EST)
    assert [f["check"] for f in verify(SURFACE, BICYCLE, text)["failures"]] == ["numbers_match"]


def test_verify_checks_every_sweep_point():
    points = sweep(ADDER)
    points[2]["bicycle"] = {**points[2]["bicycle"], "physical_qubit_seconds": -1.0}
    checks = [f["check"] for f in verify(*ADDER[0][1:], ADDER_4, sweep=points)["failures"]]
    assert checks == ["physical_bounds", "grows_with_size"]


def test_numbers_in_the_task_prompt_count_as_known():
    text = answer("An 8-bit adder: surface 140,015 physical qubits, two-gross 1,226.", S_EST, B_EST)
    failed = verify(SURFACE, BICYCLE, text)
    assert failed["failures"] == [
        {"check": "numbers_match", "reason": "numbers not in any tool output: ['8']"}
    ]
    prompt = "How many physical qubits does an 8-bit ripple-carry adder need on each architecture?"
    assert verify(SURFACE, BICYCLE, text, prompt=prompt)["passed"]


# Grader v2. SURFACE runs 1,934,400 ns and BICYCLE 75,294,213 ns.
@pytest.mark.parametrize(
    "written",
    ["0.0019344 seconds", "0.0019344 second", "1.9344 milliseconds", "1,934.4 microseconds",
     "1,934,400 nanoseconds", "1,934,400 ns", "0.075294213 seconds"],
)  # fmt: skip
def test_numbers_match_accepts_time_units_written_out(written):
    assert numbers_match(f"Surface runs {written}", [SURFACE, BICYCLE]) is None


@pytest.mark.parametrize("written", ["1.9344 seconds", "1934400 seconds", "1.9344 nanoseconds"])
def test_numbers_match_still_rejects_a_wrong_value_in_a_written_out_unit(written):
    assert numbers_match(f"Surface runs {written}", [SURFACE, BICYCLE])


def test_float_noise_beyond_double_precision_matches():
    # Gemini copied 0.0006471858333333334 as 0.00064718583333333343: the digits past what a
    # double holds are noise, not a different number.
    outputs = [{"error": 0.0006471858333333334, "qs": 98645.80684800001}]
    assert numbers_match("error 0.00064718583333333343, 98645.806848000007 qs", outputs) is None
    assert numbers_match("error 0.00064718593333333343", outputs)  # differs at the 9th digit


def test_fields_match_tolerates_float_noise():
    estimate = {"result_id": "r1", "architecture": "surface", "physical_qubits": Decimal(140015),
                "runtime_ns": Decimal(1934400),
                "physical_qubit_seconds": Decimal("270.84501600000004199")}  # fmt: skip
    assert fields_match([estimate], {"r1": SURFACE}, ["r1"]) is None
    estimate["physical_qubit_seconds"] = Decimal("270.8450161")
    assert fields_match([estimate], {"r1": SURFACE}, ["r1"])
