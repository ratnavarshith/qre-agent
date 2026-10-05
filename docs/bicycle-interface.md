# Bicycle compiler: Qiskit interface and inputs for estimate_bicycle

Checked 2026-10-05 against the local checkout and by running it, not just the docs. Investigation only; no wrapper yet.

## Versions

- bicycle-architecture-compiler `bed7e8a` (workspace v0.2.1), release binaries built with `-F bicycle_compiler/rsgridsynth` (rsgridsynth 0.2.2, seed fixed at 1 in `small_angle.rs:144`). No external `gridsynth` on PATH. HEAD's only change since the binaries were built is doc comments.
- rustc/cargo 1.99.0; Python 3.11.9, qiskit 2.5.2, qdk 1.32.3 (our `.venv`); Windows 11, x86-64.
- Paper: Tour de gross, arXiv:2506.03094**v1** (the only version). Section/table numbers below refer to v1.

## 1. Qiskit path

Pipeline (`scripts/qiskit_demo.sh`):

```
qiskit_demo.py N  |  bicycle_compiler CODE --measurement-table T  |  bicycle_numerics N MODEL
```

1. **Qiskit → PBC** (`qiskit_demo.py:54-63`): `transpile(c, basis_gates=["rz","t","tdg"] + get_clifford_gate_names())` (default optimization level), then `LitinskiTransformation(fix_clifford=False)`. Litinski moves Cliffords to the end and drops them, turns every non-Clifford `rz/t/tdg` into a single-term `PauliEvolutionGate`, turns `measure` into `PauliProductMeasurement`, and absorbs rotations at multiples of π/2 as Cliffords.
2. **PBC → JSON lines** (`qiskit_parser.py:30-96`): one JSON object per line:
   `{"Rotation":{"basis":[...],"angle":"<str>"}}` or `{"Measurement":{"basis":[...],"flip_result":bool}}`. Each basis is a full-width list over `I/X/Y/Z`, one entry per circuit qubit.
3. **Compiler** (`bicycle_compiler/src/main.rs`): streams JSON lines from stdin. It sets the number of modules from the **first** op's basis length (`ceil(n/11)`, `architecture.rs:25`). It parses `angle` into the fixed-point type `I32F96` from a string (`language.rs:361,374`). Exponent strings like `"1e-05"` parse fine. Output is one JSON line per input op, a list of `(module, ISA instruction)`.
4. **Numerics** (`bicycle_numerics`): reads compiler output and writes CSV (see §3). The `N` passed here must match the circuit width, because it rebuilds the architecture from it (`main.rs:207`).

**What the parser accepts:** only `PauliEvolution` with exactly one Pauli term, and `pauli_product_measurement`. Anything else raises `ValueError("Unsupported instruction in PBC circuit: …")`. Multi-term evolutions raise `"PauliEvolution is not a single rotation."` Nothing is silently dropped.

**What Litinski accepts:** Clifford gates from `get_clifford_gate_names()`, `rz/rx/ry/p/t/tdg`, Pauli product rotations/measurements, and `measure`. It **rejects `barrier` and `reset`** with a `TranspilerError`. `measure_all()` inserts a barrier, so our circuits need `RemoveBarriers()` first.

**Other things observed:**
- The demo's `transpile` uses the default optimization level and no seed. We'd pin `optimization_level=0, seed_transpiler=0` as on the surface side.
- With `use_ppr=True`, Litinski emits `pauli_product_rotation` (`exp(-iθ/2·P)`), which the parser rejects.
- The compiler ignores `flip_result` (`language.rs:392`, "currently not used").
- Measurement signs are handled correctly, Y included. I checked ±Y, ±Z and `-YX` against the expected conjugations; the parser maps `phase==2` to `flip_result: true`.
- **Do not run `qiskit_demo.sh` / `generate_measurement_tables.sh` as-is.** They call `cargo build --release` *without* `-F rsgridsynth`, which overwrites our binaries with ones that need external gridsynth, and they write `data/` inside the compiler repo. Call the binaries by path instead, and keep the measurement tables outside that repo. `bicycle_compiler <code> generate <path>` takes ≈63 s per code.

## 2. Parser bugs and how we correct them

### Bug A: angle (known, `qiskit_parser.py:75`)

- `PauliEvolutionGate(c·P, t)` = `exp(−i·t·c·P)`. I checked it numerically: `t=0.15` on Z equals `RZ(0.3)`.
- The compiler implements `exp(iφ/2·P)` (`bicycle_compiler/README.md`, `small_angle.rs:41`) and treats `|φ| = π/4` as one T (`small_angle.rs:39`; `TGate` = `exp(iπ/8 P)`, `bicycle_common/src/lib.rs`).
- So the correct value is **φ = −2·t·c**. The parser emits `t·c`: half the magnitude and the wrong sign.

**Impact, measured:**
- A single `t` gate becomes a PBC rotation with `t=π/8`. Raw output is `angle "0.39269908169872414"`, which compiles to **91 T injections**. The corrected `−π/4` compiles to **1**.
- QFT4 on gross: raw 1,647 T injections, corrected 828 (= 9·1 + 9·91). Corrected counts match the surface side's 9 T + 9 rotations.
- The sign is irrelevant to cost (T and T† cost the same), but we should get it right anyway.

### Bug B: qubit indices (new, `qiskit_parser.py:71-73`)

The parser writes `basis[i] = pauli` using the operator-local index from `to_sparse_list()`. It never maps through `inst.qubits` (the measurement branch does, at line 88). Litinski emits each rotation only on its support qubits, so supports land on the wrong qubits:

| circuit | raw parser basis | correct |
|---|---|---|
| `t` on q2 of 3 | `["Z","I","I"]` | `["I","I","Z"]` |
| `cx(0,2); rz(0.3, 2)` | `["Z","Z","I"]` | `["Z","I","Z"]` |
| `PauliEvolutionGate("XY")` on `[1,2]` | `["Y","X","I"]` | `["I","Y","X"]` |

Weight is preserved but positions are wrong. That changes Clifford-synthesis cost (QFT4: 204 vs 210 in-module measurements; demo n=4 on gross: 631 with the raw parser, 469 with only the index fixed). Once n > 11 it also changes which modules a rotation touches. The upstream demo's own numbers are affected.

### Other gates

- Measurements carry no angle, and their qubit mapping and sign are correct (see §1).
- Clifford-angle rotations never reach the parser: Litinski absorbs them.
- A float `±π/4` string doesn't hit the exact `T_ANGLE` equality (`small_angle.rs:52`). It goes through gridsynth, which returns a single T. That's fragile but correct today, and should be covered by the same test.
- No other angle problems found.

### Decision: write our own converter (recommended)

Both bugs are in the 15-line `PauliEvolution` branch. A wrapper would have to re-derive the qubit mapping anyway, so wrapping saves nothing. A converter of about 25 lines (prototype tested in scratch) would:
- map local index `k` → `inst.qubits[k]` → circuit index
- set `angle = −2·t·Re(c)`
- use the public `PauliProductMeasurement.pauli()` instead of the private `_to_pauli_data()`
- reject anything else

Optionally it could also accept `pauli_product_rotation` (φ = −θ). With it, the demo output keeps the same structure (52 ops); only the angles and supports change.

### Failing-test ideas (write before the converter)

1. `t` on q0 of 1 → Litinski → converter gives `angle == -π/4` (raw parser gives `+π/8`, so the test fails against it). An end-to-end variant, marked slow and needing the binary: the compiler emits exactly one `TGate` (raw: 91).
2. `t` on q2 of 3 → basis `["I","I","Z"]` (raw `["Z","I","I"]`).
3. `PauliEvolutionGate(SparsePauliOp("XY", coeffs=[-0.5]), time=0.2)` on `[1,2]` of 3 → basis `["I","Y","X"]`, angle `+0.2` (raw: `["Y","X","I"]`, `-0.1`).
4. Measurement of −Y (`s; h; measure`) → `["Y"]`, `flip_result: true`. This is a regression guard; it passes on raw too.

### Shared circuit preparation (`surface.prepare`)

Both estimators start from the same prepared circuit: a transpile to the fixed basis, then three angle rules. Each rule exists because one side would otherwise silently see a different circuit:

- **Angles are reduced mod 2π.** That changes only the global phase. QPE's controlled powers reach 2π/3·2³¹, past the compiler's `I32F96` range (±2³¹), where it panics with `parse error: overflow`.
- **Rotations with |angle| < 1e-9 are dropped, and charged to the error.** `qasm3.dumps` writes them as `0` (`pi_check` eps 1e-9) and v3 then drops them, while the bicycle path never exports. Each dropped rotation adds |θ|/2 (operator-norm distance from the identity, after the mod-2π reduction) to the total; both estimators return the sum as `dropped_error` (also in the bicycle `error_breakdown`) and v3 runs with it subtracted from `max_error`. It is 0 for every circuit in `results/comparison`. QFT32 has three of these.
- **Rotations within 2.45e-6 of a multiple of π/2 are rejected with an error.** `LitinskiTransformation` treats them as Clifford and drops them, even at `approximation_degree=1.0`. I measured the threshold at 2.4495e-6 on qiskit 2.5.2. v3 counts them, so on QFT32 the bicycle side saw 231 fewer rotations. Dropping them would be an approximate-QFT choice whose error isn't accounted for, so we fail loudly instead. This caps QFT and QPE at n = 20, whose smallest rotation is π/2²⁰ ≈ 3.0e-6.

## 3. Compiler and numerics output

**Compiler stdout:** one line per input PBC op, e.g. `[[0,{"Measure":…}],[0,{"Automorphism":{"x":3,"y":2}}],…,[[0,{"JointMeasure":…}],[1,{"JointMeasure":…}]]]`. Each element is a list of `(module, instruction)`, and paired entries are inter-module ops. The instructions emitted are `Measure`, `JointMeasure`, `Automorphism` and `TGate`. Each rotation compiles to:
- pre-rotations
- pivot preparation
- native measurement
- GHZ across modules
- the synthesized T sequence on the last module, next to the factory
- uncompute
- post-rotations

`optimize.rs` removes trivial automorphisms and duplicate measurements. Output was deterministic across 3 runs, byte for byte.

**Numerics CSV** (`bicycle_numerics/src/main.rs:131-144`, `lib.rs:459-525`): one row per input PBC op.

| column | meaning | units / scope |
|---|---|---|
| `code`, `p` | model | p is fixed by the model, one of 1e-3 / 1e-4 |
| `i` | 1-based op index | |
| `qubits` | `11 × modules` | **logical** data qubits, not physical |
| `idles` | idle blocks inserted while modules wait | count **for this row**; one idle = 8 timesteps (`model.rs:309-312`) |
| `t_injs` | T injections | count for this row |
| `automorphisms` | shift-automorphism **generators** (`nr_generators()`) | count for this row |
| `measurements` / `joint_measurements` | in-module / inter-module measurements | count for this row |
| `measurement_depth` | max over modules of measurement depth | **cumulative** |
| `end_time` | max over modules of elapsed time | **cumulative, in timesteps** |
| `total_error` | Σ instruction errors + idle errors | **cumulative, additive (union bound, paper Eq. 26), can exceed 1** |

- **Units caveat:** the numerics README says `end_time` is "in syndrome cycles", but the code sums Table 2 durations, which are in **timesteps**. One syndrome cycle is 8 timesteps (paper §2.2).
- **Totals:** sum the per-row columns and take the last row for the cumulative ones.
- **`total_error` excludes rotation-synthesis error.** We must add that ourselves.

**Example runs**, corrected converter, accuracy 1e-9, our circuits from `tests/circuits.py` (all ≤11 qubits, so 1 module, no joint measurements, no idles):

| circuit | model | T inj | meas | joint | aut gens | idles | depth | end_time (timesteps) | total_error |
|---|---|---|---|---|---|---|---|---|---|
| qft4 | gross_1e-3 | 828 | 210 | 0 | 312 | 0 | 210 | 419,124 | 1.67 |
| qft4 | two-gross_1e-3 | 828 | 149 | 0 | 200 | 0 | 149 | 2,008,188 | 1.74e-05 |
| qft8 | gross_1e-3 | 5,823 | 1,073 | 0 | 1,370 | 0 | 1,073 | 2,890,017 | 11.7 |
| qft8 | two-gross_1e-3 | 5,823 | 877 | 0 | 954 | 0 | 877 | 14,081,049 | 1.22e-04 |
| pauli_evo3 | gross_1e-3 | 372 | 73 | 0 | 100 | 0 | 73 | 185,268 | 0.749 |
| pauli_evo3 | two-gross_1e-3 | 372 | 38 | 0 | 54 | 0 | 38 | 895,404 | 7.81e-06 |

Sanity checks:
- qft4 gross: 828 × 2.01e-3 = 1.66, which accounts for the error; 828 × 471 = 390k, which accounts for most of `end_time`.
- **gross at p=1e-3 cannot run any of these within a 1e-3 budget:** each T injection carries 2e-3 error (paper Table 2: 10^−5.5 + P_C, where P_C = 10^−2.7).
- The upstream demo (n=4, two-gross_1e-4, raw parser) gives 4,428 T injections and end_time 2,832,492.

## 4. Physical conversion: what's stated where

| quantity | value | source |
|---|---|---|
| logical qubits per module | 12 (qubit 1 is the pivot) | paper §1.2 ("each store 12 logical qubits") |
| data qubits per module used by compiler | 11 (+1 pivot) | `bicycle_compiler/README.md` "Multi-block circuits"; `architecture.rs:26`; paper §4 ("n = 11M") |
| code qubits `c` | gross 288, two-gross 576 | paper Table 1 |
| LPU `u` | 90 / 158 | paper Table 1 |
| code-code adapter `a` | 22 / 34 (= 2(d−1)) | paper Table 1, §2.4 |
| factory `f` | gross: 454 (p=1e-3), 810 (p=1e-4); two-gross: 463 (1e-3), 18,600 (1e-4) | paper Table 1, Table 3 |
| code-factory adapter `a′` | gross 29 / 13, two-gross 29 / 49 (= 2·d_factory − 1) | paper Table 1, Table 3 |
| total physical qubits | **q = M(c+u+a) − a + a′ + f**, for 1D chain of M modules + one factory | paper §4, Eq. 25 |
| timestep | "a timestep is a physical gate"; all physical ops take one timestep | paper Table 2 caption |
| syndrome cycle | 8 timesteps (8·C + 1 for C cycles) | paper §2.2; `model.rs` `idle: 8` |
| instruction durations | idle 8; shift 14; in-module and inter-module meas. 120 (gross), 216 (two-gross); T inj = τ_factory + τ_C (351+120, 2167+216, 73+120, 407+216) | paper Table 2 (§2.5 gives τ_T = τ_factory + τ_C + 1) |
| surface-code cycle in timesteps (paper's convention) | 6 | paper §2.5 ("6 timesteps per code cycle for the surface code") |
| **timestep or cycle length in ns/µs** | **not stated** anywhere in the paper or the repo | grepped the paper and all crates/scripts/notebooks |
| bicycle physical qubit count in tool output | **not produced**. Numerics `qubits` is logical; we must apply Eq. 25 | `bicycle_numerics/src/lib.rs:465` |

Example of Eq. 25: one gross module at p=1e-3 gives q = 400 − 22 + 29 + 454 = 861.

**Code vs paper mismatches in `bicycle_numerics/src/model.rs`** (the code is what runs):
- Shift timing: `shift: 12`, and every `Automorphism` instruction costs `2·shift` = 24 timesteps in both time and error, even when it is one generator. The paper says 14 per shift.
- gross_1e-4 T-inj timing: `109 + 120` vs paper `73 + 120`. This was changed in `5c1bd77` "Fix timing. Address #1" (2025-10-10); I can't see issue #1 from here.
- gross_1e-4 shift error `6.07e-14` vs paper 10^−12.2 ≈ 6.3e-13 (10× lower in code).
- gross_1e-4 T-inj error `8.79e-7` vs paper 10^−7.4 + P_C ≈ 9e-8 (10× higher in code).
- These last two have been there since the repo's first import (`01f7a43`) with no explanation. The other models match Table 2 within rounding.

## 5. Rotation precision

- **Compiler:** `--accuracy ε` (default `1e-9`, `main.rs:208-210`) is the operator-norm error per rotation, ‖e^{iθZ/2} − U‖ ≤ ε (`small_angle.rs:43`). It must be ≤ 0.1 (assert). The same ε applies to every rotation.
- **Paper:** for its TFIM example it sets ε = 5×10⁻⁹ ≈ 10⁻³/N_R, with N_R = 184,000 rotations (§A.10). That is, ε = budget / number of rotations.
- **Measured T per rotation** (rsgridsynth, 30 random angles in (−π, π), seed 0):

| ε | 1e-1 | 1e-2 | 1e-3 | 1e-4 | 1e-5 | 1e-6 | 1e-9 | 1e-12 |
|---|---|---|---|---|---|---|---|---|
| median T | 10 | 22 | 31 | 42 | 53 | 62 | 92.5 | 123 |
| min–max | 0–14 | 0–24 | 28–34 | 39–44 | 43–55 | 59–66 | 86–96 | 108–125 |

The median grows by about 10 T per decade of ε, as expected for Ross–Selinger.

- **These match the Ross–Selinger fit.** arXiv:2203.10064 Table 1, row "Diagonal [RS15]", gives a mean T count of 3.02·log2(1/ε) + 1.77. That predicts 41.9 / 62.0 / 92.0 T at ε = 1e-4 / 1e-6 / 1e-9; we measured means of 41.8 / 62.3 / 92.0.

### v3's Ts-per-rotation model (corrects the first version of this section)

Source: `microsoft/qdk` `source/qre/src/trace/transforms/psspc.rs` at tag v1.32.0. The file was last changed in 2fc02b15f (2026-04-23), before that tag, so it matches our qdk 1.32.3.

- `PSSPC` counts `num_ts_per_rotation × rotations` T states and adds `rotations × synthesis_error()` to the trace's base error (`psspc.rs:147`), where

  ```
  synthesis_error(k) = 2^((4.86 − k) / 0.53)      (psspc.rs:203-205)
  ⇔ k = 0.53·log2(1/ε) + 4.86
  ```

- The code cites arXiv:2203.10064 Table 1 ("Clifford+T in the mixed fallback approximation protocol"). That row is the **mean** linear fit (fit for ε < 1e-4; the max fit is 0.57·log2(1/ε) + 8.83), with **ε measured in diamond distance**. "Fallback" is the repeat-until-success-with-fallback method of [BRS15a]; "mixed" adds probabilistic mixing. So it is RUS-style, not gridsynth: about 0.5·log2(1/ε) T per rotation vs gridsynth's about 3.0·log2(1/ε).
- v3 enumerates k ∈ 5..20 and keeps the k that fits its pooled `max_error`. **What v3 actually targets:**

  | circuit | rotations | k chosen | ε per rotation (diamond) | rotations·ε | share of entry error |
  |---|---|---|---|---|---|
  | qft4 | 9 | 13 (14 on one frontier point) | 2.38e-5 (6.44e-6) | 2.1e-4 | 0.22–0.44 |
  | qft8 | 63 | 14 on every frontier point | **6.44e-6** | 4.06e-4 | 0.43–0.64 |

  Sanity check: k=13 on QFT8 would give 63 × 2.38e-5 = 1.5e-3, which is over the budget, so k ≥ 14 is forced.
- **Correction:** I previously wrote that "13–15 Ts ≈ gridsynth at ε 0.1–0.01". That matched T counts, not precision, and is withdrawn. At v3's own ε of 6.44e-6, gridsynth needs 3.02·17.25 + 1.77 ≈ 54 T per rotation vs v3's 14: **≈ 3.9× from the synthesis method alone**, at the same precision.
- **Error metrics differ.** Gridsynth's ε is operator norm, while the mixed-fallback ε is diamond distance. 2203.10064 says the diamond distance between unitaries is bounded by twice the minimum spectral-norm distance between ±U and V. So a gridsynth ε in operator norm is at most 2ε in diamond distance.
- **Pinning v3 works:** `estimate(..., trace_query=PSSPC.q(num_ts_per_rotation=[k]) * LatticeSurgery.q())`, with `PSSPC` and `LatticeSurgery` imported from `qdk.qre` (not `qdk.qre.models`). On QFT4 every frontier entry reported exactly k for k = 13, 20, 21, 42 and 92. Values outside the declared 5–20 domain are accepted.
- Oddity to recheck later: min-qubit QFT4 was 54,495 at k=13 and 21 but 57,375 at k=20 and 42. That isn't monotone in k (probably discrete factory/distance effects).

## 6. Assumption matching (`assumptions/default.yaml`)

| assumption | surface side | bicycle equivalent | match? |
|---|---|---|---|
| `qec: surface_code` | | needs `code: gross \| two-gross` | new field |
| `physical_error_rate: 1e-3` | any float | only 1e-3 or 1e-4, baked into the model (`gross_1e-3`, …) | partial: discrete only |
| `gate_time_ns`, `two_qubit_gate_time_ns`, `measurement_time_ns` (50/50/100) | per-op times | one uniform "timestep" for all ops, length not stated | **mismatch**: needs a ns-per-timestep choice |
| `code_cycle_ns: 400` | surface cycle | syndrome cycle = 8 timesteps; paper counts the surface cycle as 6 timesteps | **mismatch**: see decision 3 |
| `error_budget: 1e-3` | v3 pooled `max_error` (union bound) | none as input. `total_error` is an additive output; `-e` only stops streaming. Synthesis error is not included | **mismatch**: we check `total_error + N_R·ε ≤ budget` ourselves |
| rotation precision | v3 searches Ts/rot (5–20) | `--accuracy ε` per rotation (gridsynth) | **mismatch**: see §5 |
| `transpile.basis_gates` | `[h,x,y,z,s,sdg,t,tdg,rx,ry,rz,cx,cz]` | `rz,t,tdg` + Clifford names, then Litinski | different basis; logical counts agree on all 3 test circuits (qft4 18 = 9 T + 9 rot, qft8 84 = 21 + 63, pauli_evo3 4) |
| `optimization_level: 0`, `seed: 0` | | same settings usable | yes (demo uses defaults; we'd pin) |
| T factory | v3 `RoundBasedFactory` search | exactly one fixed factory per (code, p) (Table 3), adjacent to the last module | different model |
| layout / routing | v3 lattice surgery | 1D chain of modules, one factory | different model |
| output | Pareto frontier | single point | different |

Note: the paper's own surface-code comparison (§A.10) uses 2d² qubits per patch and P₁ = 0.03(p/0.01)^{d/2}. That is the same threshold/prefactor as our v3 `SurfaceCode` defaults.

## Decisions (2026-10-05)

0. **v3 synthesis model, verified:** v3 uses mixed fallback, k = 0.53·log2(1/ε) + 4.86 with ε in diamond distance (§5). On QFT8 it targets ε = 6.44e-6 per rotation (k = 14).
1. **Own converter.** Write our own Qiskit PBC → JSON converter: angle = −2·t·Re(c) for `PauliEvolutionGate` and −θ for `pauli_product_rotation`; qubits mapped through `inst.qubits`; measurements via the public `PauliProductMeasurement.pauli()`; reject everything else. Tests:
   - (a) cases where upstream's parser is correct produce identical output: measurements (±Z, ±Y, multi-qubit), and rotations on the leading qubits with angles compared after the −2 factor
   - (b) the angle bug: `t` → −π/4, not +π/8
   - (c) the index bug: `t` on q2 of 3 → `["I","I","Z"]`
2. **Error rates.** Run both architectures at p = 1e-3 and 1e-4, the only rates bicycle supports. At 1e-3, report gross as **"fails budget"** (a finding: each T injection alone is 2e-3) and use two-gross for the comparison.
3. **Timestep.** Add an assumption field `timestep_ns`, default **66.7** (= 400 ns / 6). The basis is the paper's §2.5, Table 3 discussion: "we multiply the number of cycles reported in [Lit19a, Table 1] by 6 since there are 6 timesteps per code cycle for the surface code". Our surface code cycle is 400 ns. Runtime is `end_time × timestep_ns`, linear in it. Also sweep 50 / 66.7 / 100 ns, and report the timestep at which bicycle's qubit-seconds advantage over surface disappears (solved from the linear relation).
4. **Rotation synthesis: same method and same ε on both sides.**
   - Synthesis gets 1/3 of the budget (legacy convention): ε = (budget/3) / N_rotations.
   - Main table uses gridsynth counts on both sides: compiler `--accuracy ε`; v3 pinned with `PSSPC.q(num_ts_per_rotation=[k])`, where k is the compiler's measured T count per non-T rotation at that ε.
   - Expected for QFT8 at budget 1e-3: ε ≈ 5.3e-6, so about 55 T per rotation (RS fit). This is to be measured, not assumed.
   - Note: RUS / mixed-fallback synthesis would lower T counts on both sides by roughly 4× at these ε. A secondary table can show v3's own choice.
   - *Implementation note:* with k pinned to gridsynth's count (about 55), v3's internal synthesis error 2^((4.86−k)/0.53) is about 1e-28, effectively zero. So v3 must be called with `max_error = budget − N_rot·ε` (the non-synthesis 2/3); otherwise it spends the synthesis third on smaller distances.
5. **Pass rule (bicycle):** `total_error + N_rotations·ε ≤ budget`. Both terms are additive union bounds, so the rule is conservative.
6. **Constants.** Use the code's `model.rs` constants as-is and list the four paper discrepancies (§4) with every result. **Flag on all 1e-4 runs:** the gross_1e-4 shift error (10× lower than the paper) and T-injection error (10× higher than the paper) feed directly into the 1e-4 totals. The two-gross 1e-4 rates match Table 2.
7. **Lookup tables.** Cache them in a gitignored `cache/` folder in this repo, with the path in config. Never write them inside the compiler repo.
8. **Upstream issue #26.** A comment on the qubit-index bug is drafted in `notes/bicycle-issue26-index-bug.md` (not posted).

9. **Operator norm vs diamond.** Gridsynth's ε is an operator-norm bound, while the budget and pass rule are diamond-distance union bounds. The compiler therefore gets `--accuracy ε/2`, and the v3 pin uses gridsynth's count at ε/2.
10. **Synthesis modes.** `synthesis: gridsynth` (default, main results) pins v3 to `ceil(mean gridsynth T count at ε/2)` over the circuit's actual rotation angles and calls v3 with `max_error = budget − rotations·ε`. `synthesis: native` keeps v3's own mixed-fallback search over the whole budget, as a secondary table. With no rotations, gridsynth mode falls back to v3's default query (there is no synthesis to pay for).

### Notes

- **Correction to decision 6:** the 10× mismatches are in the **gross** 1e-4 model, not two-gross. Two-gross 1e-4 matches the paper within rounding (e.g. T-injection error 1e-18 = 10^−24.4 + P_C, where P_C = 1e-18). The flag still applies to the 1e-4 runs via gross_1e-4.

## Open gaps

- **p=1e-4 compares factories as much as architectures.** The two-gross 1e-4 result is dominated by the paper's distillation factory: 18,600 qubits (92–96% of the total on QFT 4–16), output error 6e-25, which the paper itself calls "very conservative". The paper uses cultivation only at p=1e-3, because the cultivation reference gives no end-to-end estimates at p=1e-4 (Tour de gross §2.5, Table 3). The surface side uses v3's own factory search (`RoundBasedFactory`). See `results/qft_comparison/table.md`.
- **QFT and QPE stop at n = 20 (kept for now, by decision).** Larger sizes contain rotations within Litinski's Clifford tolerance, and `prepare` rejects them (see "Shared circuit preparation"). Going further needs an explicit approximate-QFT rule (drop rotations below some angle on both sides) with its error added to the budget.
