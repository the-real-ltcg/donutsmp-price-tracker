#!/usr/bin/env python3
"""
Daily liveness check: has donut.auction started ingesting sales again?

DonutSMP disabled their official API on 2026-09-29 and donut.auction scrapes
from it, so its transaction feed froze. The endpoints still return HTTP 200 with
the same snapshot, which is why this checks the AGE OF THE NEWEST SALE rather
than whether the request succeeded.

Writes nothing to the spreadsheet and makes only a few requests. Appends one
line per run to upstream_status.log, and prints loudly when the feed revives.

Usage:
    py check_upstream.py            # check, log, print verdict
    py check_upstream.py --quiet    # log only (for the scheduled task)

Exit codes:  0 = feed is live    1 = still frozen    2 = could not reach it
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import track

LOG = track.SCRIPT_DIR / "upstream_status.log"

# A handful of normally-busy items. If several agree the feed is live, it is not
# just one item that happened to get a late scrape.
PROBES = {
    "netherite_ingot": "e5bce2cd-c5d1-42c2-9f3a-86f298c6ecff",
    "gilded_blackstone": "c5e2b819-5679-408f-ac72-93dc0de5e048",
    "end_crystal": "43a96e6f-ebd5-4fe8-8768-b46272d1d91d",
    "sea_pickle": "435f9262-6352-4908-bbc0-08190b7a7043",
}

FRESH_MIN = 90          # newest sale younger than this = feed is moving again


def main() -> int:
    ap = argparse.ArgumentParser(description="Check if donut.auction is live again.")
    ap.add_argument("--quiet", action="store_true", help="log only, minimal output")
    args = ap.parse_args()

    ages: dict[str, float] = {}
    errors = 0
    for name, iid in PROBES.items():
        try:
            s = track.fetch_sold(iid)
            ages[name] = s["age_min"] if s else float("inf")
        except Exception as exc:              # noqa: BLE001 - network/API trouble
            errors += 1
            if not args.quiet:
                print(f"  {name}: unreachable ({type(exc).__name__})", file=sys.stderr)

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not ages:
        line = f"{stamp}  UNREACHABLE  all {len(PROBES)} probes failed"
        LOG.write_text(LOG.read_text(encoding="utf-8") + line + "\n"
                       if LOG.exists() else line + "\n", encoding="utf-8")
        print(line)
        return 2

    fresh = [n for n, a in ages.items() if a <= FRESH_MIN]
    best = min(ages.values())
    verdict = "LIVE" if len(fresh) >= 2 else "FROZEN"
    detail = "  ".join(f"{n}={a / 60:.1f}h" for n, a in sorted(ages.items()))
    line = f"{stamp}  {verdict}  newest={best / 60:.1f}h  {detail}"

    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    if verdict == "LIVE":
        # Deliberately loud: this is the whole point of the check.
        print("=" * 68)
        print("  donut.auction IS INGESTING SALES AGAIN")
        print(f"  newest sale is {best / 60:.1f}h old ({len(fresh)}/{len(ages)} probes fresh)")
        print("  Re-enable tracking with:")
        print('    Enable-ScheduledTask -TaskName "Donut Price Tracker"')
        print('    Enable-ScheduledTask -TaskName "Donut Market Scan"')
        print("=" * 68)
        return 0

    if not args.quiet:
        print(line)
        print(f"  still frozen -- newest sale {best / 60:.1f}h old "
              f"(need under {FRESH_MIN / 60:.1f}h on 2+ items)")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
