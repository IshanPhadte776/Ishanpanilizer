"""
Try N seeded panel layouts and keep the best one.

A re-roll costs more than the plain aligned grid -- sliding the grid off a wall's corner
leaves a partial panel at both edges instead of one, and each partial is another unique
type. Searching is how you get a varied layout without paying the worst of that spread.

Writes the winning seed back into the config, so the normal panelize/view step picks it up.

    python scripts/search_layouts.py --trials 20
    python scripts/search_layouts.py --trials 40 --objective types
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.panelizer import load_panelizer_config  # noqa: E402
from src.service import OBJECTIVES, search_panelization  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Search seeded panel layouts and keep the best.")
    parser.add_argument("--config", default="config/panelizer_config.json")
    parser.add_argument("--trials", type=int, default=20, help="How many seeds to try")
    parser.add_argument("--objective", default="cost", choices=sorted(OBJECTIVES),
                        help="What to minimise (default: cost)")
    parser.add_argument("--start-seed", type=int, default=1, help="First seed; seeds run consecutively")
    parser.add_argument("--keep-baseline", action="store_true",
                        help="Also rank the plain aligned grid (seed=None), and adopt it if it wins")
    args = parser.parse_args()

    config = load_panelizer_config(args.config)
    seeds = list(range(args.start_seed, args.start_seed + args.trials))
    if args.keep_baseline:
        seeds = [None] + seeds

    print(f"Searching {len(seeds)} layout(s), minimising {args.objective} ...")
    results = search_panelization(config, seeds, args.objective)

    print(f"\n{'rank':>4} {'seed':>6} {'panels':>7} {'types':>7} {'cost CAD':>11}")
    for rank, result in enumerate(results[:10], start=1):
        s = result["summary"]
        print(f"{rank:>4} {str(result['seed']):>6} {s['total_panels']:>7} "
              f"{s['total_unique_types']:>7} {s['cost_total']:>11,.0f}")
    if len(results) > 10:
        print(f"     ... {len(results) - 10} more")

    best, worst = results[0], results[-1]
    print(f"\nbest  : seed {best['seed']}  ->  CAD {best['summary']['cost_total']:,.0f}")
    print(f"worst : seed {worst['seed']}  ->  CAD {worst['summary']['cost_total']:,.0f}")
    print(f"spread: CAD {worst['summary']['cost_total'] - best['summary']['cost_total']:,.0f}")

    with open(args.config, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["seed"] = best["seed"]
    with open(args.config, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, indent=2)
    print(f"\nWrote seed={best['seed']} to {args.config}")


if __name__ == "__main__":
    main()
