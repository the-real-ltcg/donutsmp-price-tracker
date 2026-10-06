# DonutSMP Auction Price Tracker

Logs what items **actually sold for** on the DonutSMP auction house into a Google
Sheet, on a schedule, with no API key required.

The emphasis is on *actually sold for*. Price-tracker sites typically show a
smoothed "value" estimate, and those estimates can sit well above real sale
prices — measured on live data, donut.auction's value ran **+19% on gilded
blackstone, +8.6% on netherite ingot, and +42% on sea pickle** versus the median
of real completed sales at the same moment. This tool reads the raw transaction
feed instead: individual fills, with seller, price, stack count and sale time.

> ## ⚠️ Status: upstream is currently dead
>
> DonutSMP disabled their official API on **2026-09-29**. donut.auction scrapes
> from it, so its transaction feed froze at that moment and has not moved since.
> **This tool cannot collect new data until that changes.**
>
> The code still works and is worth keeping: `check_upstream.py` watches for the
> feed reviving, and the staleness guards mean a frozen upstream can never
> silently poison your data. See [Is it still working?](#is-it-still-working).

---

## Contents

- [What it does](#what-it-does)
- [Install](#install)
- [Why the design looks like this](#why-the-design-looks-like-this)
- [Requirements](#requirements)
- [Setup](#setup)
- [Usage](#usage)
- [Scheduling](#scheduling)
- [The whole-market scanner](#the-whole-market-scanner)
- [Is it still working?](#is-it-still-working)
- [Analysing the data](#analysing-the-data)
- [Gotchas learned the hard way](#gotchas-learned-the-hard-way)
- [Limitations](#limitations)
- [Attribution and fair use](#attribution-and-fair-use)
- [License](#license)

---

## What it does

Three scripts, each doing one job.

| Script | Job |
|---|---|
| `track.py` | Watchlist tracker. A handful of items, one tab each, full price history at a tight interval. **Depth.** |
| `market.py` | Whole-market scanner. Every tradeable item (~1030), one row each, refreshed hourly. **Breadth.** |
| `check_upstream.py` | Liveness check. Is the data source actually producing new sales? |

`track.py` writes these columns per reading:

| Column | Meaning |
|---|---|
| `timestamp_local` | When the reading was taken (real datetime, chartable) |
| `timestamp_utc` | Same moment in UTC, as text |
| `item` | Item name |
| `sold_median` | **Median per-unit price** of the recent fills |
| `sold_low` / `sold_high` | Cheapest and dearest per-unit price in the sample |
| `sold_count` | How many fills the sample contains (usually 10) |
| `sold_window_min` | How many minutes of trading those fills span |
| `ask_low` / `ask_median` / `ask_listings` | Current asking prices — blank unless you have an official API key |

Prices are **per unit**: a listing's price divided by its stack count, so a stack
of 64 isn't mistaken for a single item.

Summarised with the **median**, not the mean. The feed returns only ~10 sales, and
on a sample that small one fat-fingered overpay (someone paying 50× for a single
item — it happens constantly) wrecks an average.

`sold_window_min` is the most under-rated column. It tells you how fast an item
trades: 4 minutes for 10 sales means a busy market, 10,000 minutes means nobody is
buying and the price is stale rather than wrong. **Filter on it** before trusting
anything from the long tail.

---

## Why the design looks like this

Choices that look odd but are deliberate.

**Why not `gspread`?** On the machine this was developed on, `requests` could not
reliably complete TLS connections — it hung, then failed with `SSLEOFError`,
against google.com, googleapis.com and the game APIs alike, while stdlib `urllib`
reached all of them fine. Raw TLS handshakes succeeded even using urllib3's own
SSL context, so it wasn't certifi, ALPN, or TLS interception. The cause was never
found. Everything therefore uses stdlib `urllib`, and Google Sheets is called
through its **REST API** with a hand-rolled service-account JWT. If `requests`
works fine for you, swapping in `gspread` is reasonable — but nothing here needs
it, and the only dependency is `cryptography` (to sign the JWT).

**Why a browser User-Agent?** Both game APIs sit behind Cloudflare, which answers
a library User-Agent with `403 error code: 1010` — a browser-integrity block —
before the request ever reaches the application. A browser UA gets through.

**Why one tab per item for the watchlist but one row per item for the market?**
Arithmetic. 1032 items × 144 readings/day × 11 columns = **1.6M cells/day**, and a
Google spreadsheet caps at **10M cells total**. Per-item history for the whole
market fills the sheet in about six days. The snapshot design uses ~11k cells and
lasts indefinitely.

**Why does the market scan run hourly, not every 10 minutes?** The transaction
feed has no bulk endpoint, so a full sweep is one request per item: ~7.5 minutes
and ~1030 requests. At a 10-minute cadence that's 148k requests/day against a free
third-party service, with the job running half the time. Hourly is ~25k/day.

---

## Install

```bash
git clone https://github.com/the-real-ltcg/donutsmp-price-tracker.git
cd donutsmp-price-tracker
```

Or download the ZIP from the repo's green **Code** button.

## Requirements

- **Python 3.10+** (uses `X | Y` type syntax and `zoneinfo`-free date handling)
- `cryptography` — the only third-party package
- A Google account, for the sheet
- Optionally, a DonutSMP API key for asking prices (see below)

```bash
pip install -r requirements.txt
```

On Windows, Python is often `py` rather than `python`:

```bash
py -m pip install -r requirements.txt
```

---

## Setup

### 1. Create a Google Sheet

Make a blank spreadsheet. Copy its ID from the URL — the long string between
`/d/` and `/edit`:

```
https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit
```

### 2. Create a Google service account

A service account lets the script write while you're not at the computer: no
browser login, no token to refresh, no OAuth dance.

1. Go to <https://console.cloud.google.com/> and create a project (any name).
2. **APIs & Services → Library** → enable **Google Sheets API**.
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Name it anything; skip the optional role and access steps; click Done.
4. Click the new service account → **Keys → Add key → Create new key → JSON**.
5. Save the downloaded file as `service_account.json` beside the scripts.

### 3. Share the sheet with the service account

Open `service_account.json` and copy the `client_email` value — it looks like
`something@your-project.iam.gserviceaccount.com`. In your Google Sheet press
**Share**, paste that address, give it **Editor**, and send.

**Skipping this step is the single most common failure.** A service account is a
separate Google account; without the share it can only return 403.

### 4. Configure

```bash
cp config.example.json config.json
```

Set `spreadsheet_id` in `config.json`. Everything else has a working default.

| Key | Default | Meaning |
|---|---|---|
| `spreadsheet_id` | — | **Required.** Your sheet's ID |
| `service_account_file` | `service_account.json` | Path to the key, absolute or relative |
| `interval_seconds` | 600 | Poll interval for `--loop` mode |
| `pages_per_item` | 5 | Listing pages to scan for asking prices |
| `max_staleness_min` | 90 | Refuse to record if the newest sale is older than this |
| `items` | 5 items | What to track |

Each item needs only `name`. `search` and `worksheet` default to a title-cased
version of it, and `donut_id` is resolved and cached automatically on first run:

```json
{ "name": "sea_pickle", "search": "Sea Pickle", "worksheet": "Sea Pickle", "pages": 12 }
```

`name` is matched against each listing's item id, so a search for "Netherite
Ingot" can't quietly record netherite blocks. `pages` overrides `pages_per_item`
for one item — useful for cheap, high-volume items, because the listing board
sorts by **total** price, so bulk stacks hide far below single-item listings even
when their unit price is better.

Any config value can be overridden by environment variable using the prefix
`DONUT_`, e.g. `DONUT_SPREADSHEET_ID`, `DONUT_INTERVAL_SECONDS`. Handy for keeping
your sheet ID out of the file.

### 5. Optional — asking prices

The `ask_*` columns need a key from DonutSMP's official API. Run this **in the
Minecraft chat box** while connected to the server (not in a terminal):

```
/api
```

If that reports "not a command", try `/api key create`. Paste the result into
`api_key.txt` beside the scripts, or set `DONUT_API_KEY`.

Without a key these columns stay blank and everything else works normally. Having
both is genuinely useful, though: the gap between what people *ask* and what
things *sell for* tells you how much room there is to undercut.

Auth is `Authorization: Bearer {KEY}` per the official spec. The client tries that
first, falls back to a bare key, and remembers whichever the server accepted. The
documented rate limit is 250 requests/minute per key.

---

## Usage

```bash
py track.py
```

One reading of every tracked item, appended, then exits. **This is the mode to use
with a scheduler.**

```bash
py track.py --loop
```

Polls every `interval_seconds` until you stop it. Only runs while the terminal is
open.

```bash
py track.py --dry-run
```

Prints readings and writes the local CSV backup, but never touches the Sheet. Use
this to confirm the data half works before dealing with Google credentials.

```bash
py track.py --sold "sea pickle"
```

Prints the most recent real sales for anything — tracked or not — with per-unit
prices and sellers, then the median. Needs no key and no sheet.

```bash
py track.py --reset-tabs
```

Clears the tracked tabs and rewrites headers. **Destructive.** Needed if you change
the column layout; the script otherwise refuses to append under a mismatched header
rather than interleaving two different measurements in one column.

Other flags: `--interval N`, `--pages N`, `--worksheet NAME`.

Every reading is also appended to `readings_backup.csv` locally, before the Sheets
write, so a Google outage never loses data.

---

## Scheduling

Run the one-shot mode from your OS scheduler.

### Windows

```powershell
$exe = "C:\Path\To\pythonw.exe"       # pythonw = no console window
$dir = "C:\Path\To\donutsmp-price-tracker"
$action  = New-ScheduledTaskAction -Execute $exe -Argument "`"$dir\track.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "Donut Price Tracker" -Action $action -Trigger $trigger

# These are OFF by default and are the reason most people lose overnight data:
$t = Get-ScheduledTask -TaskName "Donut Price Tracker"
$t.Settings.WakeToRun = $true                   # wake a sleeping PC to run
$t.Settings.StartWhenAvailable = $true          # catch up a missed run
$t.Settings.DisallowStartIfOnBatteries = $false
Set-ScheduledTask -InputObject $t
```

**`WakeToRun` and `StartWhenAvailable` matter.** A desktop that sleeps after 30
minutes idle will silently drop every overnight reading, and `schtasks /create`
does not set these. Wake timers also have to be permitted by your power scheme
(`powercfg /query SCHEME_CURRENT SUB_SLEEP RTCWAKE`) and can be vetoed by BIOS
("Wake on RTC"). None of this helps if the machine is fully powered off.

### Linux / macOS

```cron
*/10 * * * * cd /path/to/donutsmp-price-tracker && /usr/bin/python3 track.py >> tracker.log 2>&1
```

---

## The whole-market scanner

```bash
py market.py --discover   # build catalogue.json (~1 min); a prebuilt one is included
py market.py              # scan every item, refresh the snapshot
py market.py --dry-run    # scan and print the 15 priciest, don't write
py market.py --limit 50   # scan only the first N items (testing)
```

Writes two tabs:

- **All Items** — one row per item, rewritten each scan with the latest price,
  low/high, fill count and trade span.
- **Market History** — one row per item per **day**, newest first. This is what you
  chart long-term trends from.

There is no list endpoint and search returns at most 25 matches, so `--discover`
sweeps short substrings until the catalogue stops growing. A prebuilt
`catalogue.json` (~1030 items) ships with this repo, so you only need `--discover`
if new items are added to the game.

Enchanted variants are deliberately excluded — they're separate listings with their
own prices, and folding them in would mix plain "netherite helmet" with a dozen
enchanted ones.

---

## Is it still working?

```bash
py check_upstream.py
```

Exit codes: `0` live, `1` frozen, `2` unreachable. Appends one line per run to
`upstream_status.log` and prints loudly when the feed revives.

Schedule it daily and it costs essentially nothing:

```powershell
# Windows
$action  = New-ScheduledTaskAction -Execute $exe -Argument "`"$dir\check_upstream.py`" --quiet" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 9am
Register-ScheduledTask -TaskName "Donut Upstream Check" -Action $action -Trigger $trigger
```

**Why this script exists, and it's the most important thing in this repo:**

When the upstream scraper died, its endpoints kept returning **HTTP 200 with the
same frozen snapshot**. Nothing errored. Exit codes stayed 0. Fresh rows kept
landing in the sheet — each containing a four-day-old price. The tracker logged
**~510 identical readings per item over four days** and looked perfectly healthy.

The trap was that the obvious health signal lied. `sold_window_min` — the span
*between* the sampled fills — stayed small and healthy-looking (14 minutes),
because those 10 sales genuinely were 14 minutes apart. Four days ago.

The fix is to check the **age of the newest fill**, not whether the request
succeeded or how tight the sample is:

- `fetch_sold()` returns `age_min`
- `track.py` skips any item whose newest sale is older than `max_staleness_min`
  and prints a loud warning, rather than recording a frozen price
- `market.py` **aborts entirely** if more than half the items are stale, so a dead
  upstream can never overwrite a good snapshot with a frozen one

If you build anything on a third-party feed, steal this idea. "The request
succeeded" and "the data is current" are different claims.

---

## Analysing the data

`best-times.gs` is a Google Apps Script that computes **average price by hour of
day** per item. Paste it into **Extensions → Apps Script** and run
`calculateBestTimes()`.

What it does, and why it's shaped carefully:

- **Detrends** each reading against a centred ±12h rolling average, so a steadily
  falling item doesn't simply report "cheapest = whenever it was lowest".
- **Aggregates per day-hour first**, so each day counts once. Several readings
  inside the same hour of the same day are *not* independent observations —
  especially on slow items, where consecutive polls re-read the same sales.
- **Refuses to claim a winner** it can't support, reporting `NOT ENOUGH DATA` or
  `NOT SIGNIFICANT` instead of a number. Expect `NOT ENOUGH DATA` for about a week.
- Single pass, not O(n²), so it won't hit the Apps Script 6-minute limit.

That last point is the one that matters. **The naive version of this analysis is
actively misleading.** Searching for "the most profitable buy→sell pair in the
history" always returns a positive result — with any volatility, some pair wins —
reports one historical moment rather than a recurring time, and produces
spectacular ROI figures that mean nothing. On real data that approach reported a
**1624% ROI** on sea pickle. It's hindsight, not a forecast.

### What the data actually showed

From two weeks of real fills across 16 items, tested with a permutation test that
shuffles hour labels **within each day** (the correct null, since it preserves
day-to-day drift):

**13 of 16 items showed no time-of-day pattern at all**, including every expensive
one — gilded blackstone (p=0.20), ancient debris (p=0.14), netherite ingot
(p=0.59), netherite block (p=0.21), elytra (p=0.79).

Three were real: sea pickle (115% swing, p=0.004), totem of undying (48%,
p<0.001), golden apple (35%, p=0.001). All cheap, high-churn items, cheapest
overnight and dearest from midday to evening — plausibly tracking when players are
online and actively consuming them, while stored wealth like netherite trades
whenever its owner feels like it.

Two lessons worth carrying: **the expensive items, where timing would actually be
worth money, are exactly the ones with no pattern**; and an earlier version of this
analysis built on the smoothed "value" field produced a clean, confident, *wrong*
answer with the right shape and a 10× understated size. Smoothing doesn't just
shift prices — it manufactures tidy daily rhythms that aren't in the trades.

---

## Gotchas learned the hard way

**Google Sheets eats date formats.** Clearing a tab drops its number format, so
datetime serials render as `46279.889`. The header writer pins column A to
`DATE_TIME` explicitly rather than hoping Sheets infers it.

**`insertDataOption=INSERT_ROWS` inherits formatting from the row above.** On a
fresh tab that's the header, so the first reading inherits the header's format and
shows a raw serial anyway. Appends deliberately omit it and write into the
pre-formatted grid.

**The batch price endpoint returns results sorted by item name, not in request
order.** Key results by item id, never by position.

**Items differ enormously in trade speed.** Gilded blackstone accumulates 10 fills
in ~4 minutes; heavy core takes ~114. On the slow ones, consecutive 10-minute
readings re-read many of the same sales, so their rows are heavily autocorrelated.
Don't treat them as independent samples.

**Exit code 0 does not mean it worked.** Per-item failures are caught so one bad
item can't kill a cycle — which also means the script can exit cleanly having
written nothing. Check the data, not the exit code.

---

## Limitations

- **The transaction feed caps at the 10 most recent sales per item.** This tracks
  *price* well, but it is not a complete record of *volume*. On busy items those 10
  sales span only a few minutes, so a 10-minute poll samples a fraction of trades
  and misses the rest. Fine for prices; useless for "how many sold today".
- **Asking prices need an API key** most players cannot obtain.
- **Listing pages sort by total price, not unit price**, so the cheapest *per unit*
  offer can sit well below page 1. `pages_per_item` controls how deep to look.
- **No volume-weighted pricing.** `sold_median` weights a 1-item sale the same as a
  64-stack.
- **Readings only happen while your machine is on.** Gaps are visible in the data;
  account for them rather than averaging over them.
- **This depends on a third party's scraper**, which has already died once.

---

## Attribution and fair use

Price data comes from [donut.auction](https://donut.auction), an independent
price-tracker site that is **not affiliated with DonutSMP**. They ask that projects
using their data provide visible attribution and link back — please keep that in
place if you fork this.

This tool reads publicly available endpoints, rate-limits itself (a delay between
every request), and does not require or bypass authentication. It does send a
browser User-Agent to get past a Cloudflare browser-integrity check; that is worth
knowing about, and worth being thoughtful about. Don't raise the polling rate
without a reason — the scanner is deliberately hourly rather than every 10 minutes
for exactly this reason.

Nothing here automates gameplay, modifies a game client, or touches a player
account.

---

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 [the-real-ltcg](https://github.com/the-real-ltcg).

Do what you like with it: use it, fork it, sell it. If you fork it, please keep the
[donut.auction](https://donut.auction) attribution in place — that's their request,
not a licence condition.

---

## Files

| File | |
|---|---|
| `track.py` | Watchlist tracker |
| `market.py` | Whole-market scanner |
| `check_upstream.py` | Upstream liveness check |
| `best-times.gs` | Apps Script: price by hour of day |
| `catalogue.json` | Prebuilt item catalogue (~1030 items) |
| `config.example.json` | Copy to `config.json` |
| `requirements.txt` | `cryptography` |

**Never commit** `service_account.json` (a private key), `api_key.txt`, or your own
`config.json` (contains your spreadsheet ID). The included `.gitignore` covers all
three.
