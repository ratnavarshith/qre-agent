| QFT | p | arch | physical qubits | runtime (ms) | qubit-seconds | error | pass | qubit-s at 50 / 66.7 / 100 ns | crossover timestep (ns) |
|---|---|---|---|---|---|---|---|---|---|
| 4 | 1e-03 | surface | 140,015 | 1.93 | 271 | 4.64e-04 | yes | n/a | n/a |
| 4 | 1e-03 | gross | 861 | 16.4 | 14.1 | 9.25e-01 | FAILS budget | n/a | n/a |
| 4 | 1e-03 | two-gross | 1,226 | 75.3 | 92.3 | 3.43e-04 | yes | 69.2 / 92.3 / 138 | 195.7 |
| 4 | 1e-04 | surface | 17,395 | 0.744 | 12.9 | 5.99e-04 | yes | n/a | n/a |
| 4 | 1e-04 | gross | 1,201 | 8.95 | 10.8 | 7.37e-04 | yes | 8.06 / 10.8 / 16.1 | 80.3 |
| 4 | 1e-04 | two-gross | 19,383 | 21.4 | 415 | 3.33e-04 | yes | 311 / 415 / 622 | 2.1 |
| 8 | 1e-03 | surface | 466,745 | 7.42 | 3.46e+03 | 6.47e-04 | yes | n/a | n/a |
| 8 | 1e-03 | gross | 861 | 123 | 106 | 7.29e+00 | FAILS budget | n/a | n/a |
| 8 | 1e-03 | two-gross | 1,226 | 589 | 722 | 4.09e-04 | yes | 541 / 722 / 1.08e+03 | 319.8 |
| 8 | 1e-04 | surface | 68,385 | 3.99 | 273 | 3.52e-04 | yes | n/a | n/a |
| 8 | 1e-04 | gross | 1,201 | 65.1 | 78.2 | 3.51e-03 | FAILS budget | n/a | n/a |
| 8 | 1e-04 | two-gross | 19,383 | 164 | 3.18e+03 | 3.33e-04 | yes | 2.38e+03 / 3.18e+03 / 4.77e+03 | 5.7 |
| 16 | 1e-03 | surface | 1,592,765 | 29.6 | 4.72e+04 | 7.71e-04 | yes | n/a | n/a |
| 16 | 1e-03 | gross | 1,261 | 649 | 819 | 4.09e+01 | FAILS budget | n/a | n/a |
| 16 | 1e-03 | two-gross | 1,994 | 3.21e+03 | 6.4e+03 | 7.54e-04 | yes | 4.8e+03 / 6.4e+03 / 9.59e+03 | 491.6 |
| 16 | 1e-04 | surface | 136,285 | 10.9 | 1.49e+03 | 4.30e-04 | yes | n/a | n/a |
| 16 | 1e-04 | gross | 1,601 | 326 | 523 | 1.80e-02 | FAILS budget | n/a | n/a |
| 16 | 1e-04 | two-gross | 20,151 | 860 | 1.73e+04 | 3.33e-04 | yes | 1.3e+04 / 1.73e+04 / 2.6e+04 | 5.7 |

**Note on the p=1e-4 rows.** two-gross at 1e-4 is dominated by the paper's distillation factory (18,600 of its 19,383-20,151 qubits; output error 6e-25, which the paper calls very conservative). The paper uses cultivation only at 1e-3 because no cultivation estimates exist at 1e-4 (Tour de gross Sec. 2.5, Table 3). Surface uses v3's own factory search. So the 1e-4 rows compare factory choices as much as architectures.

**Sensitivity, not the main result.** The same compiled circuits re-scored with paper Table 2 error rates where bicycle_numerics' gross p=1e-4 model differs 10x: T injection 9.0e-8 (code 8.79e-7) and shift automorphism 6.3e-13 (code 6.07e-14). Timings and qubit counts are unchanged, so crossovers are too.

| QFT | p | arch | physical qubits | runtime (ms) | qubit-seconds | error | pass | qubit-s at 50 / 66.7 / 100 ns | crossover timestep (ns) |
|---|---|---|---|---|---|---|---|---|---|
| 4 | 1e-04 | gross (paper errors) | 1,201 | 8.95 | 10.8 | 3.75e-04 | yes | 8.06 / 10.8 / 16.1 | 80.3 |
| 8 | 1e-04 | gross (paper errors) | 1,201 | 65.1 | 78.2 | 6.60e-04 | yes | 58.6 / 78.2 / 117 | 233.0 |
| 16 | 1e-04 | gross (paper errors) | 1,601 | 326 | 523 | 2.15e-03 | FAILS budget | n/a | n/a |
