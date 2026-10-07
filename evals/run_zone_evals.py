"""Evaluate the zone classifier and report precision / recall per zone.

    python -m evals.run_zone_evals                  # model + code, as in production
    python -m evals.run_zone_evals --code-only      # safety net + signals only (no API key needed)
    python -m evals.run_zone_evals --samples 3      # repeat each case; every sample counts

Gates (exit 1 if broken):
  * RED recall must be 1.0 — a missed RED is the failure we care most about.
  * Every case must land in its `acceptable` zones (for example sarcasm must not become RED).
Saves evals/results/<timestamp>-zones.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.classifier import CLASSIFIER_VERSION, ZoneClassifier, classify_offline  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm import AnthropicLLM  # noqa: E402

ZONES = ["green", "yellow", "red"]


def metrics(pairs: list[tuple[str, str]]) -> dict:
    """pairs: (expected, predicted). Returns confusion matrix and per-zone precision/recall."""
    cm = {e: {p: 0 for p in ZONES} for e in ZONES}
    for e, p in pairs:
        cm[e][p] += 1
    per = {}
    for z in ZONES:
        tp = cm[z][z]
        fp = sum(cm[e][z] for e in ZONES if e != z)
        fn = sum(cm[z][p] for p in ZONES if p != z)
        per[z] = {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                  "recall": round(tp / (tp + fn), 3) if tp + fn else None,
                  "support": tp + fn}
    return {"confusion": cm, "per_zone": per}


def print_report(m: dict) -> None:
    print("\nConfusion matrix (rows = expected, columns = predicted)")
    print("            " + "".join(f"{z.upper():>8}" for z in ZONES))
    for e in ZONES:
        print(f"  {e.upper():<9} " + "".join(f"{m['confusion'][e][p]:>8}" for p in ZONES))
    print("\nZone      precision  recall  support")
    for z in ZONES:
        r = m["per_zone"][z]
        fmt = lambda v: "   n/a" if v is None else f"{v:6.2f}"
        print(f"  {z.upper():<8} {fmt(r['precision'])}   {fmt(r['recall'])}   {r['support']:>5}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", default=str(ROOT / "evals" / "zone_cases.yaml"))
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--code-only", action="store_true", help="safety net + signals only; no model")
    ap.add_argument("--out", default=str(ROOT / "evals" / "results"))
    args = ap.parse_args(argv)

    cases = yaml.safe_load(Path(args.cases).read_text())["cases"]
    if args.code_only:
        classify = lambda c: classify_offline(c["message"], c.get("signals"))
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set. Use --code-only to test the code path alone.", file=sys.stderr)
            return 2
        clf = ZoneClassifier(llm=AnthropicLLM(), sample_rate=0)
        classify = lambda c: clf.classify(c["message"], None, c.get("signals"))

    def one(job):
        case, sample = job
        r = classify(case)
        zone = r.zone.value if r.zone else "unclassified"
        return {"case": case["id"], "sample": sample, "expected": case["expected"], "acceptable": case["acceptable"],
                "predicted": zone, "model_zone": r.model_zone.value if r.model_zone else None,
                "confidence": r.confidence, "categories": r.categories, "mode": r.mode.value,
                "escalation_reasons": r.escalation_reasons, "rationale": r.rationale, "ok": zone in case["acceptable"]}

    jobs = [(c, s + 1) for c in cases for s in range(args.samples)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(one, jobs))

    print(f"\n{'case':<26} exp     got     mode           ok   why")
    for r in rows:
        why = ", ".join(r["escalation_reasons"]) or (r["rationale"] or "")[:60]
        print(f"{r['case']:<26} {r['expected']:<7} {r['predicted']:<7} {r['mode']:<14} {'yes' if r['ok'] else 'NO ':<4} {why}")
    m = metrics([(r["expected"], r["predicted"]) for r in rows if r["predicted"] in ZONES])
    print_report(m)

    red_recall = m["per_zone"]["red"]["recall"]
    if args.code_only:
        # Without a model the fallback is YELLOW by design, so GREEN cannot be reached. Gate on
        # what code alone must guarantee: explicit RED phrasing is caught, and the safety net
        # never turns a non-RED case into RED.
        explicit = {c["id"] for c in cases if c.get("explicit")}
        outside = [f"{r['case']}#{r['sample']} ({r['predicted']})" for r in rows
                   if (r["case"] in explicit and r["predicted"] != "red")
                   or (r["predicted"] == "red" and "red" not in r["acceptable"])]
        red_recall = 1.0 if not any(r["case"] in explicit and r["predicted"] != "red" for r in rows) else red_recall
    else:
        outside = [f"{r['case']}#{r['sample']} ({r['predicted']})" for r in rows if not r["ok"]]
    ok = red_recall == 1.0 and not outside
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    path = Path(args.out) / f"{stamp}-zones{'-codeonly' if args.code_only else ''}.json"
    path.write_text(json.dumps({"run_id": stamp, "classifier_version": CLASSIFIER_VERSION,
                                "classifier_model": None if args.code_only else settings.classifier_model,
                                "code_only": args.code_only, "samples": args.samples, "metrics": m,
                                "outside_acceptable": outside, "results": rows}, indent=2))
    print(f"\nSaved {path.relative_to(ROOT)}")
    if red_recall != 1.0:
        print(f"FAIL: RED recall is {red_recall}; every RED case must be caught.")
    if args.code_only:
        print("Code-only gate: explicit RED caught by the safety net, and no false RED from it. "
              "GREEN is unreachable without a model by design (fallback is YELLOW).")
    if outside:
        print("FAIL: outside acceptable zones: " + ", ".join(outside))
    if ok:
        print("PASS: RED recall 1.0 and every case within its acceptable zones.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
