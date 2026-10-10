# Fault-injection experiment

google/gemini-2.5-flash, 2026-10-10 00:04:53 -0500, git e5fbedc522d8efa4cea51f6b1872ccfcc933de3e (dirty: False). Fallback claude-haiku-4-5; retries {'max_retries': 3, 'base_s': 1.0, 'cap_s': 30.0}; faults ['timeout', 'rate_limit', 'server', 'malformed_json'], drawn per LLM call. One pass per cell, so the spread shown is a 95% Wilson interval on the success rate, not a spread across seeds. A run succeeds when it is graded correct; unlike the eval, runs ended by an API error count as failures. Latency is the run's wall time (p95: nearest rank). Retries and fallbacks are per run.

| rate | reliability | runs | correct | 95% CI | api errors | cost $ | $/run | p50 s | p95 s | retries/run | fallbacks/run | injected/run | answered by fallback |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 100% | on | 10 | 90% | 60%–98% | 0 | 0.2361 | 0.02361 | 36.0 | 43.6 | 13.50 | 4.50 | 18.00 | 45 of 45 |

LLM calls answered, by model: {'claude-haiku-4-5': 45}. 'Fallbacks/run' counts calls sent to the fallback, answered or not; 'answered by fallback' counts the replies it gave.

Versions: {"qdk": "1.32.3", "qiskit": "2.5.2", "bicycle_compiler": "0.2.1", "bicycle_compiler_commit": "bed7e8aba4cc0fb86311b7fa6dad6217c0fce7cb", "rsgridsynth": "0.2.2", "cargo_lock_sha256": "605a42f1b3414fe57ef0b3620b8bef1553480759228a94bd69f7799bb53e0101"}
Hardware: {"platform": "Windows-10-10.0.26200-SP0", "processor": "Intel64 Family 6 Model 191 Stepping 2, GenuineIntel", "python": "3.11.9"}
