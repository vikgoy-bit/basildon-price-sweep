#!/usr/bin/env python3
"""
Generates price_chart.html at the repo root: a self-contained interactive
price-history page (Chart.js from cdnjs, data inlined as JSON, no external
data fetch). Committed daily by the "Commit results" workflow step.

IMPORTANT: this script must NEVER fail the workflow. main() is wrapped in
try/except and the process always exits 0; the daily.yml step also has
continue-on-error. A broken chart must not block the email or data commit.
"""
import csv, json, os, sys, traceback
from collections import defaultdict
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(ROOT, 'history.csv')
OUT = os.path.join(ROOT, 'price_chart.html')

COMPETITORS = ['Big Top (own)', 'Shurgard Basildon', 'Storage King Basildon',
               'Safestore Basildon', 'Make Space (Billericay)']
SHORT = {'Big Top (own)': 'Big Top', 'Shurgard Basildon': 'Shurgard',
         'Storage King Basildon': 'Storage King', 'Safestore Basildon': 'Safestore',
         'Make Space (Billericay)': 'Make Space'}
COLORS = {'Big Top (own)': '#1F3864', 'Shurgard Basildon': '#C00000',
          'Storage King Basildon': '#2E7D32', 'Safestore Basildon': '#E65100',
          'Make Space (Billericay)': '#6A1B9A'}
# same price metrics as the email grid; manual_quote_1yr is a different
# product (12-month fixed term) and would distort the weekly-rate lines.
METRICS = {'unit', 'featured_unit', 'from_price', 'quote_after_test_form',
           'quote_step_price', 'manual_quote', 'ratecard'}


def build_series():
    """(comp, size, date) -> lowest offer that day (+ its standard rate)."""
    best = {}
    with open(HIST, newline='') as f:
        for r in csv.DictReader(f):
            if r['metric'] not in METRICS or r['competitor'] not in COMPETITORS:
                continue
            if not r['size_sqft'] or not r['offer_rate_pw_gbp']:
                continue
            try:
                size = float(r['size_sqft'])
                offer = float(r['offer_rate_pw_gbp'])
            except ValueError:
                continue
            rack = None
            if r['rack_rate_pw_gbp']:
                try:
                    rack = float(r['rack_rate_pw_gbp'])
                except ValueError:
                    rack = None
            key = (r['competitor'], size, r['date'])
            cur = best.get(key)
            if cur is None or offer < cur[0]:
                best[key] = (offer, rack)
    series = defaultdict(lambda: defaultdict(list))
    for (comp, size, date) in sorted(best, key=lambda k: k[2]):
        offer, rack = best[(comp, size, date)]
        series[size][comp].append({'d': date, 'o': offer, 'r': rack})
    return series


def fmt(v):
    return '&pound;%.2f' % v


def today_table(series):
    """Static latest-value grid (fallback if JS/CDN unavailable)."""
    th = '<th style="padding:6px 10px;background:#DCE3F0;color:#1F3864;text-align:left;">Size (sq ft)</th>'
    for c in COMPETITORS:
        th += '<th style="padding:6px 10px;background:#DCE3F0;color:#1F3864;text-align:right;">%s</th>' % SHORT[c]
    rows = ''
    for size in sorted(series.keys()):
        tds = '<td style="padding:5px 10px;border-bottom:1px solid #ddd;font-weight:bold;">%d</td>' % int(size)
        for c in COMPETITORS:
            pts = series[size].get(c)
            if not pts:
                cell = '<span style="color:#999;">&mdash;</span>'
            else:
                last = pts[-1]
                cell = fmt(last['o'])
                if last['r'] is not None and abs(last['r'] - last['o']) >= 0.01:
                    cell += '<br><span style="font-size:10px;color:#777;">std %s</span>' % fmt(last['r'])
                cell += '<br><span style="font-size:10px;color:#999;">%s</span>' % last['d']
            tds += '<td style="padding:5px 10px;border-bottom:1px solid #ddd;text-align:right;vertical-align:top;">%s</td>' % cell
        rows += '<tr>%s</tr>' % tds
    return '<table style="border-collapse:collapse;font-size:13px;">%s%s</table>' % ('<tr>%s</tr>' % th, rows)


TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Basildon competitor price history</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
body { font-family: Arial, sans-serif; color: #222; margin: 20px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: #555; font-size: 13px; margin: 0 0 14px; }
.controls { margin: 10px 0 14px; font-size: 14px; }
.controls select { font-size: 14px; padding: 3px 6px; margin-right: 18px; }
.controls label { margin-right: 12px; cursor: pointer; }
#wrap { max-width: 950px; }
#nochart { display: none; color: #B00020; font-size: 14px; margin: 10px 0; }
h2 { font-size: 16px; margin: 26px 0 8px; }
.note { color: #777; font-size: 12px; margin-top: 6px; }
</style>
</head>
<body>
<h1>Basildon competitor prices &mdash; history</h1>
<p class="sub">Weekly rates (&pound;, inc VAT). Stepped lines: each price holds until the next observation &mdash; gaps are carry-forward, not interpolation. Generated __GENDATE__.</p>
<div class="controls">
  Size: <select id="size"></select>
  <label><input type="radio" name="mode" value="o" checked> Promo / selling rate</label>
  <label><input type="radio" name="mode" value="r"> Standard rate</label>
</div>
<div id="wrap"><canvas id="chart"></canvas></div>
<p id="nochart">Chart could not load (no internet / CDN blocked). The latest prices are in the table below.</p>
<h2>Latest prices per size</h2>
__TABLE__
<p class="note">Storage King and Safestore are manual checks carried forward until refreshed; Shurgard and Make Space are swept daily; Big Top is the internal rate card.</p>
<script>
var DATA = __DATA__;
var COLORS = __COLORS__;
var SHORT = __SHORT__;
var chart = null;
function draw() {
  var sel = document.getElementById('size');
  var size = sel.value;
  var mode = document.querySelector('input[name="mode"]:checked').value;
  var per = DATA.series[size] || {};
  var dateSet = {};
  var comps = [];
  for (var i = 0; i < DATA.competitors.length; i++) {
    var c = DATA.competitors[i];
    if (per[c]) {
      comps.push(c);
      for (var j = 0; j < per[c].length; j++) { dateSet[per[c][j].d] = 1; }
    }
  }
  var dates = Object.keys(dateSet).sort();
  var datasets = [];
  for (var k = 0; k < comps.length; k++) {
    var comp = comps[k];
    var byDate = {};
    for (var m = 0; m < per[comp].length; m++) {
      var p = per[comp][m];
      byDate[p.d] = (mode === 'o') ? p.o : p.r;
    }
    var vals = [];
    for (var n = 0; n < dates.length; n++) {
      var v = byDate.hasOwnProperty(dates[n]) ? byDate[dates[n]] : null;
      vals.push(v === undefined ? null : v);
    }
    datasets.push({ label: SHORT[comp], data: vals, borderColor: COLORS[comp],
                    backgroundColor: COLORS[comp], stepped: true, spanGaps: true,
                    pointRadius: 3, borderWidth: 2 });
  }
  if (chart) { chart.destroy(); }
  var ctx = document.getElementById('chart').getContext('2d');
  chart = new Chart(ctx, {
    type: 'line',
    data: { labels: dates, datasets: datasets },
    options: {
      responsive: true,
      interaction: { mode: 'nearest', intersect: false },
      plugins: { title: { display: true,
        text: size + ' sq ft — ' + (mode === 'o' ? 'promo/selling' : 'standard') + ' weekly rate' } },
      scales: { y: { title: { display: true, text: '£ / week' } } }
    }
  });
}
function init() {
  if (typeof Chart === 'undefined') {
    document.getElementById('nochart').style.display = 'block';
    document.getElementById('wrap').style.display = 'none';
    return;
  }
  var sel = document.getElementById('size');
  for (var i = 0; i < DATA.sizes.length; i++) {
    var opt = document.createElement('option');
    opt.value = DATA.sizes[i];
    opt.textContent = DATA.sizes[i] + ' sq ft';
    sel.appendChild(opt);
  }
  if (DATA.sizes.indexOf('50') >= 0) { sel.value = '50'; }
  sel.addEventListener('change', draw);
  var radios = document.querySelectorAll('input[name="mode"]');
  for (var r = 0; r < radios.length; r++) { radios[r].addEventListener('change', draw); }
  draw();
}
init();
</script>
</body>
</html>
"""


def main():
    series = build_series()
    if not series:
        raise RuntimeError('no chartable rows found in history.csv')
    sizes = sorted(series.keys())
    payload = {
        'competitors': COMPETITORS,
        'sizes': [str(int(s)) for s in sizes],
        'series': {str(int(s)): {c: pts for c, pts in series[s].items()} for s in sizes},
    }
    gendate = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M UK')
    html = (TEMPLATE
            .replace('__DATA__', json.dumps(payload))
            .replace('__COLORS__', json.dumps(COLORS))
            .replace('__SHORT__', json.dumps(SHORT))
            .replace('__TABLE__', today_table(series))
            .replace('__GENDATE__', gendate))
    with open(OUT, 'w') as f:
        f.write(html)
    print('price_chart.html written: %d sizes, %d bytes' % (len(sizes), len(html)))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        print('chart generation failed; continuing (non-fatal by design)')
    sys.exit(0)
