#!/usr/bin/env python3
"""
Runs INSIDE GitHub Actions (which has repo write access + reliable headless).
1. Reads history.csv (all prior data incl. manual Storage King/Safestore + Big Top rate card).
2. Reads data/latest.json (today's sweep: Shurgard API + Make Space).
3. Appends today's automated rows to history.csv (Shurgard + Make Space only;
   Big Top stays the static rate card; Storage King/Safestore stay manual).
4. Builds the grid email -> report/email.html + report/subject.txt.
   Layout: one colour-banded column group per competitor, each group =
   Discounted | Standard | Discount | Duration (Safestore adds two 1yr columns).
5. Detects day-over-day changes for the summary line.
Self-contained: no dependency on the Claude session or the Projects tool.
"""
import csv, json, os, re
from html import escape
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(ROOT, 'history.csv')
LATEST = os.path.join(ROOT, 'data', 'latest.json')
OUTDIR = os.path.join(ROOT, 'report')
os.makedirs(OUTDIR, exist_ok=True)

# UK date (Actions runs UTC; BST is UTC+1 in summer — close enough for a date stamp)
TODAY = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%d')

# (competitor key, display name, footnote mark, dark colour, light band colour)
GROUPS = [
    ('Big Top (own)', 'Big Top', '', '#1F3864', '#DCE3F0'),
    ('Shurgard Basildon', 'Shurgard', '*', '#9C0006', '#F2CBCC'),
    ('Storage King Basildon', 'Storage King', '**', '#7F6000', '#FFF2CC'),
    ('Make Space (Billericay)', 'Make Space', '†', '#375623', '#E2EFDA'),
    ('Safestore Basildon', 'Safestore', '***', '#1F3864', '#D9E1F2'),
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
    """size -> comp -> (offer, rack_or_None, promo_text) for the latest date."""
    latest = {}
    sticky_rack = {}  # (comp,size) -> most recent non-empty standard rate seen (any date)
    for r in read_hist():
        if r['metric'] not in PRICE_METRICS or not r['size_sqft'] or not r['offer_rate_pw_gbp']:
            continue
        key = (r['competitor'], float(r['size_sqft']))
        rack = float(r['rack_rate_pw_gbp']) if r['rack_rate_pw_gbp'] else None
        # remember the newest non-empty standard rate so a bare daily sweep row
        # (offer only) doesn't wipe the std value captured earlier.
        if rack is not None and (key not in sticky_rack or r['date'] >= sticky_rack[key][0]):
            sticky_rack[key] = (r['date'], rack)
        val = (r['date'], float(r['offer_rate_pw_gbp']), rack, r['promo_text'] or '')
        cur = latest.get(key)
        if cur is None or val[0] > cur[0] or (val[0] == cur[0] and val[1] < cur[1]):
            latest[key] = val
    grid = defaultdict(dict)
    for (comp, size), (_, price, rack, promo) in latest.items():
        if rack is None and (comp, size) in sticky_rack:
            rack = sticky_rack[(comp, size)][1]  # inherit last known standard rate
        cur = grid[size].get(comp)
        if cur is None or price < cur[0]:
            grid[size][comp] = (price, rack, promo)
    return grid


def load_1yr():
    """Latest 12-month fixed-term manual quotes (metric manual_quote_1yr):
    size -> (intro_offer_pw, follow_on_rate_pw)."""
    latest = {}
    for r in read_hist():
        if r['metric'] != 'manual_quote_1yr' or not r['size_sqft'] or not r['offer_rate_pw_gbp']:
            continue
        key = (r['competitor'], float(r['size_sqft']))
        rack = float(r['rack_rate_pw_gbp']) if r['rack_rate_pw_gbp'] else None
        val = (r['date'], float(r['offer_rate_pw_gbp']), rack)
        if key not in latest or val[0] >= latest[key][0]:
            latest[key] = val
    out = {}
    for (comp, size), (_, offer, rack) in latest.items():
        cur = out.get(size)
        if cur is None or offer < cur[0]:
            out[size] = (offer, rack)
    return out


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


def promo_cols(comp, promo):
    """Parse a promo_text into (Discount, Duration) display strings. Never raises."""
    dash = '&mdash;'
    try:
        if comp == 'Big Top (own)':
            return ('£1/wk', 'first 6 weeks')
        if not promo:
            return (dash, dash)
        if comp == 'Storage King Basildon':
            m = re.search(r'(\d+)% off', promo)
            d = (m.group(1) + '%') if m else dash
            m2 = re.search(r'first (\d+) months?', promo)
            t = ('first %s months' % m2.group(1)) if m2 else dash
            return (d, t)
        if comp == 'Safestore Basildon':
            m = re.search(r'(\d+)% online discount', promo)
            d = (m.group(1) + '% online') if m else dash
            if promo.startswith('£1 first month'):
                t = '£1 first month'
            else:
                m2 = re.search(r'50% off first (\d+) weeks', promo)
                t = ('50% off first %s weeks' % m2.group(1)) if m2 else escape(promo.split('—')[0].strip()[:40])
            return (d, t)
        if comp == 'Shurgard Basildon':
            if '£1 first month' in promo:
                d = '£1 first month'
            elif '50% off first month' in promo:
                d = '50% first month'
            else:
                m = re.search(r'(\d+)% off', promo)
                d = (m.group(1) + '%') if m else dash
            m2 = re.search(r'ends (\d{2}/\d{2}/\d{4})', promo)
            t = ('ends %s' % m2.group(1)) if m2 else dash
            return (d, t)
        if comp == 'Make Space (Billericay)':
            m = re.search(r'(\d+)% off \(([^)]*)\)', promo)
            if m:
                d = '%s%% (%s)' % (m.group(1), escape(m.group(2)))
            else:
                m1 = re.search(r'(\d+)% off', promo)
                d = (m1.group(1) + '%') if m1 else dash
            m2 = re.search(r'first (\d+) weeks', promo)
            t = ('first %s weeks' % m2.group(1)) if m2 else dash
            return (d, t)
        return (dash, escape(promo[:40]))
    except Exception:
        return (dash, dash)


def _yr_cells(y, base):
    dash = '<td style="%scolor:#999;">&mdash;</td>' % base
    if y is None:
        return dash * 2
    offer, rack = y
    c1 = '<td style="%s">£%.2f</td>' % (base, offer)
    c2 = ('<td style="%s">£%.2f</td>' % (base, rack)) if rack is not None else dash
    return c1 + c2


def build_email(grid, summary_lines, footnotes, grid1yr=None):
    grid1yr = grid1yr or {}
    sizes = sorted(set(grid.keys()) | set(grid1yr.keys()))
    h1 = ('<tr><th rowspan="2" style="padding:6px 8px;color:#1F3864;text-align:left;'
          'border-bottom:2px solid #1F3864;vertical-align:bottom;">Size (sq ft)</th>')
    for comp, name, mark, dark, light in GROUPS:
        span = 6 if comp == 'Safestore Basildon' else 4
        h1 += ('<th colspan="%d" style="padding:6px 8px;background:%s;color:%s;'
               'text-align:center;border-bottom:1px solid %s;">%s%s</th>' % (span, light, dark, dark, name, mark))
    h1 += '</tr>'
    h2 = '<tr>'
    for comp, name, mark, dark, light in GROUPS:
        subs = ['Discounted', 'Standard', 'Discount', 'Duration']
        if comp == 'Safestore Basildon':
            subs += ['1yr promo‡', '1yr follow on rate‡']
        for s in subs:
            h2 += ('<th style="padding:4px 8px;background:%s;color:#666;font-size:11px;'
                   'text-align:right;border-bottom:2px solid %s;">%s</th>' % (light, dark, s))
    h2 += '</tr>'

    rows = ''
    for size in sizes:
        bt_entry = grid[size].get('Big Top (own)')
        bt = bt_entry[0] if bt_entry else None
        tds = '<td style="padding:4px 8px;border-bottom:1px solid #eee;font-weight:bold;">%d</td>' % int(size)
        for comp, name, mark, dark, light in GROUPS:
            base = ('padding:4px 8px;border-bottom:1px solid #eee;text-align:right;'
                    'vertical-align:top;background:%s;' % light)
            dash = '<td style="%scolor:#999;">&mdash;</td>' % base
            entry = grid[size].get(comp)
            if entry is None:
                cells = dash * 4
                if comp == 'Safestore Basildon':
                    cells += _yr_cells(grid1yr.get(size), base)
                tds += cells
                continue
            price, rack, promo = entry
            hot = comp != 'Big Top (own)' and bt is not None and price < bt
            hot_style = 'color:#C00000;font-weight:bold;' if hot else ''
            if rack is None or abs(rack - price) < 0.01:
                # single price (rate card / no separate standard): show under Standard
                cells = dash + '<td style="%s%s">£%.2f</td>' % (base, hot_style or 'font-weight:bold;', price)
            else:
                cells = ('<td style="%s%s">£%.2f</td>' % (base, hot_style, price)
                         + '<td style="%s%s">£%.2f</td>' % (base, hot_style, rack))
            d, t = promo_cols(comp, promo)
            cells += '<td style="%sfont-size:11px;color:#444;">%s</td>' % (base, d)
            cells += '<td style="%sfont-size:11px;color:#444;">%s</td>' % (base, t)
            if comp == 'Safestore Basildon':
                cells += _yr_cells(grid1yr.get(size), base)
            tds += cells
        rows += '<tr>%s</tr>' % tds

    summary_html = ''.join(f'<p style="margin:4px 0;">{s}</p>' for s in summary_lines)
    notes_html = ''.join(f'<p style="margin:3px 0;font-size:12px;color:#555;">{n}</p>' for n in footnotes)
    return f"""<html><body style="font-family:Arial,sans-serif;color:#222;">
<h2 style="margin:0 0 4px;">Basildon competitor prices &mdash; {TODAY}</h2>
<p style="margin:2px 0 10px;font-size:13px;color:#555;">Weekly rates (&pound;, inc VAT). Discounted = current selling/web rate; Standard = rate after the promo ends; a single price under Standard means no separate discounted rate. Red = a rival selling rate below Big Top at that size. &mdash; = not available.</p>
{summary_html}
<table style="border-collapse:collapse;margin:10px 0;font-size:12px;">{h1}{h2}{rows}</table>
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
        '* Shurgard: swept daily; Discount/Duration parsed from the live web promo.',
        '** Storage King: manual check; carried forward until refreshed.',
        '*** Safestore: manual check; carried forward until refreshed.',
        '† Make Space (Billericay): swept daily; intro offers vary by unit — some sizes get no intro discount at all, so trust the per-size Discount cell, not a blanket headline.',
        '‡ Safestore 1yr columns: 12-month fixed-term manual quotes. "1yr promo" = intro weekly rate for the first 52 weeks; "1yr follow on rate" = ongoing discounted weekly rate. Carried forward until refreshed; blank where no 1-yr quote exists.',
        'Big Top: authoritative rate card (weekly, inc VAT), shown under Standard; £1/wk for the first 6 weeks. A blank where a rival lists a price = that Big Top size is out of stock.',
    ]
    grid = load_grid()
    grid1 = load_1yr()
    html = build_email(grid, summary, footnotes, grid1)
    open(os.path.join(OUTDIR, 'email.html'), 'w').write(html)
    open(os.path.join(OUTDIR, 'subject.txt'), 'w').write(f'Basildon competitor prices — {TODAY}')
    print(f'{msg}; {len(changes)} changes; grid sizes={len(grid)}; 1yr sizes={len(grid1)}')


if __name__ == '__main__':
    main()
