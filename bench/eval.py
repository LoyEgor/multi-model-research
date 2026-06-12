#!/usr/bin/env python3
"""Evaluate benchmark runs against the gold reference.

Usage: python3 bench/eval.py bench/gold.json bench/bench-runs.txt

Per run: wall time, model-call counts/latency (cost proxy), verified/rejected/disputed,
top-pick extraction from final.md, live-check of recommended URLs (direct listing vs
search page, alive vs dead, current price), price gap vs gold best credible price.
Prints a Markdown table plus per-run details for the human judge.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_listing import check  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DIRECT_LISTING_RE = re.compile(r"olx\.ua/d/(?:uk/)?obyavlenie/", re.IGNORECASE)
URL_RE = re.compile(r"https://[^\s)\]\">]+")


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def report_urls(final_md: str) -> list[str]:
    """Ordered unique olx URLs from the report body, up to the Unverified section."""
    main_part = re.split(r"#+\s*(?:Unverified|Неперевірені|Непроверенные)", final_md)[0]
    urls, seen = [], set()
    for url in URL_RE.findall(main_part):
        url = url.rstrip(".,;")
        if "olx" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def evaluate_run(run_dir: Path, gold: dict) -> dict:
    run = json.loads((run_dir / "run.json").read_text())
    verification = json.loads((run_dir / "verification.json").read_text())
    final_md = (run_dir / "final.md").read_text() if (run_dir / "final.md").exists() else ""

    calls: dict[str, int] = {}
    total_latency = 0.0
    for meta_file in (run_dir / "raw").glob("*.meta.json"):
        meta = json.loads(meta_file.read_text())
        calls[meta["task_type"]] = calls.get(meta["task_type"], 0) + 1
        total_latency += meta.get("latency_sec") or 0

    urls = report_urls(final_md)
    top5 = urls[:5]
    checks = [check(url) for url in top5]
    live = sum(1 for c in checks if c["status"] == "live")
    direct = sum(1 for u in top5 if DIRECT_LISTING_RE.search(u))

    top_pick = checks[0] if checks else None
    gold_best = gold["best_pick"]["price"]
    price_by_url = {v.get("url"): v.get("price") for v in verification.get("verified", [])}
    top_claimed = price_by_url.get(urls[0]) if urls else None

    return {
        "effort": (run.get("config") or {}).get("effort"),
        "run_id": run_dir.name,
        "wall_min": round((parse_ts(run["updated_at"]) - parse_ts(run["created_at"])).total_seconds() / 60, 1),
        "calls": calls,
        "calls_total": sum(calls.values()),
        "model_minutes": round(total_latency / 60, 1),
        "verified": run.get("verified_count"),
        "rejected": run.get("rejected_count"),
        "disputed": run.get("disputed_count"),
        "report_urls": len(urls),
        "top5_live": f"{live}/{len(checks)}",
        "top5_direct": f"{direct}/{len(top5)}",
        "top_pick_url": urls[0] if urls else None,
        "top_pick_claimed_price": top_claimed,
        "top_pick_live_price": top_pick.get("price") if top_pick else None,
        "top_pick_status": top_pick.get("status") if top_pick else None,
        "gold_gap": (top_pick.get("price") - gold_best) if top_pick and top_pick.get("price") else None,
    }


def main() -> int:
    gold = json.loads(Path(sys.argv[1]).read_text())
    rows = []
    for line in Path(sys.argv[2]).read_text().splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] == "FAILED":
            continue
        rows.append(evaluate_run(ROOT / "runs" / parts[1], gold))

    cols = [
        ("effort", "effort"), ("wall_min", "wall min"), ("calls_total", "model calls"),
        ("model_minutes", "model min"), ("verified", "ok"), ("rejected", "rej"),
        ("disputed", "disp"), ("report_urls", "urls in report"), ("top5_live", "top5 live"),
        ("top5_direct", "top5 direct"), ("top_pick_live_price", "top price (live)"),
        ("gold_gap", "gap vs gold"),
    ]
    print("| " + " | ".join(label for _, label in cols) + " |")
    print("|" + "---|" * len(cols))
    for row in rows:
        print("| " + " | ".join(str(row.get(key)) for key, _ in cols) + " |")
    print()
    print(f"gold best credible price: {gold['best_pick']['price']} UAH — {gold['best_pick']['url']}")
    print()
    for row in rows:
        print(json.dumps(row, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
