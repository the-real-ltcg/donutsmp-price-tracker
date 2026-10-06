#!/usr/bin/env python3
"""
Whole-market scanner: every tradeable item on the DonutSMP auction house.

Why this is separate from track.py, and shaped differently:

  * There are ~1030 tradeable items. A history tab per item would write
    1032 x 144 x 11 = 1.6M cells/day, and a Google spreadsheet caps at 10M
    cells -- the sheet would be full in about six days.
  * A full sweep is one request per item (the real-fills transaction feed has
    no bulk form), which takes ~5 minutes. That cannot run every 10 minutes.

So this keeps ONE row per item in an "All Items" snapshot that is rewritten on
each scan, plus ONE row per item per DAY in "Market History". That is ~11k
cells for the snapshot and ~6k cells/day of history -- years of headroom.

track.py still keeps full 10-minute history for the watchlist in config.json.
The two are complementary: this is breadth, that is depth.

Usage:
    py market.py --discover     # rebuild catalogue.json (~1 min, do occasionally)
    py market.py                # scan every item, refresh snapshot (+ daily history)
    py market.py --dry-run      # scan and print, don't touch the Sheet
    py market.py --limit 50     # scan only the first N items (testing)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import string
import sys
import time
import urllib.parse
from pathlib import Path

import track

CATALOGUE = track.SCRIPT_DIR / "catalogue.json"
MAX_AGE_MIN = 90        # newest scraped sale older than this = upstream frozen
SNAPSHOT_TAB = "All Items"
HISTORY_TAB = "Market History"

SNAPSHOT_COLS = ["item", "price", "low", "high", "fills", "span_min", "updated"]
HISTORY_COLS = ["date", "item", "price", "fills", "span_min"]


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

def discover() -> dict[str, str]:
    """Enumerate every item donut.auction knows about.

    There is no list endpoint and search returns at most 25 matches, so this
    sweeps short substrings until the set stops growing. Enchanted variants are
    skipped -- they are separate listings with their own prices, and folding
    them in would mix "netherite helmet" with a dozen enchanted ones.
    """
    queries = list(string.ascii_lowercase)
    queries += [a + b for a in string.ascii_lowercase for b in "aeiou_"]

    found: dict[str, str] = {}
    for i, q in enumerate(queries, 1):
        try:
            data = track.da_get(f"/items/search?q={urllib.parse.quote(q)}")
        except Exception as exc:              # noqa: BLE001 - a dud query is fine
            print(f"  search {q!r} failed: {exc}", file=sys.stderr)
            continue
        for m in data.get("items") or []:
            item = m["item"]
            if not item.get("enchantments"):
                found[item["id"]] = item["itemName"]
        if i % 40 == 0:
            print(f"  {i}/{len(queries)} queries, {len(found)} items", flush=True)

    CATALOGUE.write_text(json.dumps(found, indent=1, sort_keys=True), encoding="utf-8")
    print(f"Saved {len(found)} items to {CATALOGUE.name}")
    return found


def load_catalogue() -> dict[str, str]:
    if not CATALOGUE.exists():
        print("No catalogue yet -- discovering (about a minute)...")
        return discover()
    return json.loads(CATALOGUE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------

def scan(catalogue: dict[str, str], limit: int = 0) -> list[dict]:
    items = sorted(catalogue.items(), key=lambda kv: kv[1])
    if limit:
        items = items[:limit]

    rows, missing, stale, started = [], 0, 0, time.time()
    for n, (iid, name) in enumerate(items, 1):
        try:
            s = track.fetch_sold(iid)
        except Exception:                     # noqa: BLE001 - one dud item is fine
            s = None
        if s and s["age_min"] > MAX_AGE_MIN:
            # Upstream frozen: endpoints answer but the data stopped moving.
            stale += 1
            s = None
        if not s:
            missing += 1
        else:
            rows.append({
                "item": name,
                "price": round(s["median"], 2),
                "low": round(s["low"], 2),
                "high": round(s["high"], 2),
                "fills": s["count"],
                "span": s["window_min"],
            })
        if n % 200 == 0:
            rate = (time.time() - started) / n
            print(f"  {n}/{len(items)} scanned ({rate:.2f}s each, "
                  f"{(len(items) - n) * rate / 60:.1f} min left)", flush=True)
        time.sleep(0.15)                      # be gentle with a free public API

    print(f"Scanned {len(items)} items in {(time.time() - started) / 60:.1f} min "
          f"({len(rows)} priced, {missing} skipped, of which {stale} stale)")
    if stale and stale > len(items) * 0.5:
        # A broad freeze means donut.auction's scraper is down, not that the
        # market went quiet. Writing a snapshot here would overwrite good data
        # with a frozen one, so bail out and leave the sheet alone.
        raise SystemExit(
            f"Aborting: {stale}/{len(items)} items have stale data. "
            "donut.auction is not ingesting new sales; the sheet was not touched."
        )
    return rows


# --------------------------------------------------------------------------
# sheet writing
# --------------------------------------------------------------------------

def write_snapshot(sheet: track.Sheet, rows: list[dict]) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values = [SNAPSHOT_COLS] + [
        [r["item"], r["price"], r["low"], r["high"], r["fills"], r["span"], stamp]
        for r in rows
    ]
    sheet.ensure_plain_tab(SNAPSHOT_TAB, len(SNAPSHOT_COLS))
    # Rewritten wholesale each scan, so clear first -- otherwise a shorter scan
    # would leave stale rows from a longer one below the new data.
    sheet.clear_tab(SNAPSHOT_TAB)
    sheet.put(SNAPSHOT_TAB, "A1", values)
    sheet.style_header(SNAPSHOT_TAB, len(SNAPSHOT_COLS))


def append_history(sheet: track.Sheet, rows: list[dict]) -> bool:
    """One row per item per day. Returns False if today is already recorded."""
    sheet.ensure_plain_tab(HISTORY_TAB, len(HISTORY_COLS))
    today = dt.date.today().isoformat()

    existing = sheet.get(HISTORY_TAB, "A1:A2")
    if not existing:
        sheet.put(HISTORY_TAB, "A1", [HISTORY_COLS])
        sheet.style_header(HISTORY_TAB, len(HISTORY_COLS))
    elif len(existing) > 1 and str(existing[1][0]).startswith(today):
        return False                          # already logged today

    block = [[today, r["item"], r["price"], r["fills"], r["span"]] for r in rows]
    sheet.insert_rows_at_top(HISTORY_TAB, block)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan every DonutSMP auction item.")
    ap.add_argument("--discover", action="store_true", help="rebuild catalogue.json and exit")
    ap.add_argument("--dry-run", action="store_true", help="scan and print, skip the Sheet")
    ap.add_argument("--limit", type=int, default=0, help="scan only the first N items")
    args = ap.parse_args()

    if args.discover:
        discover()
        return 0

    catalogue = load_catalogue()
    rows = scan(catalogue, args.limit)
    if not rows:
        print("Nothing priced; leaving the sheet alone.", file=sys.stderr)
        return 1

    if args.dry_run:
        for r in sorted(rows, key=lambda r: -r["price"])[:15]:
            print(f"  {r['item']:<28} ${r['price']:>16,.2f}  {r['fills']} fills / {r['span']:.0f}m")
        return 0

    cfg = track.load_config()
    sheet = track.Sheet(cfg)
    write_snapshot(sheet, rows)
    print(f"Snapshot written: {len(rows)} items")
    print("Daily history appended" if append_history(sheet, rows)
          else "Daily history already recorded for today")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
