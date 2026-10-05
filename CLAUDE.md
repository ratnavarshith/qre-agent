# Project 2: qre-agent

Agent that turns a plain-English problem into fault-tolerant resource estimates on two architectures (surface code via Microsoft's QDK estimator, bicycle/gross code via IBM's bicycle compiler), with a deterministic verifier, evals, and a production layer.

## Hard rules
- You may push, open PRs, and post comments, but only after I've approved the exact text, and every such command must go through the ask prompt. Draft all public text (PR descriptions, issue comments, posts) into `notes/` first.
- `notes/` is gitignored and holds drafts of public text only. Technical notes that should be committed go in `docs/`.
- Commits go under my name only: Ratna Varshith Kolachala <kolachalavarshith@gmail.com>. No Co-Authored-By lines, no "Generated with" footers. Run `git log -5 --format=%B` before telling me a branch is ready.
- LLM/API spend: before any call that costs money, tell me the budget (max tokens / max $ for the run) and get my OK. Track actual spend against it and stop if it would be exceeded.
- One change per branch/PR. Don't touch unrelated code or reformat files you didn't need to change.
- Bug fixes: write the failing test first, show it fails, then fix.
- Before saying anything is done, run the repo's own checks (tests, linters).
- Never commit anything in `notes/`, raw or large run outputs, venvs, `.env`, or API keys. Final results tables, plots, and the configs that produced them do get committed.

## Numbers
- For anything stochastic (LLM evals, sampling), report spread across runs/seeds, never a single run. Deterministic estimator outputs just need versions and settings recorded.
- Record seeds, package versions, and hardware with every result.
- Sanity check every number before showing it to me. If something looks off, say so instead of smoothing it over.
- Experiments are driven by a config file saved next to the results. One command regenerates each table/plot.

## External tools
- Bicycle compiler is at `C:\Users\kolac\GitHub\qldpc\bicycle-architecture-compiler`. Build with `-F bicycle_compiler/rsgridsynth`. Call it by path; never edit that repo from here.
- Its Qiskit parser (`scripts/qiskit_parser.py:75`) emits the wrong angle for PauliEvolutionGate: it outputs `+t·c`; the correct value in the compiler's `exp(iφ/2·P)` convention is `−2·t·c`. Our code must correct for this and have a test for it.
- Its tests hang with rsgridsynth unless run with `--test-threads=1`.
- Pin all dependencies in a lock file. API keys only in env vars.

## Style
- Code should be minimal and readable, match the surrounding code's style.
- Keep explanations to me short and direct.
