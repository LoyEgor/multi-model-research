#!/usr/bin/env python3
"""One-command, domain-agnostic benchmark harness for a research run.

Unlike the OLX-specific gold approach (bench/eval.py + gold.json), this measures the Phase-4
quality properties against the LIVE web at eval time, for ANY query (not just shopping):

  - top-pick credibility: is the recommended URL still live, and right product (page title)?
  - price integrity: live price vs the run's claimed price (drift %), USD coverage.
  - link health: share of verified URLs that are live now.
  - source diversity: host distribution of the verified set (anti-monoculture check).
  - intent gating: counts of off_intent / wrong_tier / not_below_official rejections.
  - cost: model calls, wall time.

Usage:
  python3 bench/harness.py --run <run_id>                  # eval an existing run
  python3 bench/harness.py --prompt "..." [--effort 3] [--site olx.ua]   # run, then eval

The run's config + the live snapshot taken NOW are recorded together, so the report is
self-anchored (no stale external gold). Stdlib-only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "bench"))
import research  # noqa: E402
from check_listing import check  # noqa: E402


def newest_run() -> str | None:
    runs = sorted((ROOT / "runs").glob("*/run.json"), reverse=True)
    return runs[0].parent.name if runs else None


def run_research(prompt: str, effort: str, site: str | None) -> str:
    cmd = [sys.executable, str(ROOT / "research.py"), "--effort", effort]
    if site:
        cmd += ["--site", site]
    cmd.append(prompt)
    print(f"harness: running {cmd}", flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=False)
    return newest_run()


def evaluate(run_id: str) -> dict:
    run_dir = ROOT / "runs" / run_id
    run = json.loads((run_dir / "run.json").read_text())
    verification = json.loads((run_dir / "verification.json").read_text())
    verified = verification.get("verified", [])
    rejected = verification.get("rejected", [])

    # cost
    calls = Counter()
    wall = None
    for meta in (run_dir / "raw").glob("*.meta.json"):
        calls[json.loads(meta.read_text()).get("leg", "?")] += 1
    created, updated = run.get("created_at"), run.get("updated_at")
    if created and updated:
        wall = round((research.parse_iso_ts(updated) - research.parse_iso_ts(created)).total_seconds() / 60, 1)

    # source diversity
    hosts = research.host_distribution(verified)
    top_host_share = (max(hosts.values()) / len(verified)) if verified else 0.0

    # USD coverage + intent gating
    usd_cov = sum(1 for v in verified if v.get("price_usd") is not None)
    rej_reasons = Counter(r for it in rejected for r in (it.get("reasons") or []))

    # top-pick live re-check (the first verified, USD-sorted)
    top = research.sort_by_usd(verified)[0] if verified else None
    top_live = check(top["url"]) if top and top.get("url") else None
    drift = None
    if top and top_live and top_live.get("price") and top.get("price"):
        try:
            drift = round(100 * (top_live["price"] - top["price"]) / top["price"], 1)
        except ZeroDivisionError:
            drift = None

    # link health on the top 5
    top5 = research.sort_by_usd(verified)[:5]
    live = [check(it["url"]) for it in top5 if it.get("url")]
    return {
        "run_id": run_id,
        "effort": (run.get("config") or {}).get("effort"),
        "intent": run.get("intent"),
        "wall_min": wall,
        "calls": dict(calls),
        "verified": len(verified),
        "rejected": len(rejected),
        "usd_coverage": f"{usd_cov}/{len(verified)}",
        "host_distribution": hosts,
        "top_host_share": round(top_host_share, 2),
        "rejection_reasons": dict(rej_reasons),
        "top_pick": top.get("url") if top else None,
        "top_pick_claimed_usd": top.get("price_usd") if top else None,
        "top_pick_live_status": top_live.get("status") if top_live else None,
        "top_pick_live_price": top_live.get("price") if top_live else None,
        "top_pick_price_drift_pct": drift,
        "top5_live": f"{sum(1 for c in live if c['status'] == 'live')}/{len(live)}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Domain-agnostic research benchmark harness.")
    ap.add_argument("--run", help="Evaluate an existing run id.")
    ap.add_argument("--prompt", help="Run a fresh research with this prompt, then evaluate.")
    ap.add_argument("--effort", default="3")
    ap.add_argument("--site", default=None)
    args = ap.parse_args()

    run_id = args.run or (run_research(args.prompt, args.effort, args.site) if args.prompt else newest_run())
    if not run_id:
        print("no run to evaluate", file=sys.stderr)
        return 1
    print(json.dumps(evaluate(run_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
