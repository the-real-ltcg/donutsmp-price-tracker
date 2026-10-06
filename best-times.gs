/**
 * BEST TIMES — average price by hour of day, per item.
 *
 * What this does differently from the obvious approach: it does NOT search for
 * the most profitable past buy/sell pair. That search always returns a positive
 * result (with any volatility some pair wins), reports one historical moment
 * rather than a recurring time, and has no predictive value.
 *
 * Instead, for each reading it divides the price by the average of everything
 * within +/-12 hours of it, then averages those ratios by local hour across all
 * days. Detrending that way stops a steadily falling item from simply reporting
 * "cheapest = whenever it was lowest".
 *
 * It reports NOT ENOUGH DATA or NOT SIGNIFICANT rather than inventing a winner.
 * Expect NOT ENOUGH DATA until roughly a week of readings have accumulated.
 */

// Below this many DAYS contributing to an hour bucket, no claim is made.
// Days, not readings: several readings inside the same hour of the same day are
// not independent -- on slow-moving items they re-read many of the same sales --
// so they are averaged into a single observation for that day.
var MIN_DAYS_PER_HOUR = 5;
// The hourly swing must exceed this multiple of its own error bar to count.
var SIGNIFICANCE_T = 2.0;

function calculateBestTimes() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  var out = ss.getSheetByName("BEST TIMES");
  if (!out) {
    out = ss.insertSheet("BEST TIMES");
  } else {
    out.clear();
    out.setConditionalFormatRules([]);
  }

  var headers = ["Item", "Cheapest hour", "vs avg", "Priciest hour", "vs avg",
                 "Swing", "Readings", "Min days/hr", "Status"];
  out.getRange(1, 1, 1, headers.length).setValues([headers]);

  var results = [];
  var sheets = ss.getSheets();

  for (var s = 0; s < sheets.length; s++) {
    var sheet = sheets[s];
    if (sheet.getName() === "BEST TIMES") continue;

    var row = analyseSheet(sheet);
    if (row) results.push(row);
  }

  if (results.length > 0) {
    out.getRange(2, 1, results.length, headers.length).setValues(results);
  }

  out.getRange(1, 1, 1, headers.length).setFontWeight("bold");
  out.setFrozenRows(1);
  if (results.length > 0) {
    out.getRange(2, 3, results.length, 1).setNumberFormat("+0.00%;-0.00%");
    out.getRange(2, 5, results.length, 1).setNumberFormat("+0.00%;-0.00%");
    out.getRange(2, 6, results.length, 1).setNumberFormat("0.00%");
  }

  // Green only for a result that actually cleared the significance bar.
  var statusRange = out.getRange(2, 9, Math.max(results.length, 1), 1);
  out.setConditionalFormatRules([
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo("OK").setBackground("#b7e1cd")
      .setRanges([statusRange]).build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextStartsWith("NOT").setBackground("#fce8b2")
      .setRanges([statusRange]).build()
  ]);

  out.autoResizeColumns(1, headers.length);
  out.getRange("K1").setValue("Last updated");
  out.getRange("K2").setValue(new Date())
     .setNumberFormat("m/d/yyyy h:mm AM/PM");
}

/** Returns a result row for one item sheet, or null if it isn't one. */
function analyseSheet(sheet) {
  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return null;

  var tCol = data[0].indexOf("timestamp_local");
  var pCol = data[0].indexOf("sold_median");
  if (tCol === -1 || pCol === -1) return null;     // not a price sheet

  var pts = [];
  var skipped = 0;
  for (var i = 1; i < data.length; i++) {
    var t = data[i][tCol];
    var p = Number(data[i][pCol]);
    // A cell that lost its date format arrives as a number, not a Date. Count
    // those rather than dropping them silently -- an all-skipped sheet looks
    // identical to an empty one otherwise.
    if (!(t instanceof Date)) { skipped++; continue; }
    if (!isFinite(p) || p <= 0) { skipped++; continue; }
    pts.push({ t: t.getTime(), hour: t.getHours(), p: p,
               day: t.getFullYear() + "-" + t.getMonth() + "-" + t.getDate() });
  }

  if (pts.length === 0) {
    return [sheet.getName(), "", "", "", "", "", 0, 0,
            skipped > 0 ? "UNREADABLE DATES (" + skipped + " rows)" : "NO DATA"];
  }

  pts.sort(function (a, b) { return a.t - b.t; });

  // Centred +/-12h baseline via a sliding window (single pass, not O(n^2)).
  var HALF = 12 * 3600 * 1000;
  var lo = 0, hi = 0, sum = 0;
  var daily = {};                     // "day|hour" -> running mean of that hour

  for (var k = 0; k < pts.length; k++) {
    while (hi < pts.length && pts[hi].t <= pts[k].t + HALF) { sum += pts[hi].p; hi++; }
    // The `lo < hi` guard matters: after a long gap in readings the trailing
    // edge can otherwise walk past the end of the array and read undefined.
    while (lo < hi && pts[lo].t < pts[k].t - HALF) { sum -= pts[lo].p; lo++; }
    var n = hi - lo;
    if (n < 2) continue;
    var base = sum / n;
    if (base <= 0) continue;
    var key = pts[k].day + "|" + pts[k].hour;
    var cell = daily[key];
    if (!cell) cell = daily[key] = { hour: pts[k].hour, sum: 0, n: 0 };
    cell.sum += pts[k].p / base - 1;
    cell.n++;
  }

  // One observation per day per hour, so each day counts once.
  var buckets = [];
  for (var h = 0; h < 24; h++) buckets.push([]);
  for (var key2 in daily) {
    var c = daily[key2];
    buckets[c.hour].push(c.sum / c.n);
  }

  var hours = [];
  var minDays = Infinity;
  for (var hh = 0; hh < 24; hh++) {
    var v = buckets[hh];
    if (v.length === 0) continue;
    minDays = Math.min(minDays, v.length);
    hours.push({ hour: hh, mean: mean(v), n: v.length, sd: stdev(v) });
  }
  if (hours.length === 0) {
    return [sheet.getName(), "", "", "", "", "", pts.length, 0, "NOT ENOUGH DATA"];
  }
  if (!isFinite(minDays)) minDays = 0;

  var lowH = hours[0], highH = hours[0];
  for (var q = 1; q < hours.length; q++) {
    if (hours[q].mean < lowH.mean) lowH = hours[q];
    if (hours[q].mean > highH.mean) highH = hours[q];
  }

  var swing = highH.mean - lowH.mean;
  // Error bar on the difference of the two hourly means.
  var se = Math.sqrt(
    (lowH.sd * lowH.sd) / Math.max(lowH.n, 1) +
    (highH.sd * highH.sd) / Math.max(highH.n, 1)
  );

  var status;
  if (hours.length < 24 || minDays < MIN_DAYS_PER_HOUR) {
    status = "NOT ENOUGH DATA";
  } else if (!(se > 0) || swing / se < SIGNIFICANCE_T) {
    // Picking the max and min of 24 noisy averages produces a large gap by
    // construction, so a swing that doesn't clear its error bar means nothing.
    status = "NOT SIGNIFICANT";
  } else {
    status = "OK";
  }

  return [
    sheet.getName(),
    pad(lowH.hour) + ":00", lowH.mean,
    pad(highH.hour) + ":00", highH.mean,
    swing, pts.length, minDays, status
  ];
}

function mean(a) {
  var s = 0;
  for (var i = 0; i < a.length; i++) s += a[i];
  return s / a.length;
}

function stdev(a) {
  if (a.length < 2) return 0;
  var m = mean(a), s = 0;
  for (var i = 0; i < a.length; i++) s += (a[i] - m) * (a[i] - m);
  return Math.sqrt(s / (a.length - 1));
}

function pad(n) { return (n < 10 ? "0" : "") + n; }
