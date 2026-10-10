# Fault-injection experiment

google/gemini-2.5-flash, 2026-10-10 00:11:30 -0500, git e5fbedc522d8efa4cea51f6b1872ccfcc933de3e (dirty: False). Fallback claude-haiku-4-5; retries {'max_retries': 3, 'base_s': 1.0, 'cap_s': 30.0}; faults ['timeout', 'rate_limit', 'server', 'malformed_json'], drawn per LLM call. One pass per cell, so the spread shown is a 95% Wilson interval on the success rate, not a spread across seeds. A run succeeds when it is graded correct; unlike the eval, runs ended by an API error count as failures. Latency is the run's wall time (p95: nearest rank). Retries and fallbacks are per run.

| rate | reliability | runs | correct | 95% CI | api errors | cost $ | $/run | p50 s | p95 s | retries/run | fallbacks/run | injected/run | answered by fallback |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0% | off | 40 | 75% | 60%–86% | 1 | 0.1629 | 0.00407 | 13.4 | 19.8 | 0.00 | 0.00 | 0.00 | 0 of 191 |
| 0% | on | 40 | 78% | 62%–88% | 0 | 0.2062 | 0.00516 | 13.9 | 25.6 | 0.00 | 0.00 | 0.00 | 0 of 210 |
| 10% | off | 40 | 55% | 40%–69% | 14 | 0.0998 | 0.00250 | 12.1 | 16.7 | 0.00 | 0.00 | 0.42 | 0 of 154 |
| 10% | on | 40 | 78% | 62%–88% | 0 | 0.1344 | 0.00336 | 12.5 | 23.1 | 0.55 | 0.03 | 0.42 | 1 of 193 |
| 20% | off | 40 | 40% | 26%–55% | 20 | 0.0732 | 0.00183 | 11.8 | 18.3 | 0.00 | 0.00 | 0.70 | 0 of 140 |
| 20% | on | 40 | 75% | 60%–86% | 1 | 0.1200 | 0.00300 | 13.7 | 20.0 | 0.95 | 0.03 | 0.88 | 1 of 188 |
| 30% | off | 40 | 20% | 10%–35% | 29 | 0.0738 | 0.00184 | 9.2 | 17.4 | 0.00 | 0.00 | 0.93 | 0 of 112 |
| 30% | on | 40 | 75% | 60%–86% | 1 | 0.1406 | 0.00351 | 14.2 | 33.5 | 2.00 | 0.07 | 1.88 | 2 of 192 |

LLM calls answered, by model: {'google/gemini-2.5-flash': 1376, 'claude-haiku-4-5': 4}. 'Fallbacks/run' counts calls sent to the fallback, answered or not; 'answered by fallback' counts the replies it gave.

## Findings

- **One reliability-on API error was a bug, fixed after the run.** `ff-grover-5` at 20% (reliability on) ended on an unclassified `http.client.IncompleteRead`: the client caught only `URLError` and timeouts, so the reliability layer never saw it. Connection errors are retried since commit `d6b759a`. The experiment was not rerun, so the 20%-on cell shows 1 API error (75%) that the fixed code would probably have retried; the cells are not corrected.
- **The other reliability-on API error was injected faults alone.** `std-grover8-1e4-gross` at 30%: 4 injected faults on the primary, then 4 on the fallback, which is injected at the same rate. Roughly a 1-in-120 event across that cell.
- **OpenRouter really did return replies with no usage.** 13 retries in the reliability-on cells (12 on `ff-draper-adder-4`, 1 on `std-adder8-1e3-twogross`), and 1 more on `ff-draper-adder-4` in the 0% reliability-off cell, where nothing retried it and the run ended as an API error. Not injected. On that task the retries ran out twice (10% and 20%) and the fallback finished the run. Failed attempts are billed in the spend log at their worst case, 17 of them for $0.22.
- **No evidence either way on whether a mid-run fallback costs quality.** 5 runs sent a call to the fallback: 1 correct (`std-tfim64-1e3-twogross`), 3 graded wrong circuit, 1 API error (the double failure above). The 3 wrong ones were on `ff-draper-adder-4` (twice) and `ff-grover-4`, which are wrong in 8 of 8 and 7 of 8 cells of this experiment, with no faults at all, and `ff-draper-adder-4` was wrong in 3 of 3 gemini-2.5-flash eval runs. They would probably have failed anyway; with 5 runs this can't separate the two.
- **Off-cell latency and cost are not comparable with the on cells.** A run without the layer stops at its first injected error, so it is shorter and cheaper. Cost differences between cells are also within the 27% difference between the two 0% cells, which have the same setup.

Versions: {"qdk": "1.32.3", "qiskit": "2.5.2", "bicycle_compiler": "0.2.1", "bicycle_compiler_commit": "bed7e8aba4cc0fb86311b7fa6dad6217c0fce7cb", "rsgridsynth": "0.2.2", "cargo_lock_sha256": "605a42f1b3414fe57ef0b3620b8bef1553480759228a94bd69f7799bb53e0101"}
Hardware: {"platform": "Windows-10-10.0.26200-SP0", "processor": "Intel64 Family 6 Model 191 Stepping 2, GenuineIntel", "python": "3.11.9"}
