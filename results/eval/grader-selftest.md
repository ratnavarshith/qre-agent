# Grader self-test

`evals/tasks.yaml` (sha256 230e2860eddc). Each task's reference answer, built with the real tools, must be graded correct; each corrupted answer must fail with the expected category. Regenerate with `.venv/Scripts/python scripts/run_eval.py --self-test`.

257/257 cases as expected (standard: 156, free-form: 62, ambiguous: 39).

| task | type | variant | expected | got | ok |
|---|---|---|---|---|---|
| std-qft4-1e3-twogross | standard | reference | correct | correct | yes |
| std-qft4-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qft4-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qft4-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qft4-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qft4-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-qft4-1e4-gross | standard | reference | correct | correct | yes |
| std-qft4-1e4-gross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qft4-1e4-gross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qft4-1e4-gross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qft4-1e4-gross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qft4-1e4-gross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-qft16-1e4-twogross | standard | reference | correct | correct | yes |
| std-qft16-1e4-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qft16-1e4-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qft16-1e4-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qft16-1e4-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qft16-1e4-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-qft20-1e3-twogross | standard | reference | correct | correct | yes |
| std-qft20-1e3-twogross | standard | budget fail unstated | missed budget fail | missed budget fail | yes |
| std-qft20-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qft20-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qft20-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qft20-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qft20-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-qft8-1e3-gross | standard | reference | correct | correct | yes |
| std-qft8-1e3-gross | standard | budget fail unstated | missed budget fail | missed budget fail | yes |
| std-qft8-1e3-gross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qft8-1e3-gross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qft8-1e3-gross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qft8-1e3-gross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qft8-1e3-gross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-qpe4-1e3-twogross | standard | reference | correct | correct | yes |
| std-qpe4-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qpe4-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qpe4-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qpe4-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qpe4-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-qpe8-1e4-twogross | standard | reference | correct | correct | yes |
| std-qpe8-1e4-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qpe8-1e4-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qpe8-1e4-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qpe8-1e4-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qpe8-1e4-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-qpe16-1e3-twogross | standard | reference | correct | correct | yes |
| std-qpe16-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qpe16-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qpe16-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qpe16-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qpe16-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-qpe4-1e4-gross | standard | reference | correct | correct | yes |
| std-qpe4-1e4-gross | standard | budget fail unstated | missed budget fail | missed budget fail | yes |
| std-qpe4-1e4-gross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-qpe4-1e4-gross | standard | runtime in ms | unit/format | unit/format | yes |
| std-qpe4-1e4-gross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-qpe4-1e4-gross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-qpe4-1e4-gross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-adder8-1e3-twogross | standard | reference | correct | correct | yes |
| std-adder8-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-adder8-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-adder8-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-adder8-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-adder8-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-adder4-1e4-gross | standard | reference | correct | correct | yes |
| std-adder4-1e4-gross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-adder4-1e4-gross | standard | runtime in ms | unit/format | unit/format | yes |
| std-adder4-1e4-gross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-adder4-1e4-gross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-adder4-1e4-gross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-adder16-1e4-gross | standard | reference | correct | correct | yes |
| std-adder16-1e4-gross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-adder16-1e4-gross | standard | runtime in ms | unit/format | unit/format | yes |
| std-adder16-1e4-gross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-adder16-1e4-gross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-adder16-1e4-gross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-adder32-1e3-twogross | standard | reference | correct | correct | yes |
| std-adder32-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-adder32-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-adder32-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-adder32-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-adder32-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-adder64-1e4-twogross | standard | reference | correct | correct | yes |
| std-adder64-1e4-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-adder64-1e4-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-adder64-1e4-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-adder64-1e4-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-adder64-1e4-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-adder16-1e3-gross | standard | reference | correct | correct | yes |
| std-adder16-1e3-gross | standard | budget fail unstated | missed budget fail | missed budget fail | yes |
| std-adder16-1e3-gross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-adder16-1e3-gross | standard | runtime in ms | unit/format | unit/format | yes |
| std-adder16-1e3-gross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-adder16-1e3-gross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-adder16-1e3-gross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-tfim4-1e3-twogross | standard | reference | correct | correct | yes |
| std-tfim4-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-tfim4-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-tfim4-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-tfim4-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-tfim4-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-tfim8-1e4-twogross | standard | reference | correct | correct | yes |
| std-tfim8-1e4-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-tfim8-1e4-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-tfim8-1e4-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-tfim8-1e4-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-tfim8-1e4-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-tfim16-1e3-twogross | standard | reference | correct | correct | yes |
| std-tfim16-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-tfim16-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-tfim16-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-tfim16-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-tfim16-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-tfim32-1e3-twogross | standard | reference | correct | correct | yes |
| std-tfim32-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-tfim32-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-tfim32-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-tfim32-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-tfim32-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-tfim64-1e3-twogross | standard | reference | correct | correct | yes |
| std-tfim64-1e3-twogross | standard | budget fail unstated | missed budget fail | missed budget fail | yes |
| std-tfim64-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-tfim64-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-tfim64-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-tfim64-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-tfim64-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-grover4-1e3-twogross | standard | reference | correct | correct | yes |
| std-grover4-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-grover4-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-grover4-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-grover4-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-grover4-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-grover8-1e4-gross | standard | reference | correct | correct | yes |
| std-grover8-1e4-gross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-grover8-1e4-gross | standard | runtime in ms | unit/format | unit/format | yes |
| std-grover8-1e4-gross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-grover8-1e4-gross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-grover8-1e4-gross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-grover16-1e3-twogross | standard | reference | correct | correct | yes |
| std-grover16-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-grover16-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-grover16-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-grover16-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-grover16-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-grover32-1e4-gross | standard | reference | correct | correct | yes |
| std-grover32-1e4-gross | standard | budget fail unstated | missed budget fail | missed budget fail | yes |
| std-grover32-1e4-gross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-grover32-1e4-gross | standard | runtime in ms | unit/format | unit/format | yes |
| std-grover32-1e4-gross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-grover32-1e4-gross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-grover32-1e4-gross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| std-grover64-1e3-twogross | standard | reference | correct | correct | yes |
| std-grover64-1e3-twogross | standard | made-up ratio | made-up numbers | made-up numbers | yes |
| std-grover64-1e3-twogross | standard | runtime in ms | unit/format | unit/format | yes |
| std-grover64-1e3-twogross | standard | wrong size | wrong circuit | wrong circuit | yes |
| std-grover64-1e3-twogross | standard | wrong p | wrong assumptions | wrong assumptions | yes |
| std-grover64-1e3-twogross | standard | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-qft-noswap-5 | free-form | reference | correct | correct | yes |
| ff-qft-noswap-5 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-qft-noswap-5 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-qft-noswap-5 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-qft-noswap-5 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-qft-noswap-5 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-qft-noswap-6 | free-form | reference | correct | correct | yes |
| ff-qft-noswap-6 | free-form | budget fail unstated | missed budget fail | missed budget fail | yes |
| ff-qft-noswap-6 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-qft-noswap-6 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-qft-noswap-6 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-qft-noswap-6 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-qft-noswap-6 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-grover-4 | free-form | reference | correct | correct | yes |
| ff-grover-4 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-grover-4 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-grover-4 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-grover-4 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-grover-4 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-grover-5 | free-form | reference | correct | correct | yes |
| ff-grover-5 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-grover-5 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-grover-5 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-grover-5 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-grover-5 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-adder-interleaved-4 | free-form | reference | correct | correct | yes |
| ff-adder-interleaved-4 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-adder-interleaved-4 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-adder-interleaved-4 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-adder-interleaved-4 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-adder-interleaved-4 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-adder-layout-3 | free-form | reference | correct | correct | yes |
| ff-adder-layout-3 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-adder-layout-3 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-adder-layout-3 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-adder-layout-3 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-adder-layout-3 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-ghz-rz-5 | free-form | reference | correct | correct | yes |
| ff-ghz-rz-5 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-ghz-rz-5 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-ghz-rz-5 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-ghz-rz-5 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-ghz-rz-5 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-draper-adder-4 | free-form | reference | correct | correct | yes |
| ff-draper-adder-4 | free-form | budget fail unstated | missed budget fail | missed budget fail | yes |
| ff-draper-adder-4 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-draper-adder-4 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-draper-adder-4 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-draper-adder-4 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-draper-adder-4 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-heisenberg-4 | free-form | reference | correct | correct | yes |
| ff-heisenberg-4 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-heisenberg-4 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-heisenberg-4 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-heisenberg-4 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-heisenberg-4 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| ff-crz-ladder-4 | free-form | reference | correct | correct | yes |
| ff-crz-ladder-4 | free-form | made-up ratio | made-up numbers | made-up numbers | yes |
| ff-crz-ladder-4 | free-form | runtime in ms | unit/format | unit/format | yes |
| ff-crz-ladder-4 | free-form | wrong circuit | wrong circuit | wrong circuit | yes |
| ff-crz-ladder-4 | free-form | wrong p | wrong assumptions | wrong assumptions | yes |
| ff-crz-ladder-4 | free-form | wrong code | wrong assumptions | wrong assumptions | yes |
| amb-grover8-p-code | ambiguous | reference | correct | correct | yes |
| amb-grover8-p-code | ambiguous | made-up ratio | made-up numbers | made-up numbers | yes |
| amb-grover8-p-code | ambiguous | runtime in ms | unit/format | unit/format | yes |
| amb-grover8-p-code | ambiguous | wrong size | wrong circuit | wrong circuit | yes |
| amb-grover8-p-code | ambiguous | alternative assumption | correct | correct | yes |
| amb-grover8-p-code | ambiguous | assumption unstated | wrong assumptions | wrong assumptions | yes |
| amb-grover8-p-code | ambiguous | assumption misstated | wrong assumptions | wrong assumptions | yes |
| amb-qft-n | ambiguous | reference | correct | correct | yes |
| amb-qft-n | ambiguous | made-up ratio | made-up numbers | made-up numbers | yes |
| amb-qft-n | ambiguous | runtime in ms | unit/format | unit/format | yes |
| amb-qft-n | ambiguous | wrong p | wrong assumptions | wrong assumptions | yes |
| amb-qft-n | ambiguous | wrong code | wrong assumptions | wrong assumptions | yes |
| amb-qft-n | ambiguous | alternative assumption | correct | correct | yes |
| amb-qft-n | ambiguous | assumption unstated | wrong assumptions | wrong assumptions | yes |
| amb-qft-n | ambiguous | assumption misstated | wrong assumptions | wrong assumptions | yes |
| amb-adder4-code | ambiguous | reference | correct | correct | yes |
| amb-adder4-code | ambiguous | made-up ratio | made-up numbers | made-up numbers | yes |
| amb-adder4-code | ambiguous | runtime in ms | unit/format | unit/format | yes |
| amb-adder4-code | ambiguous | wrong size | wrong circuit | wrong circuit | yes |
| amb-adder4-code | ambiguous | wrong p | wrong assumptions | wrong assumptions | yes |
| amb-adder4-code | ambiguous | alternative assumption | correct | correct | yes |
| amb-adder4-code | ambiguous | assumption unstated | wrong assumptions | wrong assumptions | yes |
| amb-adder4-code | ambiguous | assumption misstated | wrong assumptions | wrong assumptions | yes |
| amb-tfim8-p | ambiguous | reference | correct | correct | yes |
| amb-tfim8-p | ambiguous | made-up ratio | made-up numbers | made-up numbers | yes |
| amb-tfim8-p | ambiguous | runtime in ms | unit/format | unit/format | yes |
| amb-tfim8-p | ambiguous | wrong size | wrong circuit | wrong circuit | yes |
| amb-tfim8-p | ambiguous | wrong code | wrong assumptions | wrong assumptions | yes |
| amb-tfim8-p | ambiguous | alternative assumption | correct | correct | yes |
| amb-tfim8-p | ambiguous | assumption unstated | wrong assumptions | wrong assumptions | yes |
| amb-tfim8-p | ambiguous | assumption misstated | wrong assumptions | wrong assumptions | yes |
| amb-qpe-n-p | ambiguous | reference | correct | correct | yes |
| amb-qpe-n-p | ambiguous | budget fail unstated | missed budget fail | missed budget fail | yes |
| amb-qpe-n-p | ambiguous | made-up ratio | made-up numbers | made-up numbers | yes |
| amb-qpe-n-p | ambiguous | runtime in ms | unit/format | unit/format | yes |
| amb-qpe-n-p | ambiguous | wrong code | wrong assumptions | wrong assumptions | yes |
| amb-qpe-n-p | ambiguous | alternative assumption | correct | correct | yes |
| amb-qpe-n-p | ambiguous | assumption unstated | wrong assumptions | wrong assumptions | yes |
| amb-qpe-n-p | ambiguous | assumption misstated | wrong assumptions | wrong assumptions | yes |
