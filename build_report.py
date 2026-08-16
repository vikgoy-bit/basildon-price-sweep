#!/usr/bin/env python3
"""
Runs INSIDE GitHub Actions (which has repo write access + reliable headless).
1. Reads history.csv (all prior data incl. manual Storage King/Safestore + Big Top rate card).
2. Reads data/latest.json (today's sweep: Shurgard API + Make Space).
3. Appends today's automated rows to history.csv (Shurgard + Make Space only;
   Big Top stays the static rate card; Storage King/Safestore stay manual).
4. Builds the grid email -> report/email.html + report/subject.txt.
5. Detects day-over-day changes for the summary line.
Self-contained: no dependency on the Claude session or the Projects tool.
"""
import csv, json, os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(ROOT, 'history.csv')
LATEST = os.path.join(ROOT, 'data', 'latest.json')
OUTDIR = os.path.join(ROOT, 'report')
os.makedirs(OUTDIR, exist_ok=True)

# UK date (Actions runs UTC; BST is UTC+1 in summer — close enough for a date stamp)
TODAY = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%d')

COLS = [
    ('Big Top (own)', 'Big Top', ''),
    ('Shurgard Basildon', 'Shurgard', '*'),
    ('Storage King Basildon', 'Storage King', '**'),
    ('Safestore Basildon', 'Safestore', '***'),
    ('Make Space (Billericay)', 'Make Space', '†'),
]
PRICE_METRICS = {'unit', 'featured_unit', 'from_price', 'quote_after_test_form',
                 'quote_step_price', 'manual_quote', 'ratecard'}
HEADER = ['date', 'competitor', 'metric', 'size_sqft', 'rack_rate_pw_gbp',
          'offer_rate_pw_gbp', 'promo_text', 'source_url', 'notes']


def read_hist():
    if not os.path.exists(HIST):
        return []
    with open(HIST, newline='') as f:
        return list(csv.DictReader(f))


def append_today():
    """Append today's Shurgard + Make Space observations from the sweep."""
    if not os.path.exists(LATEST):
        return 0, 'sweep file missing'
    data = json.load(open(LATEST))
    obs = data.get('observations', [])
    rows = []
    for o in obs:
        comp = o.get('competitor', '')
        size = o.get('size_sqft')
        offer = o.get('offer_rate')
        if not size or offer in (None, ''):
            continue
        if 'Shurgard' in comp:
            rows.append([TODAY, 'Shurgard Basildon', 'unit', int(size),
                         o.get('rack_rate') or '', offer, o.get('promo') or '',
                         o.get('source') or '', 'daily Actions sweep'])
        elif 'Make Space' in comp:
            rows.append([TODAY, 'Make Space (Billericay)', 'quote_after_test_form', int(size),
                         o.get('rack_rate') or '', offer, o.get('promo') or '',
                         o.get('source') or '', 'daily Actions sweep'])
    # don't double-append if today's rows already exist for that competitor
    existing = {(r['date'], r['competitor']) for r in read_hist()}
    new = [r for r in rows if (str(r[0]), r[1]) not in existing]
    if new:
        write_header = not os.path.exists(HIST)
        with open(HIST, 'a', newline='') as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(HEADER)
            w.writerows(new)
    return len(new), f'{len(new)} rows appended'


def load_grid():
    latest = {}
    for r in read_hist():
        if r['metric'] not in PRICE_METRICS or not r['size_sqft'] or not r['offer_rate_pw_gbp']:
            continue
        key = (r['competitor'], float(r['size_sqft']))
        rack = float(r['rack_rate_pw_gbp']) if r['rack_rate_pw_gbp'] else None
        val = (r['date'], float(r['offer_rate_pw_gbp']), rack)
        if key not in latest or val[0] >= latest[key][0]:
            if key in latest and val[0] == latest[key][0]:
                keep = latest[key]
                val = (val[0], min(val[1], keep[1]), val[2] if val[1] <= keep[1] else keep[2])
            latest[key] = val
    grid = defaultdict(dict)
    for (comp, size), (_, price, rack) in latest.items():
        cur = grid[size].get(comp)
        if cur is None or price < cur[0]:
            grid[size][comp] = (price, rack)
    return grid


def detect_changes():
    """Compare the two most recent dates of Shurgard/Make Space automated data."""
    per = defaultdict(dict)  # date -> (comp,size) -> offer
    dates = set()
    for r in read_hist():
        if r['competitor'] in ('Shurgard Basildon', 'Make Space (Billericay)') and r['size_sqft'] and r['offer_rate_pw_gbp']:
            per[r['date']][(r['competitor'], float(r['size_sqft']))] = float(r['offer_rate_pw_gbp'])
            dates.add(r['date'])
    ds = sorted(dates)
    if len(ds) < 2:
        return []
    today_d, prev_d = ds[-1], ds[-2]
    changes = []
    for k, v in per[today_d].items():
        pv = per[prev_d].get(k)
        if pv is not None and abs(pv - v) >= 0.01:
            comp, size = k
            changes.append(f'{comp} {int(size)} sq ft: £{pv:.2f} → £{v:.2f}')
    return changes


def build_email(grid, summary_lines, footnotes):
    sizes = sorted(grid.keys())
    th = '<tr><th style="padding:6px 10px;background:#DCE3F0;color:#1F3864;text-align:left;border-bottom:2px solid #1F3864;">Size (sq ft)</th>'
    for _, name, mark in COLS:
        th += f'<th style="padding:6px 10px;background:#DCE3F0;color:#1F3864;text-align:right;border-bottom:2px solid #1F3864;">{name}{mark}</th>'
    th += '</tr>'
    rows = ''
    for size in sizes:
        bt_entry = grid[size].get('Big Top (own)')
        bt = bt_entry[0] if bt_entry else None
        tds = f'<td style="padding:5px 10px;border-bottom:1px solid #ddd;font-weight:bold;">{int(size)}</td>'
        for comp, _, _ in COLS:
            entry = grid[size].get(comp)
            if entry is None:
                cell, style = '&mdash;', 'color:#999;'
            else:
                p, rack = entry
                cell = f'£{p:.2f}'
                if rack and abs(rack - p) >= 0.01:
                    cell += f'<br><span style="font-size:10px;color:#777;font-weight:normal;">std £{rack:.2f}</span>'
                style = ''
                if comp != 'Big Top (own)' and bt is not None and p < bt:
                    style = 'background:#FDE7E9;color:#B00020;font-weight:bold;'
            tds += f'<td style="padding:5px 10px;border-bottom:1px solid #ddd;text-align:right;vertical-align:top;{style}">{cell}</td>'
        rows += f'<tr>{tds}</tr>'
    summary_html = ''.join(f'<p style="margin:4px 0;">{s}</p>' for s in summary_lines)
    notes_html = ''.join(f'<p style="margin:3px 0;font-size:12px;color:#555;">{n}</p>' for n in footnotes)
    return f"""<html><body style="font-family:Arial,sans-serif;color:#222;">
<h2 style="margin:0 0 4px;">Basildon competitor prices &mdash; {TODAY}</h2>
<p style="margin:2px 0 10px;font-size:13px;color:#555;">Weekly rates: large = current selling/web rate, small "std" = standard rate after promo. Red = a rival selling rate below Big Top at that size. &mdash; = not available.</p>
{summary_html}
<table style="border-collapse:collapse;margin:10px 0;font-size:13px;">{th}{rows}</table>
<h3 style="margin:14px 0 4px;font-size:14px;">Comments</h3>
{notes_html}
</body></html>"""


def main():
    n, msg = append_today()
    changes = detect_changes()
    if changes:
        summary = ['<b>Changes today:</b> ' + '; '.join(changes[:8]) + ('…' if len(changes) > 8 else '')]
    else:
        summary = ['<b>No day-over-day price changes</b> in the automated sources (Shurgard, Make Space).']
    # Shurgard promo-expiry watch
    for r in read_hist():
        if r['competitor'] == 'Shurgard Basildon' and 'ends 21/08/2026' in (r['notes'] + r['promo_text']):
            summary.append('<b>Watch:</b> Shurgard special rates end 21 Aug 2026.')
            break
    footnotes = [
        '* Shurgard: web (special) rate large, standard beneath. £1 first month; special rates end 21 Aug 2026.',
        '** Storage King: promo rate large, standard beneath. From your manual/browser check; carried forward until refreshed.',
        '*** Safestore: online rate large, standard beneath. From your manual check; carried forward until refreshed.',
        '† Make Space (Billericay): web rate large, standard beneath. Intro 50% off first 12 weeks.',
        'Big Top: authoritative rate card (weekly, inc VAT); £1 for first 6 weeks. A blank where a rival lists a price = that Big Top size is out of stock.',
    ]
    grid = load_grid()
    html = build_email(grid, summary, footnotes)
    open(os.path.join(OUTDIR, 'email.html'), 'w').write(html)
    open(os.path.join(OUTDIR, 'subject.txt'), 'w').write(f'Basildon competitor prices — {TODAY}')
    print(f'{msg}; {len(changes)} changes; grid sizes={len(grid)}')


if __name__ == '__main__':
    main()
