"""Suggest a similarity threshold from an evaluation run's raw retrieval scores.

    python eval/calibrate.py eval/results/<config>.json

Prints the top score of every question, grouped by whether it should be answered or
refused, and the threshold that separates them best. Set it as "similarityThreshold" in
cdk.json, deploy, and run the evaluation again to confirm.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from metrics import SHOULD_REFUSE, calibrate


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    results = report["results"]
    for title, keep in (
        ("Should be answered", lambda r: r["expected_outcome"] == "ANSWERED"),
        ("Should be refused", lambda r: r["category"] in SHOULD_REFUSE),
    ):
        print(f"\n{title}:")
        for r in sorted(filter(keep, results), key=lambda r: r["top_similarity"]):
            print(f"  {r['id']}  {r['category']:12}  {r['top_similarity']:.3f}")

    result = calibrate(results)
    print(f"\nLowest answerable score: {result['lowest_answerable']}")
    print(f"Highest refusable score: {result['highest_refusable']}")
    if result["gap"] > 0:
        print(f"Clean gap of {result['gap']}: every question can be handled correctly.")
    else:
        print(f"The ranges overlap by {-result['gap']}: no threshold gets every question right.")
    print(
        f"\nSuggested similarityThreshold: {result['suggested_threshold']} "
        f"({result['correct_at_suggestion']} of {result['questions']} correct)"
    )
    if result["would_wrongly_refuse"]:
        print(f"  would refuse answerable: {', '.join(result['would_wrongly_refuse'])}")
    if result["would_wrongly_answer"]:
        print(f"  would answer refusable: {', '.join(result['would_wrongly_answer'])}")
    print(f"(currently deployed: {report['config'].get('threshold', 0.35)})")


if __name__ == "__main__":
    main()
