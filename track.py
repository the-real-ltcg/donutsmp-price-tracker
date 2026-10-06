#!/usr/bin/env python3
"""
DonutSMP auction price tracker -> Google Sheets.

Records what items ACTUALLY SOLD FOR on the DonutSMP auction house, and appends a
timestamped row per item to a Google Sheet -- one worksheet tab per item.

Two sources, both optional-by-availability:

  sold_*  Completed sales. Read from donut.auction's transaction feed, which is
          raw scraped fills (seller, price, count, time sold) -- NOT their
          computed "value", which runs well above what things really sell for
          (+19% on gilded blackstone, +42% on sea pickle when last measured).
          Needs no credentials, so this always works.

  ask_*   Current asking prices from the official DonutSMP API. Needs an API key
          generated in game with /api. If no key is configured these columns are
          simply left blank -- the sold_* columns still fill in.

Networking note: everything here goes through stdlib urllib on purpose. On the
machine this was developed on, the `requests` library could not reliably complete
TLS connections -- it hung and then failed with SSLEOFError against google.com,
googleapis.com and the game APIs alike, while stdlib urllib reached all of them
fine. The cause was never found (not certifi, not ALPN, not TLS interception).
That is why this talks to the Google Sheets REST API directly instead of using
gspread, which depends on requests. If requests works fine for you, gspread would
be a reasonable swap -- but nothing here needs it.

Usage:
    py track.py                     # one reading of every item, append, exit
    py track.py --loop              # poll forever at config interval (default 30 min)
    py track.py --dry-run           # print readings, don't touch the Sheet
    py track.py --sold "sea pickle" # show recent real sales for anything
    py track.py --reset-tabs        # rewrite tab headers (wipes old rows)
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import json
import os
import random
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DA_API = "https://api.donut.auction/v2"
AH_API = "https://api.donutsmp.net/v1"
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"

BROWSER_HEADERS = {
    # Both game APIs sit behind Cloudflare, which answers a library User-Agent
    # with a 403 ("error code: 1010" on DonutSMP) before the request ever reaches
    # the application. A browser UA gets through.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
KEY_PATH = SCRIPT_DIR / "api_key.txt"
BACKUP_CSV = SCRIPT_DIR / "readings_backup.csv"

COLUMNS = [
    "timestamp_local",
    "timestamp_utc",
    "item",
    "sold_median",
    "sold_low",
    "sold_high",
    "sold_count",
    "sold_window_min",
    "ask_low",
    "ask_median",
    "ask_listings",
]


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

DEFAULTS = {
    "items": [
        {"name": "gilded_blackstone", "search": "Gilded Blackstone",
         "worksheet": "Gilded Blackstone"},
        {"name": "ancient_debris", "search": "Ancient Debris",
         "worksheet": "Ancient Debris"},
        {"name": "netherite_ingot", "search": "Netherite Ingot",
         "worksheet": "Netherite Ingot"},
        {"name": "sea_pickle", "search": "Sea Pickle",
         "worksheet": "Sea Pickle", "pages": 12},
    ],
    "spreadsheet_id": "",
    "service_account_file": "service_account.json",
    "pages_per_item": 5,
    "interval_seconds": 1800,
    # Skip recording when the newest scraped sale is older than this.
    "max_staleness_min": 90,
}


def tab_name(item_name: str) -> str:
    return item_name.replace("_", " ").title()


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))       # deep copy
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        stored.pop("item", None)
        stored.pop("item_id", None)
        cfg.update(stored)

    for key in ("spreadsheet_id", "service_account_file", "interval_seconds",
                "pages_per_item", "max_staleness_min"):
        env = os.environ.get("DONUT_" + key.upper())
        if env:
            cfg[key] = int(env) if isinstance(cfg[key], int) else env

    for entry in cfg["items"]:
        entry.setdefault("search", tab_name(entry["name"]))
        entry.setdefault("worksheet", tab_name(entry["name"]))
        entry.setdefault("donut_id", "")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def load_api_key() -> str:
    """Optional. Returns '' when no key is configured."""
    key = os.environ.get("DONUT_API_KEY", "").strip()
    if not key and KEY_PATH.exists():
        key = KEY_PATH.read_text(encoding="utf-8").strip()
    return key


# --------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------

RETRY_CODES = (429, 500, 502, 503, 504)


class HttpError(Exception):
    def __init__(self, code: int, body: str):
        super().__init__(f"HTTP {code}: {body[:300]}")
        self.code, self.body = code, body


def http_json(url: str, *, method: str = "GET", headers: dict | None = None,
              data: bytes | None = None, timeout: int = 30, attempts: int = 4):
    """Request JSON with exponential backoff. Raises HttpError / RuntimeError."""
    last = None
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code not in RETRY_CODES:
                raise HttpError(exc.code, body) from None
            last = f"HTTP {exc.code}: {body[:200]}"
        except Exception as exc:              # noqa: BLE001 - retry anything transient
            last = f"{type(exc).__name__}: {exc}"
        if i < attempts - 1:
            delay = 2 ** i + random.uniform(0, 1)
            print(f"  retry {i + 1}/{attempts - 1} in {delay:.1f}s ({last})", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError(f"{method} {url.split('?')[0]} failed: {last}")


# --------------------------------------------------------------------------
# completed sales (donut.auction transaction feed -- no credentials needed)
# --------------------------------------------------------------------------

def da_get(path: str):
    return http_json(DA_API + path, headers=BROWSER_HEADERS, timeout=25)


def resolve_donut_id(name: str) -> str:
    """Look up donut.auction's internal id for an item name."""
    data = da_get(f"/items/search?q={urllib.parse.quote(name)}")
    matches = data.get("items") or []
    for m in matches:
        item = m["item"]
        if item["itemName"].lower() == name.lower() and not item.get("enchantments"):
            return item["id"]
    if matches:
        return matches[0]["item"]["id"]
    raise RuntimeError(f"donut.auction knows no item called {name!r}")


def fetch_sold(donut_id: str) -> dict | None:
    """Summarise the most recent completed sales. None if the feed is empty."""
    data = da_get(f"/auctions/items/{donut_id}/transactions")
    tx = data.get("transactions") or []
    if not tx:
        return None

    units, stamps = [], []
    for t in tx:
        count = t.get("itemCount") or 1
        price = t.get("price")
        if price is None or count <= 0:
            continue
        units.append(float(price) / count)
        if t.get("timeSold"):
            stamps.append(t["timeSold"])
    if not units:
        return None

    window = 0.0
    if len(stamps) > 1:
        parsed = sorted(dt.datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
        window = (parsed[-1] - parsed[0]).total_seconds() / 60

    # How old the NEWEST fill is. This is the staleness check that was missing:
    # when donut.auction's scraper died on 2026-09-29 its endpoints kept
    # returning HTTP 200 with the same frozen snapshot, and the tracker recorded
    # ~510 identical readings per item over four days without a single error.
    # window_min did NOT catch it -- that is the span BETWEEN fills, which stays
    # small and healthy-looking in a frozen snapshot.
    age = float("inf")
    if stamps:
        newest = max(dt.datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps)
        age = (dt.datetime.now(dt.timezone.utc) - newest).total_seconds() / 60

    return {
        # Median, not mean: a single fat-fingered listing (someone paying 50x for
        # one pickle) would drag an average badly on this small a sample.
        "median": statistics.median(units),
        "low": min(units),
        "high": max(units),
        "count": len(units),
        "window_min": round(window, 1),
        "age_min": round(age, 1),
    }


# --------------------------------------------------------------------------
# current asks (official DonutSMP API -- needs an in-game /api key)
# --------------------------------------------------------------------------

class AuctionHouse:
    """Live Auction House reader.

    The API docs specify `Authorization: Bearer {KEY}`, so that is tried first;
    the bare-key form is kept as a fallback in case the server ever accepts only
    that. Whichever works is remembered, so the fallback costs one 401 at most.

    The server allows 250 requests per minute per key.
    """

    def __init__(self, key: str):
        self._key = key
        self._scheme: str | None = None
        self.disabled = not key
        self.reason = "" if key else "no API key configured"

    def _headers(self, scheme: str) -> dict:
        h = dict(BROWSER_HEADERS)
        h["Authorization"] = f"Bearer {self._key}" if scheme == "bearer" else self._key
        h["Content-Type"] = "application/json"
        return h

    def _request(self, path: str, body: dict | None):
        data = json.dumps(body).encode() if body is not None else None
        schemes = [self._scheme] if self._scheme else ["bearer", "bare"]
        last = None
        for scheme in schemes:
            try:
                out = http_json(AH_API + path, method="GET",
                                headers=self._headers(scheme), data=data)
                self._scheme = scheme
                return out
            except HttpError as exc:
                last = exc
                if exc.code != 401:
                    raise
        # Don't kill the run -- the sold_* columns are the important half.
        self.disabled = True
        self.reason = "API key rejected (401)"
        raise RuntimeError(self.reason) from last

    def listings(self, search: str, page: int) -> list[dict]:
        out = self._request(f"/auction/list/{page}",
                            {"search": search, "sort": "lowest_price"})
        return out.get("result") or []


def normalise(name: str) -> str:
    """'minecraft:gilded_blackstone' / 'Gilded Blackstone' -> 'gilded_blackstone'."""
    return name.split(":")[-1].strip().lower().replace(" ", "_")


def matches(listing: dict, want: str) -> bool:
    item = listing.get("item") or {}
    if normalise(item.get("id") or "") == want:
        return True
    return normalise(item.get("display_name") or "") == want


def fetch_asks(ah: AuctionHouse, entry: dict, pages: int) -> dict | None:
    """Walk the cheapest pages and summarise live asks. None if unavailable."""
    if ah.disabled:
        return None
    want = normalise(entry["name"])
    prices: list[float] = []
    try:
        for page in range(1, pages + 1):
            batch = ah.listings(entry["search"], page)
            if not batch:
                break
            for lst in batch:
                if not matches(lst, want):
                    continue
                item = lst.get("item") or {}
                count = item.get("count") or 1
                price = lst.get("price")
                if price is not None and count > 0:
                    prices.append(float(price) / count)
            time.sleep(0.4)               # be gentle; this is someone's game server
    except RuntimeError:
        return None
    if not prices:
        return None
    return {"low": min(prices), "median": statistics.median(prices), "listings": len(prices)}


# --------------------------------------------------------------------------
# Google Sheets (REST, via stdlib urllib -- see module docstring)
# --------------------------------------------------------------------------

def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _a1(title: str, cell: str) -> str:
    """A1 notation for a tab. Always quoted, so spaces in tab names are safe."""
    return f"'{title.replace(chr(39), chr(39) * 2)}'!{cell}"


class Sheet:
    """Minimal Google Sheets client: service-account JWT auth + append."""

    def __init__(self, cfg: dict, reset: bool = False):
        if not cfg["spreadsheet_id"]:
            raise SystemExit(
                "No spreadsheet_id set. Put your sheet's ID in config.json "
                "(it's the long string in the sheet URL between /d/ and /edit)."
            )
        key_path = Path(cfg["service_account_file"])
        if not key_path.is_absolute():
            key_path = SCRIPT_DIR / key_path
        if not key_path.exists():
            raise SystemExit(f"Service account key not found at {key_path}. See README.md.")

        self._sa = json.loads(key_path.read_text(encoding="utf-8"))
        self._sid = cfg["spreadsheet_id"]
        self._reset = reset
        self._token = ""
        self._token_expiry = 0.0
        self._ready: set[str] = set()
        self._ids: dict[str, int] = {}

    # -- auth ------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        sa, now = self._sa, int(time.time())
        signing_input = b".".join([
            _b64url(json.dumps({"alg": "RS256", "typ": "JWT",
                                "kid": sa["private_key_id"]}).encode()),
            _b64url(json.dumps({"iss": sa["client_email"], "scope": SHEETS_SCOPE,
                                "aud": sa["token_uri"], "iat": now,
                                "exp": now + 3600}).encode()),
        ])
        key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())

        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": (signing_input + b"." + _b64url(signature)).decode(),
        }).encode()
        tok = http_json(sa["token_uri"], method="POST", data=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})

        self._token = tok["access_token"]
        self._token_expiry = time.time() + int(tok.get("expires_in", 3600))
        return self._token

    def _call(self, path: str, *, method: str = "GET", payload: dict | None = None):
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        return http_json(f"{SHEETS_API}/{self._sid}{path}", method=method,
                         headers=headers, data=data)

    # -- tabs ------------------------------------------------------------

    def _write_header(self, title: str, sheet_id: int) -> None:
        self._call(f"/values/{urllib.parse.quote(_a1(title, 'A1'))}?valueInputOption=RAW",
                   method="PUT", payload={"values": [COLUMNS]})
        self._call(":batchUpdate", method="POST", payload={"requests": [
            {"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }},
            {"updateSheetProperties": {
                "properties": {"sheetId": sheet_id,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }},
            # Column A holds real datetime serials. Clearing a tab drops its
            # number format, and an unformatted serial renders as "46279.889",
            # so pin the format explicitly rather than relying on Sheets to
            # infer it from the first value written.
            {"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"numberFormat": {
                    "type": "DATE_TIME", "pattern": "yyyy-mm-dd hh:mm:ss"}}},
                "fields": "userEnteredFormat.numberFormat",
            }},
        ]})

    def _ensure_tab(self, title: str) -> None:
        if title in self._ready:
            return

        meta = self._call("?fields=sheets.properties(title,sheetId)")
        tabs = {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in meta.get("sheets", [])}

        if title not in tabs:
            added = self._call(":batchUpdate", method="POST", payload={"requests": [
                {"addSheet": {"properties": {
                    "title": title,
                    "gridProperties": {"rowCount": 1000, "columnCount": len(COLUMNS),
                                       "frozenRowCount": 1},
                }}}
            ]})
            tabs[title] = added["replies"][0]["addSheet"]["properties"]["sheetId"]

        end = chr(ord("A") + len(COLUMNS) - 1)
        rng = urllib.parse.quote(_a1(title, f"A1:{end}1"))
        header = (self._call(f"/values/{rng}").get("values") or [[]])[0]

        if self._reset and header:
            self._call(f"/values/{urllib.parse.quote(_a1(title, 'A:Z'))}:clear",
                       method="POST", payload={})
            header = []

        if not header:
            self._write_header(title, tabs[title])
        elif header != COLUMNS:
            raise SystemExit(
                f"Tab {title!r} has an old column layout:\n"
                f"  found:    {header}\n"
                f"  expected: {COLUMNS}\n"
                "Those are different measurements and must not share a column.\n"
                "Re-run with --reset-tabs to clear these tabs and start the new\n"
                "series, or rename them in Sheets to keep the old data."
            )
        self._ready.add(title)

    def _sheet_id(self, title: str) -> int:
        if title not in self._ids:
            meta = self._call("?fields=sheets.properties(title,sheetId)")
            self._ids = {s["properties"]["title"]: s["properties"]["sheetId"]
                         for s in meta.get("sheets", [])}
        return self._ids[title]

    # -- generic tab helpers (used by market.py) -------------------------

    def ensure_plain_tab(self, title: str, cols: int, rows: int = 2000) -> int:
        """A tab with no enforced column schema, created if missing."""
        try:
            return self._sheet_id(title)
        except KeyError:
            pass
        added = self._call(":batchUpdate", method="POST", payload={"requests": [
            {"addSheet": {"properties": {
                "title": title,
                "gridProperties": {"rowCount": rows, "columnCount": cols,
                                   "frozenRowCount": 1},
            }}}
        ]})
        sid = added["replies"][0]["addSheet"]["properties"]["sheetId"]
        self._ids[title] = sid
        return sid

    def get(self, title: str, rng: str) -> list[list]:
        return self._call(
            f"/values/{urllib.parse.quote(_a1(title, rng))}").get("values", [])

    def put(self, title: str, cell: str, values: list[list]) -> None:
        self._call(
            f"/values/{urllib.parse.quote(_a1(title, cell))}?valueInputOption=USER_ENTERED",
            method="PUT", payload={"values": values})

    def clear_tab(self, title: str) -> None:
        self._call(f"/values/{urllib.parse.quote(_a1(title, 'A:Z'))}:clear",
                   method="POST", payload={})

    def style_header(self, title: str, cols: int) -> None:
        sid = self._sheet_id(title)
        self._call(":batchUpdate", method="POST", payload={"requests": [
            {"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold"}},
            {"updateSheetProperties": {
                "properties": {"sheetId": sid,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
        ]})

    def insert_rows_at_top(self, title: str, block: list[list]) -> None:
        """Insert a block directly under the header, newest-first."""
        if not block:
            return
        self._call(":batchUpdate", method="POST", payload={"requests": [
            {"insertDimension": {
                "range": {"sheetId": self._sheet_id(title), "dimension": "ROWS",
                          "startIndex": 1, "endIndex": 1 + len(block)},
                "inheritFromBefore": False}}
        ]})
        self.put(title, "A2", block)

    def reset_tabs(self, titles: list[str]) -> None:
        """Clear each tab and rewrite the current header."""
        for title in titles:
            self._ensure_tab(title)
            print(f"  cleared and re-headed {title!r}")

    def append(self, title: str, row: list) -> None:
        self._ensure_tab(title)
        # USER_ENTERED so Sheets stores the timestamp as a real datetime and the
        # prices as real numbers -- otherwise charting the column is painful.
        # Newest first: open a blank row directly under the header and write
        # there, rather than appending at the bottom. inheritFromBefore=False
        # takes formatting from the row below (a data row) instead of the
        # header, which keeps the column A date format off the bold header.
        self._call(":batchUpdate", method="POST", payload={"requests": [
            {"insertDimension": {
                "range": {"sheetId": self._sheet_id(title), "dimension": "ROWS",
                          "startIndex": 1, "endIndex": 2},
                "inheritFromBefore": False,
            }}
        ]})
        self._call(
            f"/values/{urllib.parse.quote(_a1(title, 'A2'))}?valueInputOption=USER_ENTERED",
            method="PUT", payload={"values": [row]},
        )


def write_backup(row: list) -> None:
    """Always keep a local copy, so a Sheets outage never loses a reading."""
    new = not BACKUP_CSV.exists()
    with BACKUP_CSV.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLUMNS)
        w.writerow(row)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_row(item: str, sold: dict, asks: dict | None) -> list:
    now, utc = dt.datetime.now(), dt.datetime.now(dt.timezone.utc)
    return [
        now.strftime("%Y-%m-%d %H:%M:%S"),
        utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        item,
        round(sold["median"], 2),
        round(sold["low"], 2),
        round(sold["high"], 2),
        sold["count"],
        sold["window_min"],
        round(asks["low"], 2) if asks else "",
        round(asks["median"], 2) if asks else "",
        asks["listings"] if asks else "",
    ]


def take_readings(cfg: dict, ah: AuctionHouse, sheet: Sheet | None,
                  default_pages: int) -> None:
    dirty = False
    stale = 0
    max_age = float(cfg.get("max_staleness_min") or 90)
    for n, entry in enumerate(cfg["items"]):
        if n:
            time.sleep(0.5)                   # don't machine-gun a free public API
        if not entry.get("donut_id"):
            try:
                entry["donut_id"] = resolve_donut_id(entry["name"])
                dirty = True
            except Exception as exc:          # noqa: BLE001
                print(f"  {entry['name']}: cannot resolve id ({exc})", file=sys.stderr)
                continue

        try:
            sold = fetch_sold(entry["donut_id"])
        except Exception as exc:              # noqa: BLE001
            print(f"  {entry['name']}: sales lookup failed ({exc})", file=sys.stderr)
            continue
        if not sold:
            print(f"  {entry['name']}: no recent sales in the feed", file=sys.stderr)
            continue

        # Refuse to record a frozen upstream rather than filling the sheet with
        # thousands of identical rows that look like real history.
        if sold["age_min"] > max_age:
            stale += 1
            print(f"  {entry['name']}: STALE -- newest sale is "
                  f"{sold['age_min'] / 60:.1f}h old, not recording", file=sys.stderr)
            continue

        asks = fetch_asks(ah, entry, int(entry.get("pages") or default_pages))

        row = build_row(entry["name"], sold, asks)
        write_backup(row)
        ask_txt = (f"   ask low ${asks['low']:>12,.2f}" if asks else "")
        age_txt = f"  [{sold['age_min']:.0f}m old]" if sold["age_min"] > 20 else ""
        print(f"{row[0]}  {entry['name']:<20} sold ${sold['median']:>13,.2f}"
              f"  (${sold['low']:,.2f}-${sold['high']:,.2f}, {sold['count']} fills"
              f" / {sold['window_min']:.0f}m){ask_txt}{age_txt}")

        if sheet is not None:
            try:
                sheet.append(entry["worksheet"], row)
            except SystemExit:
                raise
            except Exception as exc:          # noqa: BLE001
                print(f"  sheet append failed for {entry['name']}: {exc}", file=sys.stderr)

    if stale:
        print(f"\n  !! {stale} item(s) skipped: donut.auction's feed is not updating.\n"
              "  !! Its endpoints still answer, but the newest sale is hours old --\n"
              "  !! nothing was recorded rather than logging a frozen price.",
              file=sys.stderr)

    if dirty:
        save_config(cfg)


def main() -> int:
    ap = argparse.ArgumentParser(description="Track DonutSMP auction prices in Google Sheets.")
    ap.add_argument("--sold", metavar="ITEM", help="show recent real sales for ITEM and exit")
    ap.add_argument("--loop", action="store_true", help="keep polling instead of exiting")
    ap.add_argument("--interval", type=int, help="seconds between polls when looping")
    ap.add_argument("--pages", type=int, help="AH pages to scan per item (asks only)")
    ap.add_argument("--dry-run", action="store_true", help="print readings, skip Google Sheets")
    ap.add_argument("--reset-tabs", action="store_true",
                    help="clear tracked tabs and rewrite headers (wipes old rows)")
    args = ap.parse_args()

    cfg = load_config()

    if args.sold:
        iid = resolve_donut_id(args.sold.strip().lower().replace(" ", "_"))
        data = da_get(f"/auctions/items/{iid}/transactions")
        tx = data.get("transactions") or []
        if not tx:
            print(f"No recent sales for {args.sold!r}.")
            return 0
        print(f"{'sold at':<26}{'count':>7}{'price':>16}{'unit price':>16}   seller")
        for t in tx:
            count = t.get("itemCount") or 1
            price = float(t.get("price") or 0)
            seller = (t.get("seller") or {}).get("name", "?")
            print(f"{t.get('timeSold','?')[:25]:<26}{count:>7}{price:>16,.2f}"
                  f"{price / count:>16,.2f}   {seller}")
        s = fetch_sold(iid)
        print(f"\nmedian unit price: ${s['median']:,.2f}   "
              f"range ${s['low']:,.2f}-${s['high']:,.2f} over {s['window_min']:.0f} min")
        return 0

    # Clearing tabs is a Sheets-only job -- don't require anything else for it.
    if args.reset_tabs:
        Sheet(cfg, reset=True).reset_tabs([e["worksheet"] for e in cfg["items"]])
        print("Done. Run without --reset-tabs to start recording.")
        return 0

    if args.interval:
        cfg["interval_seconds"] = args.interval
    if not cfg["items"]:
        raise SystemExit("No items configured. Add some to 'items' in config.json.")

    ah = AuctionHouse(load_api_key())
    if ah.disabled:
        print(f"Asking prices skipped: {ah.reason}. Recording completed sales only.")
    pages = args.pages or int(cfg["pages_per_item"])
    sheet = None if args.dry_run else Sheet(cfg)

    if not args.loop:
        take_readings(cfg, ah, sheet, pages)
        return 0

    interval = max(60, int(cfg["interval_seconds"]))
    print(f"Tracking {len(cfg['items'])} item(s) every {interval // 60} min. Ctrl+C to stop.")
    while True:
        try:
            take_readings(cfg, ah, sheet, pages)
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except Exception as exc:              # noqa: BLE001 - a bad poll shouldn't kill the loop
            print(f"  reading failed: {exc}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
