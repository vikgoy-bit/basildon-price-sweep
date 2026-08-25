#!/usr/bin/env python3
"""
Runs INSIDE GitHub Actions (which has repo write access + reliable headless).
1. Reads history.csv (all prior data incl. manual Storage King/Safestore + Big Top rate card).
2. Reads data/latest.json (today's sweep: Shurgard API + Make Space + Big Top).
3. Appends today's automated rows to history.csv:
   - Shurgard + Make Space: unchanged, always appended as before.
   - Big Top: HYBRID (v4, 25 Aug 2026). Sizes the live site shows today get a
     fresh row (weekly-normalised, monthly x12/52); sizes NOT shown today
     (out of stock / off the site) are simply not written, so load_grid()'s
     latest-date-per-size lookup naturally falls back to whatever that size's
     last-known row was (in practice: the 15 Aug 2026 ratecard baseline, or a
     more recent live row if it was visible more recently than that).
     A sanity guard rejects any weekly figure outside £5-£600 before writing
     it — this exists specifically to catch a mis-parsed promo banner (e.g.
     a stray "£1" from a "£1 for 6 weeks" banner) rather than a real price;
     a rejected size falls back to last-known exactly like a hidden size.
4. Builds the grid email -> report/email.html + report/subject.txt.
   Layout: one colour-banded column group per competitor, each group =
   Discounted | Standard | Discount | Duration (Safestore adds two 1yr columns).
5. Detects day-over-day changes for the summary line, PER COMPETITOR (each
   competitor compared against its own most recent prior date, not a single
   shared "yesterday" across all three — Big Top may have gaps, e.g. no
   automated row between the 15 Aug ratecard baseline and the first hybrid
   run, so a shared-date comparison would silently miss a real Big Top move
   on exactly the run where catching it matters most).
Self-contained: no dependency on the Claude session or the Projects tool.

CAVEAT (unchanged from architecture doc): if a rate is changed in Storeganise
but bigtopselfstorage.com isn't republished, this sweep re-indexes the OLD
price from the still-stale site. An empty Big Top change line after a rate
change you know happened means the site itself is stale, not that nothing
moved — check the site before assuming the sweep is wrong.

NOTE: the homepage ("£1 for 6 weeks") vs /pricing ("50% off first 8 weeks")
lead-offer inconsistency is still unresolved (flagged 13 Aug 2026), so the
Discount/Duration columns for Big Top still show fixed placeholder copy
rather than parsing per-size promo text from the live scrape. Fixing that
is a separate decision, not part of this hybrid-pricing change.
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

# Big Top hybrid guard: weekly rates outside this range are rejected as
# mis-parses (e.g. a "£1" promo-banner fragment), never written to history.
BIGTOP_MIN_PW, BIGTOP_MAX_PW = 5.0, 600.0

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
AUTOMATED_COMPETITORS = ('Shurgard Basildon', 'Make Space (Billericay)', 'Big Top (own)')


def read_hist():
    if not os.path.exists(HIST):
        return []
    with open(HIST, newline='') as f:
        return list(csv.DictReader(f))


def _to_weekly(value, per_unit):
    """Normalise a scraped price to a weekly £ figure. per_unit is whatever
    scrape.js recorded in the observation's 'per' field (usually 'week', but
    the Big Top site has shown monthly figures elsewhere e.g. the £35/month
    student rate captured 13 Aug 2026 as ~£8.08/wk using the same x12/52)."""
    p = (per_unit or '').lower()
    is_monthly = 'month' in p or p in ('mo', '/mo', 'pm')
    return round(value * 12 / 52, 2) if is_monthly else round(value, 2)


def append_today():
    """Append today's Shurgard + Make Space + Big Top (hybrid) observations."""
    if not os.path.exists(LATEST):
        return 0, 'sweep file missing'
    data = json.load(open(LATEST))
    obs = data.get('observations', [])
    rows = []
    bigtop_best = {}  # size_int -> (weekly_offer, weekly_rack_or_None, promo, source)
    bigtop_rejected = []  # sizes seen but rejected by the sanity guard, for the log line
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
        elif 'Big Top' in comp:
            try:
                offer_val = float(offer)
            except (TypeError, ValueError):
                continue
            per_unit = o.get('per') or 'week'
            weekly = _to_weekly(offer_val, per_unit)
            if not (BIGTOP_MIN_PW <= weekly <= BIGTOP_MAX_PW):
                bigtop_rejected.append((size, weekly))
                continue  # sanity guard: falls back to last-known, same as a hidden size
            rack_weekly = None
            rack = o.get('rack_rate')
            if rack not in (None, ''):
                try:
                    rack_weekly = _to_weekly(float(rack), per_unit)
                except (TypeError, ValueError):
                    rack_weekly = None
            size_i = int(size)
            # a size can appear more than once in one sweep (both /reserve and
            # /pricing are scraped) — keep the lowest valid weekly figure, same
            # tie-break load_grid() already uses elsewhere in this file.
            cur = bigtop_best.get(size_i)
            if cur is None or weekly < cur[0]:
                bigtop_best[size_i] = (weekly, rack_weekly, o.get('promo') or '', o.get('source') or '')
    for size_i, (weekly, rack_weekly, promo, source) in bigtop_best.items():
        rows.append([TODAY, 'Big Top (own)', 'unit', size_i,
                     rack_weekly if rack_weekly is not None else '', weekly, promo,
                     source, 'daily Actions sweep (live site, hybrid v4)'])
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
    msg = f'{len(new)} rows appended'
    if bigtop_rejected:
        msg += f'; Big Top sanity guard rejected {len(bigtop_rejected)} size(s): {bigtop_rejected}'
    return len(new), msg


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
    """Compare each automated competitor's own two most recent dates
    independently (NOT a single shared 'yesterday' across all three) — Big
    Top can have gaps (e.g. nothing between the 15 Aug ratecard baseline and
    the first hybrid-scrape row), and a shared-date comparison would compare
    Big Top against a date it has no row on, silently reporting no change
    even when the real gap (vs. its true last-known row) is large."""
    per_comp = defaultdict(lambda: defaultdict(dict))  # comp -> date -> size -> offer
    for r in read_hist():
        if r['competitor'] in AUTOMATED_COMPETITORS and r['size_sqft'] and r['offer_rate_pw_gbp']:
            per_comp[r['competitor']][r['date']][float(r['size_sqft'])] = float(r['offer_rate_pw_gbp'])
    changes = []
    for comp in AUTOMATED_COMPETITORS:
        dates = sorted(per_comp[comp].keys())
        if len(dates) < 2:
            continue
        today_d, prev_d = dates[-1], dates[-2]
        for size, v in per_comp[comp][today_d].items():
            pv = per_comp[comp][prev_d].get(size)
            if pv is not None and abs(pv - v) >= 0.01:
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
        summary = ['<b>No day-over-day price changes</b> in the automated sources (Shurgard, Make Space, Big Top).']
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
        'Big Top: hybrid (v4, 25 Aug 2026) — sizes currently listed on bigtopselfstorage.com update daily from the live site; sizes not shown (out of stock) keep their last-known rate (15 Aug 2026 rate card baseline). Weekly figures outside £5–£600 are rejected as mis-parses and fall back to last-known too. Homepage vs /pricing lead-offer wording still differs (£1/6wks vs 50% off/8wks, unresolved) so the Discount/Duration columns still show fixed £1/6-week copy pending that decision. If a known Storeganise rate change doesn’t show up here, the live site likely hasn’t been republished yet — check the site, don’t assume the sweep is wrong.',
    ]
    grid = load_grid()
    grid1 = load_1yr()
    html = build_email(grid, summary, footnotes, grid1)
    open(os.path.join(OUTDIR, 'email.html'), 'w').write(html)
    open(os.path.join(OUTDIR, 'subject.txt'), 'w').write(f'Basildon competitor prices — {TODAY}')
    print(f'{msg}; {len(changes)} changes; grid sizes={len(grid)}; 1yr sizes={len(grid1)}')


if __name__ == '__main__':
    main()
