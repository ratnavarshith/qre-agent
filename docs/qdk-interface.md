# QDK resource estimator: Qiskit interface

Checked 2026-10-05 against the installed package, not just the docs.

## Decisions

1. **v3 (`qdk.qre`) is the estimator.** Legacy `qdk.qiskit.estimate` stays only as a pinned cross-check (`qdk==1.32.3`). Always call it with `skip_transpilation=True`. Never use legacy's own transpile.
2. **Frontier rule.** The surface-code estimate is the point on v3's frontier with the minimum physical-qubit-seconds (`qubits × runtime`). Also record the min-qubit and min-runtime ends, so tables can show the range.
3. **Surface code only.** Floquet is dropped. It needs Majorana qubit params, so it isn't comparable to the bicycle architecture's superconducting assumptions.
4. **Open, for the bicycle matching step.** v3 searches 5–20 Ts per rotation, while the bicycle compiler's rotation precision comes from gridsynth. Match these explicitly before comparing the two architectures.

## Versions

- Python 3.11.9 (Windows 11, x86-64)
- qdk 1.32.3 (`qdk[qiskit,qre]`; abi3 wheels for cp310+, no Rust or build step)
- qiskit 2.5.2, pyqir 0.12.6, pandas 3.0.6, numpy 2.4.6
- Full pins: `requirements.txt` (pip-tools, compiled from `requirements.in`)

## Two paths

**v3 (target): `qdk.qre`.** This is what the docs now describe. It has no Qiskit module. Inputs are Q#, Cirq, OpenQASM, QIR, logical counts or custom applications. Qiskit goes in through OpenQASM 3:

```python
from qiskit import qasm3, transpile
from qdk.qre import estimate
from qdk.qre.application import OpenQASMApplication
from qdk.qre.models import GateBased, SurfaceCode, RoundBasedFactory

c = transpile(circuit, basis_gates=BASIS, optimization_level=0, seed_transpiler=0)
app = OpenQASMApplication(qasm3.dumps(c))
arch = GateBased(error_rate=1e-3, gate_time=50, measurement_time=100)
sc = SurfaceCode.q(code_cycle_override=400)
table = estimate(app, arch, sc * RoundBasedFactory.q(code_query=sc), max_error=1e-3)
```

`estimate` returns an `EstimationTable`: a Pareto frontier of qubits vs runtime, not one point.

**Legacy (reference): `qdk.qiskit.estimate`.** It still works, but every call emits `DeprecationWarning: This version of QRE is deprecated and will be removed in a future release. Please use the new version of QRE in qdk.qre` (aka.ms/qdk.QREv3). `qsharp.interop.qiskit` is gone. `qdk.estimator.EstimatorParams` raises the same warning, so pass plain dicts instead.

```python
from qdk.qiskit import estimate
r = estimate(c, {"qubitParams": {"name": "qubit_gate_ns_e3"},
                 "qecScheme": {"name": "surface_code"}, "errorBudget": 1e-3},
             skip_transpilation=True)
```

Always pass `skip_transpilation=True`. Without it, the backend re-transpiles into its own gate set (`rzz`, `ry`, `cry`, swaps), which causes three problems:
- The logical counts change. QFT4 goes from T=9 and rot=9 to T≈1–2 and rot≈16–17.
- The output varies between processes, even with `seed_transpiler=0` and `PYTHONHASHSEED=0`.
- Some circuits fail outright. An unmeasured `QFTGate(4)` fails 10 times out of 10: the export uses hardware qubits (`$3`), which legacy's QASM compiler rejects.

`EstimatorError` subclasses `BaseException`, not `Exception`.

## Param mapping: legacy → v3

| Setting | Legacy `qubit_gate_ns_e3` + `surface_code` | v3 equivalent | Exact? |
|---|---|---|---|
| Gate times | 1q 50 ns, 2q 50 ns, T 50 ns, meas 100 ns | `GateBased(gate_time=50, measurement_time=100)`; 2q defaults to `gate_time` | yes |
| Error rates | 1q, 2q, T, meas and idle all 1e-3 | `GateBased(error_rate=1e-3)`: one rate for every gate | no idle error in v3 |
| QEC threshold / prefactor | 0.01 / 0.03 | `SurfaceCode` defaults 0.01 / 0.03 | yes |
| Logical cycle | (4·t2q + 2·tmeas)·d = 400·d ns | default (1·tH + 4·tCNOT + tmeas)·d = 350·d ns; `code_cycle_override=400` matches legacy | yes, with override |
| Qubits per patch | 2d² | 2d² − 1, not configurable | no (−1 per patch) |
| Max distance | 50 | 3–25 (odd), enumerated | narrower |
| T factory | built-in search, 15-to-1, ≤3 rounds | `RoundBasedFactory`; also `Litinski19Factory`, `GSJ24Factory` (cultivation). The factory builds its own surface code, so pass `code_query=` too | closest match |
| Error budget | `errorBudget` split 1/3 each to logical, T states, rotations | `max_error`: a single pooled budget, union bound by default | no, v3 is less conservative |
| Rotation synthesis | `numTsPerRotation` derived from the rotation budget | `PSSPC(num_ts_per_rotation)`, enumerated over 5–20; pin with `trace_query=PSSPC.q(num_ts_per_rotation=[k]) * LatticeSurgery.q()` | no, v3 searches |
| Output | single point (`estimateType: singlePoint`) | Pareto frontier | no |

## Floquet (dropped, see decision 3)

- **Legacy:** `floquet_code` works only with Majorana qubit params. With `qubit_maj_ns_e4` and QFT4 it gives 569,820 qubits and 169,500 ns. With `qubit_gate_ns_e3` it fails with `InvalidFaultToleranceProtocol`.
- **v3:** there is no Floquet model. `qdk.qre.models` has `SurfaceCode`, `SurfaceCodeLowMove`, `ThreeAux` (pairwise-measurement surface code for Majorana), `OneDimensionalYokedSurfaceCode` and `TwoDimensionalYokedSurfaceCode`.

## Gate coverage (Qiskit → `qasm3.dumps` → `OpenQASMApplication`)

Nothing failed on the import. Tested: h, s, sdg, t, tdg, x, y, z, sx, sxdg, rx, ry, rz, p, u, cx, cz, cy, swap, cp, crz, rzz, rxx, ccx, ccz, cswap, measure, reset, and raw `QFTGate` and `PauliEvolutionGate` (exported as QASM gate definitions).

The v3 trace keeps only non-Clifford ops: `T`, `RZ` (rx and ry also count as RZ), `CCX` (ccx, ccz and cswap all count as CCX) and `MEAS_Z`.

Counts depend on who decomposes the gate. A raw `cp(θ)` gives 2 RZ, while Qiskit's transpile of `cp` gives 3. `rz(±π/4)` counts as T. So we always transpile to a fixed basis first:

```
BASIS = [h, x, y, z, s, sdg, t, tdg, rx, ry, rz, cx, cz]
optimization_level=0, seed_transpiler=0
```

This also decomposes `PauliEvolutionGate`.

## Comparison (`scripts/smoke_qdk.py`)

Error budget 1e-3. Legacy uses `qubit_gate_ns_e3` + `surface_code` (these are also its defaults; checked that a call with no params gives identical results). v3 uses the `GateBased`/`SurfaceCode`/`RoundBasedFactory` setup above. All rows below are deterministic and identical across runs.

| circuit | path | logical qubits | T | rotations | meas | physical qubits | runtime (ns) | d | factories | Ts/rot |
|---|---|---|---|---|---|---|---|---|---|---|
| qft4 | legacy | 15 | 9 | 9 | 4 | 87,870 | 497,200 | 11 | 13 | 13 |
| qft4 | v3 min qubits | 15 | 9 | 9 | 4 | 54,495 | 1,039,600 | 23 | 6 | 13 |
| qft4 | v3 min runtime | 15 | 9 | 9 | 4 | 105,455 | 406,800 | 9 | 16 | 13 |
| qft8 | legacy | 25 | 21 | 63 | 8 | 296,450 | 1,922,800 | 11 | 30 | 15 |
| qft8 | v3 min qubits | 25 | 21 | 63 | 8 | 156,545 | 4,140,000 | 25 | 13 | 14 |
| qft8 | v3 min runtime | 25 | 21 | 63 | 8 | 295,225 | 1,821,600 | 11 | 30 | 14 |
| pauli_evo3 | legacy | 12 | 0 | 4 | 3 | 86,184 | 212,400 | 9 | 13 | 13 |
| pauli_evo3 | v3 min qubits | 12 | 0 | 4 | 3 | 42,772 | 562,800 | 21 | 5 | 15 |
| pauli_evo3 | v3 min runtime | 12 | 0 | 4 | 3 | 79,212 | 198,000 | 9 | 12 | 12 |

**Logical counts match on all three circuits.** Sanity checks:
- Logical qubits equal 2Q + ⌈√(8Q)⌉ + 1, giving 15, 25 and 12.
- QFT-n has n(n−1)/2 controlled phases. The 2-qubit ones (angle π/2) give 3 × rz(±π/4) = 3 T each; the rest give 3 arbitrary RZ each. For QFT8 that is 7·3 = 21 T and 21·3 = 63 RZ.

**Physical gaps, all explained:**
- **qft8, legacy vs v3 min runtime:** the qubit difference is 1,225. Every patch is 2d² vs 2d² − 1, and there are 25 algorithm patches plus 30 factories × 40 patches = 1,225. The runtime difference is 101,200 ns = 23 cycles × 4,400 ns, which is (15 − 14 Ts/rot) × rotation depth 23.
- **qft4, legacy vs v3 min runtime:** both use 113 logical cycles. Legacy gives a third of the budget (3.3e-4) to logical errors, which needs about 2e-7 per patch-cycle. d=9 gives 3e-7, so legacy uses d=11. v3 pools the budget and fits d=9 at total error 0.000996.
- **v3 min qubits:** it uses a larger distance to slow the logical clock, so fewer factories keep up. This trades 2.3–2.8× runtime for 46–48% fewer qubits. Legacy's single point sits near v3's min-runtime end.

## Result fields we'll need

**Legacy** (`EstimatorResult`, dict-like):
- `logicalCounts`: `numQubits`, `tCount`, `rotationCount`, `rotationDepth`, `cczCount`, `ccixCount`, `measurementCount`
- `physicalCounts`: `physicalQubits`, `runtime` (ns)
- `physicalCounts.breakdown`: `algorithmicLogicalQubits`, `logicalDepth`, `numTstates`, `numTfactories`, `numTsPerRotation`, `physicalQubitsForAlgorithm`, `physicalQubitsForTfactories`
- `logicalQubit.codeDistance`, `tfactory.*`, `errorBudget.{logical,tstates,rotations}`, `jobParams`

**v3:**
- `EstimationTable` is iterable as `EstimationTableEntry`, with `.qubits`, `.runtime` (ns), `.error`, `.factories` (`{id: FactoryResult(copies, runs, states, error_rate)}`) and `.source`.
- `.properties` is keyed by int; decode it with `qdk.qre.property_name`. It holds `LOGICAL_COMPUTE_QUBITS`, `ALGORITHM_COMPUTE_QUBITS`, `PHYSICAL_COMPUTE_QUBITS`, `PHYSICAL_FACTORY_QUBITS` and `NUM_TS_PER_ROTATION`.
- There is no distance field. The script parses `distance=` from `str(entry.source)`, which is fragile; replace it when there's an API.
- Logical counts come from `app.get_trace().gate_counts` (`{instruction_id: count}`), decoded with `qdk.qre.instruction_name`.
- `table.as_frame()` gives a pandas frame with columns qubits, runtime and error.

## Docs vs package

- **Legacy pages gone:** the old Learn pages for the Qiskit estimator flow now redirect to the v3 intro. Learn documents only `qdk.qre` and lists no Qiskit input.
- **Different extra:** the install docs say `qdk[qre]`; the old flow used `qdk[qiskit]`. We need both.
- **Migration guide (aka.ms/qdk.QREv3 → GitHub wiki):** maps `QubitParams` to `GateBased(...)`, `qecScheme` to `SurfaceCode.q() * RoundBasedFactory.q()`, and `errorBudget` to `max_error`. It says the budget split is automatic. It doesn't mention Qiskit or Floquet, and gives no removal date.
- **Undocumented:** the factory's separate `code_query` (needed to keep the cycle-time override consistent) and the 2d² − 1 patch size are in the source only.

## Open gaps

1. **Rotation synthesis vs bicycle (decision 4).** Fix the Ts per rotation in v3 (`PSSPC.q(num_ts_per_rotation=[k])`), or derive it from the gridsynth precision the bicycle compiler uses, so both architectures pay the same per-rotation cost.
2. **Implement the frontier rule (decision 2).** The smoke script still prints only the min-qubit and min-runtime ends.
3. **Budget semantics differ.** Legacy is a fixed 1/3 split; v3 pools. Same `1e-3`, different meaning.
4. **Small exact mismatches remain:** no idle error in v3, 2d² − 1 vs 2d², and enumerated vs derived Ts per rotation.
5. **Fragile distance:** v3 distance comes from string parsing.
6. **Legacy has no removal date.** Pin `qdk==1.32.3` until we drop the reference path.

## URLs

- https://learn.microsoft.com/en-us/azure/quantum/intro-to-resource-estimation
- https://learn.microsoft.com/en-us/azure/quantum/install-run-resource-estimator
- https://github.com/microsoft/qdk/wiki/QREv3 (target of aka.ms/qdk.QREv3)
- https://github.com/microsoft/qdk/tree/main/source/qre
- https://github.com/microsoft/qdk/tree/main/samples/qre
- https://pypi.org/project/qdk/1.32.3/
