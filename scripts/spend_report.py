"""LLM spend per phase and model, and the remaining budget.

Run: .venv/Scripts/python scripts/spend_report.py
"""

from collections import defaultdict

import yaml

from qre_agent.spend import BUDGETS_PATH, LOG_PATH, read_log, spent


def main():
    caps = yaml.safe_load(BUDGETS_PATH.read_text(encoding="utf-8"))["caps"]
    records = read_log(LOG_PATH)
    by_model = defaultdict(lambda: [0, 0, 0, 0.0])
    for r in records:
        row = by_model[(r["phase"], r["model"])]
        row[0] += 1
        row[1] += r["input_tokens"]
        row[2] += r["output_tokens"]
        row[3] += r["cost_usd"]

    print(f"{'phase':<8}{'model':<40}{'calls':>6}{'in tok':>10}{'out tok':>10}{'$':>10}")
    for (phase, model), (calls, tin, tout, usd) in sorted(by_model.items()):
        print(f"{phase:<8}{model:<40}{calls:>6}{tin:>10}{tout:>10}{usd:>10.4f}")

    print(f"\n{'phase':<8}{'cap $':>8}{'spent $':>10}{'left $':>10}")
    for phase, cap in caps.items():
        s = spent(records, phase)
        print(f"{phase:<8}{cap:>8}{s:>10.4f}{cap - s:>10.4f}")
    total = sum(caps.values())
    s = sum(r["cost_usd"] for r in records)
    print(f"{'total':<8}{total:>8}{s:>10.4f}{total - s:>10.4f}")


if __name__ == "__main__":
    main()
