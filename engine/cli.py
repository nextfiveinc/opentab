"""CLI: python -m engine run|review|approve|reject|reclassify|lifecycle|validate|build <pack>"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="engine", description="Catalogue engine")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="discover -> qualify -> enrich -> gate")
    p_run.add_argument("pack", help="pack folder, e.g. packs/webapps")
    p_run.add_argument("--only", help="run a single source by id")
    p_run.add_argument("--dry-run", action="store_true", help="do not write data")
    p_run.add_argument("--max-calls", type=int, metavar="N",
                       help="raise the classifier's per-run safety fuse (classifier.max_calls_per_run) "
                            "for a one-off bulk import")

    p_build = sub.add_parser("build", help="compile published files from data/")
    p_build.add_argument("pack")
    p_build.add_argument("--out", default="dist")
    p_build.add_argument("--dry-run", action="store_true")

    p_life = sub.add_parser("lifecycle", help="re-check published items")
    p_life.add_argument("pack")
    p_life.add_argument("--all", action="store_true", dest="everything",
                        help="re-check every item now, not only those due")

    p_val = sub.add_parser("validate", help="validate pack config and data")
    p_val.add_argument("pack")

    p_review = sub.add_parser("review", help="list the review queue, most confident first")
    p_review.add_argument("pack")

    p_approve = sub.add_parser("approve", help="promote a review item to the published store")
    p_approve.add_argument("pack")
    p_approve.add_argument("item_id")

    p_reject = sub.add_parser("reject", help="drop a review item and log the reason")
    p_reject.add_argument("pack")
    p_reject.add_argument("item_id")
    p_reject.add_argument("--reason", default="human-reject")

    p_reclass = sub.add_parser("reclassify",
                               help="re-run AI classification over existing review items")
    p_reclass.add_argument("pack")
    p_reclass.add_argument("--dry-run", action="store_true", help="report verdicts, write nothing")
    p_reclass.add_argument("--keep", action="append", default=[], metavar="ID",
                           help="keep this review item in review whatever the gate says (repeatable)")

    sub.add_parser("models", help="list OpenRouter's live decision models (for decide.yml)")

    p_sum = sub.add_parser("summary", help="markdown summary of saved run/lifecycle/thumbs reports")
    p_sum.add_argument("pack")
    p_sum.add_argument("--run")
    p_sum.add_argument("--lifecycle")
    p_sum.add_argument("--thumbs")

    p_thumbs = sub.add_parser("thumbs", help="download and store preview thumbnails (data/img/)")
    p_thumbs.add_argument("pack")
    p_thumbs.add_argument("--refresh", action="store_true", help="re-download existing thumbnails")
    p_thumbs.add_argument("--no-screenshots", action="store_true",
                          help="only download preview images; never screenshot an app")

    p_page = sub.add_parser("review-page", help="write a local review page for the queue")
    p_page.add_argument("pack")
    p_page.add_argument("--out", default="review/index.html")
    p_page.add_argument("--decisions", action="append", default=[], metavar="FILE",
                        help="pre-load an earlier export (repeatable; merged, latest decision wins)")

    p_srcpage = sub.add_parser("sources-page", help="write a local page of sources (maintainer only)")
    p_srcpage.add_argument("pack")
    p_srcpage.add_argument("--out", default="review/sources.html")

    p_apply = sub.add_parser("apply-decisions", help="apply a decisions.json from the review page")
    p_apply.add_argument("pack")
    p_apply.add_argument("file", nargs="+", help="one or more exports; merged, latest decision wins")
    p_apply.add_argument("--dry-run", action="store_true")

    p_bulk = sub.add_parser("bulk", help="approve/reject many review items by rule (dry run unless --yes)")
    p_bulk.add_argument("pack")
    p_bulk.add_argument("--reject-below", type=float, help="reject items under this confidence")
    p_bulk.add_argument("--approve-at", type=float, help="approve AI-classified items at/above this confidence")
    p_bulk.add_argument("--apply-rules", action="store_true", help="reject items today's rules.yml excludes")
    p_bulk.add_argument("--tier", action="append", help="limit to source tier(s), e.g. --tier A")
    p_bulk.add_argument("--category")
    p_bulk.add_argument("--yes", action="store_true", help="apply (default is a dry run)")

    p_cases = sub.add_parser("eval-cases", help="snapshot inputs for the labelled eval set")
    p_cases.add_argument("pack")
    p_cases.add_argument("--limit", type=int, default=200)

    p_eval = sub.add_parser("eval", help="score the classifier against data/eval/labels.yml")
    p_eval.add_argument("pack")
    p_eval.add_argument("--model", action="append", help="decision model id to try (repeatable)")
    p_eval.add_argument("--mode", choices=["decide", "hybrid", "rules"], help="override classifier.mode")
    p_eval.add_argument("--json", action="store_true", help="print the full JSON report")

    args = parser.parse_args(argv)

    from engine import pipeline

    if args.command == "run":
        report = pipeline.cmd_run(args.pack, only=args.only, dry_run=args.dry_run,
                                  max_calls=args.max_calls)
    elif args.command == "build":
        report = pipeline.cmd_build(args.pack, out_dir=args.out, dry_run=args.dry_run)
    elif args.command == "lifecycle":
        report = pipeline.cmd_lifecycle(args.pack, everything=args.everything)
    elif args.command == "review":
        report = pipeline.cmd_review(args.pack)
    elif args.command == "approve":
        report = pipeline.cmd_approve(args.pack, args.item_id)
    elif args.command == "reject":
        report = pipeline.cmd_reject(args.pack, args.item_id, reason=args.reason)
    elif args.command == "reclassify":
        report = pipeline.cmd_reclassify(args.pack, dry_run=args.dry_run, keep=args.keep)
    elif args.command == "eval-cases":
        from engine.eval import build_cases
        report = build_cases(pipeline._context(args.pack), limit=args.limit)
    elif args.command == "eval":
        from engine.eval import run_eval, summarise
        report = run_eval(pipeline._context(args.pack, dry_run=True), models=args.model, mode=args.mode)
        if not args.json:
            print(summarise(report))
            return 0
    elif args.command == "thumbs":
        from engine.stages import thumbs
        report = thumbs.run(pipeline._context(args.pack, private=False), refresh=args.refresh,
                            screenshots=False if args.no_screenshots else None)
    elif args.command == "review-page":
        from pathlib import Path

        from engine.review import write_page
        report = write_page(pipeline._context(args.pack), Path(args.out), seed=args.decisions)
    elif args.command == "sources-page":
        from pathlib import Path

        from engine.review import write_sources_page
        report = write_sources_page(pipeline._context(args.pack), Path(args.out))
    elif args.command == "apply-decisions":
        from engine.review import apply_decisions
        report = apply_decisions(args.pack, args.file, dry_run=args.dry_run)
    elif args.command == "bulk":
        from engine.review import bulk
        report = bulk(args.pack, reject_below=args.reject_below, approve_at=args.approve_at,
                      tiers=args.tier, category=args.category, yes=args.yes, apply_rules=args.apply_rules)
    elif args.command == "summary":
        from engine.config import load_pack
        from engine.summary import summarise
        print(summarise(load_pack(args.pack), args.run, args.lifecycle, args.thumbs), end="")
        return 0
    elif args.command == "models":
        from engine.llm.decisions import list_decision_models
        report = {"decision_models": list_decision_models(),
                  "note": "Put a versioned id (not a ~alias) in decide.yml `model`, then run eval."}
    else:
        report = pipeline.cmd_validate(args.pack)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    # validate is a check: non-zero exit on errors, so CI and the scheduled update stop on bad data
    return 1 if args.command == "validate" and report.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
