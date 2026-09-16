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
SAFESTORE_LATEST = os.path.join(ROOT, 'data', 'safestore-latest.json')
STORAGEKING_LATEST = os.path.join(ROOT, 'data', 'storageking-latest.json')
OUTDIR = os.path.join(ROOT, 'report')
os.makedirs(OUTDIR, exist_ok=True)

# UK date (Actions runs UTC; BST is UTC+1 in summer — close enough for a date stamp)
TODAY = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime('%Y-%m-%d')

# Added 2026-09-16 per Vikas: a real timestamp (not just a date) for when
# each row was actually scraped, so same-day tiebreaking in load_grid()
# has an explicit signal instead of relying only on implicit file-append
# order (which is fragile -- e.g. a git merge could reorder rows, or two
# scrapers finishing close together could interleave unpredictably). Uses
# actual wall-clock time of the build_report.py run, which is close enough
# to "when scraped" for tiebreaking purposes (the underlying scrape itself
# happened seconds to minutes earlier in the same run).
NOW_ISO = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

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
          'offer_rate_pw_gbp', 'promo_text', 'source_url', 'notes', 'scraped_at']
AUTOMATED_COMPETITORS = ('Shurgard Basildon', 'Make Space (Billericay)', 'Big Top (own)',
                          'Storage King Basildon', 'Safestore Basildon')


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
                         o.get('source') or '', 'daily Actions sweep', NOW_ISO])
        elif 'Make Space' in comp:
            rows.append([TODAY, 'Make Space (Billericay)', 'quote_after_test_form', int(size),
                         o.get('rack_rate') or '', offer, o.get('promo') or '',
                         o.get('source') or '', 'daily Actions sweep', NOW_ISO])
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
                     source, 'daily Actions sweep (live site, hybrid v4)', NOW_ISO])
    # Only skip a row if an EXACT duplicate (same date, competitor, size,
    # AND price) already exists today. Fixed 2026-09-12: this used to dedup
    # on (date, competitor) alone, which silently discarded every re-scrape
    # after the first one each day -- so a legitimate same-day price change
    # (e.g. Big Top raising a rate mid-day) never made it into history.csv
    # at all, even though the live site had already changed. Keying on the
    # full (date, competitor, size, offer) tuple means a genuine price
    # change still gets appended as a new row, while an identical re-run
    # doesn't spam duplicate rows. load_grid()'s row-order tiebreak then
    # picks whichever same-day row was scraped most recently.
    existing = {(r['date'], r['competitor'], r['size_sqft'], r['offer_rate_pw_gbp']) for r in read_hist()}
    new = [r for r in rows if (str(r[0]), r[1], str(r[3]), str(r[5])) not in existing]
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


def append_prebuilt(json_path, source_label):
    """Append rows from a scraper that already emits the final history.csv
    schema (competitor, metric, size_sqft, rack_rate, offer_rate, per, promo,
    source, notes) -- used by safestore_scrape.py and storageking_scrape.py.
    A missing file or empty observations list is not an error: the scraper
    itself already decided (via its own safety valve) that today's fetch
    wasn't trustworthy, and simply wrote no observations. Either way,
    history.csv is left untouched for that competitor, so the grid falls
    back to the last-known row -- never overwritten with a blank.

    Fixed 2026-09-16 (real bug found): this used to stamp EVERY row with
    TODAY regardless of when the observation was actually captured. Both
    scrapers intentionally carry forward a good observation across
    runs/ticks that don't get a fresh price (safestore_scrape.py's
    tick_main() explicitly merges into the existing file rather than
    overwriting it) -- so re-reading the same carried-forward JSON on a
    later day silently relabeled 2-day-old data as "fresh today", for
    THREE consecutive days (2026-09-14 through -16) before being caught.
    Now uses each observation's own "scraped_date" field (added the same
    day) when present, falling back to TODAY only for older scraper
    versions/observations that predate this field.
    """
    if not os.path.exists(json_path):
        return 0, f'{source_label}: sweep file missing (scraper did not run or crashed before writing)'
    data = json.load(open(json_path))
    obs = data.get('observations', [])
    warnings = data.get('warnings', [])
    rows = []
    for o in obs:
        size = o.get('size_sqft')
        offer = o.get('offer_rate')
        if not size or offer in (None, ''):
            continue
        row_date = o.get('scraped_date') or TODAY
        row_at = o.get('scraped_at') or NOW_ISO
        rows.append([row_date, o.get('competitor', ''), o.get('metric', 'manual_quote'), int(size),
                     o.get('rack_rate') or '', offer, o.get('promo') or '',
                     o.get('source') or '', o.get('notes') or 'daily Actions sweep', row_at])
    # Fixed 2026-09-16 (found alongside the staleness bug above): this
    # dedup key omitted size_sqft entirely, so once ANY size was written
    # for a given (date, competitor, metric), every OTHER size for that
    # same combo was silently blocked from ever being appended that day --
    # e.g. a report rebuilt twice in one day (has happened before, see git
    # history of manual re-triggers) would drop every genuinely-new size
    # added between the two runs, not just true duplicates. Now includes
    # size so only a true (date, competitor, metric, size) repeat is
    # skipped.
    existing = {(r['date'], r['competitor'], r['metric'], r['size_sqft']) for r in read_hist()}
    new = [r for r in rows if (str(r[0]), r[1], r[2], str(r[3])) not in existing]
    if new:
        write_header = not os.path.exists(HIST)
        with open(HIST, 'a', newline='') as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(HEADER)
            w.writerows(new)
    msg = f'{source_label}: {len(new)} rows appended'
    if warnings:
        msg += f'; {len(warnings)} scraper warning(s): {warnings[:3]}'
    return len(new), msg


def load_grid():
    """size -> comp -> (offer, rack_or_None, promo_text) for the latest date.

    Row-order tiebreak (fixed 2026-09-12, upgraded 2026-09-16): when a
    (competitor, size) has more than one row on the SAME date -- e.g. Big
    Top's scraper checks both /reserve and /pricing, or the workflow was
    manually re-triggered more than once in a day -- keep whichever row
    was scraped MOST RECENTLY, not whichever happened to have the lower
    price.

    This used to pick the lower price as tiebreak, which was silently wrong
    the day Big Top's own price rose mid-day: a stale re-triggered scrape
    from earlier that day (lower, pre-change price) beat the fresh one
    (higher, correct, post-change price) purely because it was cheaper --
    the exact opposite of what "latest" should mean. Real-world price CAN
    go up, so "latest" must mean most-recently-observed, never
    lowest-observed.

    Upgraded 2026-09-16 per Vikas: "most recently" used to be inferred
    purely from file row order (i.e. read later from history.csv == append
    order == assumed chronological). That's fragile -- a git merge could
    reorder rows, or two scrapers finishing close together on different
    machines could interleave unpredictably, silently picking the wrong
    row with no way to tell after the fact. Now uses the row's own
    "scraped_at" ISO timestamp (added the same day) as the primary
    same-date tiebreak signal, falling back to row order (idx) only when
    scraped_at is missing (older rows written before this field existed).
    """
    latest = {}
    sticky_rack = {}  # (comp,size) -> most recent non-empty standard rate seen (any date)
    for idx, r in enumerate(read_hist()):
        if r['metric'] not in PRICE_METRICS or not r['size_sqft'] or not r['offer_rate_pw_gbp']:
            continue
        key = (r['competitor'], float(r['size_sqft']))
        rack = float(r['rack_rate_pw_gbp']) if r['rack_rate_pw_gbp'] else None
        # remember the newest non-empty standard rate so a bare daily sweep row
        # (offer only) doesn't wipe the std value captured earlier.
        if rack is not None and (key not in sticky_rack or r['date'] >= sticky_rack[key][0]):
            sticky_rack[key] = (r['date'], rack)
        # tiebreak_key: prefer the real scraped_at timestamp (empty string
        # for old rows without it sorts first, i.e. loses to any row that
        # DOES have a real timestamp -- reasonable, since a real timestamp
        # is strictly more trustworthy evidence of recency than its absence)
        tiebreak_key = (r.get('scraped_at') or '', idx)
        val = (r['date'], tiebreak_key, float(r['offer_rate_pw_gbp']), rack, r['promo_text'] or '')
        cur = latest.get(key)
        if cur is None or val[0] > cur[0] or (val[0] == cur[0] and val[1] > cur[1]):
            latest[key] = val
    grid = defaultdict(dict)
    for (comp, size), (_, _, price, rack, promo) in latest.items():
        if rack is None and (comp, size) in sticky_rack:
            rack = sticky_rack[(comp, size)][1]  # inherit last known standard rate
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
    even when the real gap (vs. its true last-known row) is large.

    Keyed on (competitor, metric) rather than competitor alone: Safestore
    carries both 'manual_quote' (3-month term) and 'manual_quote_1yr' (12-
    month term) rows for the same size on the same date. Keying on
    competitor+size alone would let one metric's price silently overwrite
    the other in the per-date dict and report a false "price change" that's
    actually just two different contract lengths.
    """
    per_comp = defaultdict(lambda: defaultdict(dict))  # (comp,metric) -> date -> size -> offer
    for r in read_hist():
        if r['competitor'] in AUTOMATED_COMPETITORS and r['size_sqft'] and r['offer_rate_pw_gbp']:
            key = (r['competitor'], r['metric'])
            per_comp[key][r['date']][float(r['size_sqft'])] = float(r['offer_rate_pw_gbp'])
    changes = []
    for (comp, metric), by_date in per_comp.items():
        dates = sorted(by_date.keys())
        if len(dates) < 2:
            continue
        today_d, prev_d = dates[-1], dates[-2]
        label = comp if metric != 'manual_quote_1yr' else f'{comp} (1yr)'
        for size, v in by_date[today_d].items():
            pv = by_date[prev_d].get(size)
            if pv is not None and abs(pv - v) >= 0.01:
                changes.append(f'{label} {int(size)} sq ft: £{pv:.2f} → £{v:.2f}')
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


def build_recommendations(grid):
    """Compare Big Top's STANDARD (rack) rate against each rival's standard
    rate, size by size, and suggest pricing moves.

    Deliberately uses STANDARD rates, not the headline "Discounted" price:
    the discounted/offer price is often a teaser (e.g. "£1 first month",
    "50% off"), and comparing teaser depths across competitors who each run
    very different promo structures isn't a meaningful like-for-like signal
    for "should we change our price". The standard rate is what a customer
    actually pays on an ongoing basis once any intro offer ends -- a much
    fairer basis for a real pricing decision.

    Two categories, both size-by-size:
    - RAISE opportunity: Big Top's standard rate is comfortably below the
      cheapest rival's standard rate (>= RAISE_THRESHOLD_PCT gap). Suggests
      a new rate that still keeps Big Top the cheapest in the market (a
      safety margin below the cheapest rival), not matching or exceeding it.
    - AT-RISK: Big Top's standard rate is above the cheapest rival's by a
      meaningful margin -- flagged as a competitive risk, not something to
      act on blindly, but worth knowing.

    Sizes with no rival data at all are skipped (nothing to compare
    against). This is directional advice based on current scraped data,
    not a pricing algorithm -- always sanity-check before acting, and note
    a single day's rival price can itself be an intro/teaser rate for that
    rival's own promo cycle, not necessarily their true steady-state rate.
    """
    RAISE_THRESHOLD_PCT = 8.0   # only flag if BT is at least this % below cheapest rival
    RISK_THRESHOLD_PCT = 5.0    # only flag if BT is at least this % above cheapest rival
    SAFETY_MARGIN_PCT = 5.0     # suggested new rate stays this % below cheapest rival

    raise_ops = []
    at_risk = []

    for size in sorted(grid.keys()):
        entries = grid[size]
        bt_entry = entries.get('Big Top (own)')
        if bt_entry is None:
            continue
        bt_offer, bt_rack, _ = bt_entry
        bt_standard = bt_rack if bt_rack is not None else bt_offer

        rival_standards = {}
        for comp, (offer, rack, _) in entries.items():
            if comp == 'Big Top (own)':
                continue
            rival_standards[comp] = rack if rack is not None else offer
        if not rival_standards:
            continue

        cheapest_comp, cheapest_rate = min(rival_standards.items(), key=lambda kv: kv[1])
        if cheapest_rate <= 0:
            continue
        gap_pct = (cheapest_rate - bt_standard) / cheapest_rate * 100

        if gap_pct >= RAISE_THRESHOLD_PCT:
            suggested = cheapest_rate * (1 - SAFETY_MARGIN_PCT / 100)
            # round to nearest 50p, never suggest going down
            suggested = max(bt_standard, round(suggested * 2) / 2)
            raise_ops.append({
                'size': int(size), 'current': bt_standard, 'cheapest_comp': cheapest_comp,
                'cheapest_rate': cheapest_rate, 'gap_pct': gap_pct, 'suggested': suggested,
            })
        elif gap_pct <= -RISK_THRESHOLD_PCT:
            at_risk.append({
                'size': int(size), 'current': bt_standard, 'cheapest_comp': cheapest_comp,
                'cheapest_rate': cheapest_rate, 'gap_pct': -gap_pct,
            })

    return raise_ops, at_risk


def render_recommendations_html(raise_ops, at_risk):
    if not raise_ops and not at_risk:
        return ('<h3 style="margin:14px 0 4px;font-size:14px;">Recommended Changes</h3>'
                '<p style="margin:3px 0;font-size:12px;color:#555;">No sizes currently clear the '
                'threshold for a pricing recommendation either way.</p>')

    parts = ['<h3 style="margin:14px 0 4px;font-size:14px;">Recommended Changes</h3>']
    parts.append('<p style="margin:2px 0 8px;font-size:12px;color:#555;">Based on STANDARD '
                 '(post-promo) rates, not headline discounted/teaser prices -- a fairer '
                 'like-for-like comparison. Directional advice from current scraped data, '
                 'not a pricing algorithm: sanity-check before acting, and remember a '
                 "rival's rate shown here can itself be mid-promo.</p>")

    if raise_ops:
        parts.append('<p style="margin:6px 0 3px;font-size:13px;"><b>Consider raising:</b></p>')
        parts.append('<ul style="margin:0 0 8px;padding-left:20px;font-size:12px;">')
        for op in raise_ops:
            parts.append(
                '<li style="margin:2px 0;">%d sq ft: currently £%.2f/wk, %s%% below cheapest '
                'rival (%s at £%.2f/wk) &mdash; room to raise toward ~£%.2f/wk and still be the '
                'cheapest in the market.</li>' % (
                    op['size'], op['current'], f"{op['gap_pct']:.0f}",
                    escape(op['cheapest_comp'].replace(' Basildon', '').replace(' (Billericay)', '').replace(' (own)', '')),
                    op['cheapest_rate'], op['suggested'],
                )
            )
        parts.append('</ul>')

    if at_risk:
        parts.append('<p style="margin:6px 0 3px;font-size:13px;"><b>Priced above market (competitive risk):</b></p>')
        parts.append('<ul style="margin:0 0 4px;padding-left:20px;font-size:12px;">')
        for op in at_risk:
            parts.append(
                '<li style="margin:2px 0;">%d sq ft: currently £%.2f/wk, %s%% above cheapest '
                'rival (%s at £%.2f/wk).</li>' % (
                    op['size'], op['current'], f"{op['gap_pct']:.0f}",
                    escape(op['cheapest_comp'].replace(' Basildon', '').replace(' (Billericay)', '').replace(' (own)', '')),
                    op['cheapest_rate'],
                )
            )
        parts.append('</ul>')

    return ''.join(parts)


def freshness_status():
    """For each competitor (in GROUPS order), find the most recent date it
    has ANY valid price row in history.csv, and whether that date is today.
    Also reports how many distinct sizes were actually refreshed today out
    of how many sizes we've ever seen for that competitor (added 2026-09-15
    per Vikas's request) -- "fresh" alone doesn't say whether TODAY's
    refresh was a full sweep or just a couple of sizes that happened to get
    through (e.g. Safestore's reCAPTCHA-gated flow can partially succeed).

    Deliberately does NOT filter by PRICE_METRICS -- this should answer "did
    we get real fresh data for this competitor today", regardless of which
    metric it landed under (e.g. Safestore's manual_quote AND
    manual_quote_1yr both count as evidence the scraper actually ran
    successfully that day).

    This is the freshness check Vikas asked for after the 2026-09 Storage
    King/Safestore silent-staleness issue: those two are only refreshed by
    a separate machine's Mon/Wed/Fri push (Storage King) and have been
    broken more often than not (Safestore has had a genuine server-side
    fault on Safestore's own site since 2026-08-26; Storage King has an
    intermittent click-timeout). Without this note, a stale row re-read
    into today's grid looks identical to a genuinely fresh one -- this
    makes that distinction visible in the email itself instead of requiring
    a manual history.csv check every time.
    """
    latest_date_by_comp = {}
    sizes_seen_ever = {}  # comp -> set of sizes ever seen (any date)
    sizes_seen_today = {}  # comp -> set of sizes seen today specifically
    for r in read_hist():
        if not r['size_sqft'] or not r['offer_rate_pw_gbp']:
            continue
        comp = r['competitor']
        d = r['date']
        size = r['size_sqft']
        if comp not in latest_date_by_comp or d > latest_date_by_comp[comp]:
            latest_date_by_comp[comp] = d
        sizes_seen_ever.setdefault(comp, set()).add(size)
        if d == TODAY:
            sizes_seen_today.setdefault(comp, set()).add(size)
    results = []
    for comp, name, mark, dark, light in GROUPS:
        last_date = latest_date_by_comp.get(comp)
        is_fresh = (last_date == TODAY)
        total_sizes = len(sizes_seen_ever.get(comp, set()))
        today_sizes = len(sizes_seen_today.get(comp, set()))
        results.append({
            'name': name, 'last_date': last_date, 'is_fresh': is_fresh,
            'today_sizes': today_sizes, 'total_sizes': total_sizes,
        })
    return results


def render_freshness_html(freshness):
    parts = ['<h3 style="margin:14px 0 4px;font-size:14px;">Scrape Freshness</h3>']
    parts.append('<p style="margin:2px 0 6px;font-size:12px;color:#555;">Whether each '
                 'competitor\'s price data in this email came from a real scrape today, '
                 'or is being carried forward from an earlier date because today\'s scrape '
                 'failed or wasn\'t run for that competitor.</p>')
    parts.append('<ul style="margin:0 0 4px;padding-left:4px;font-size:12px;list-style:none;">')
    for r in freshness:
        icon = '\u2705' if r['is_fresh'] else '\u274c'
        date_str = r['last_date'] if r['last_date'] else 'no data ever recorded'
        if r['is_fresh']:
            # Show how many of the known sizes actually came through today,
            # not just that SOME data landed -- a partial refresh (e.g.
            # Safestore reCAPTCHA-gated) still counts as "fresh" for the
            # date check but is meaningfully incomplete.
            status = f"fresh today ({r['today_sizes']}/{r['total_sizes']} sizes)"
        else:
            status = f'stale, last fresh: {date_str}'
        parts.append(f'<li style="margin:2px 0;">{icon} <b>{escape(r["name"])}</b> &mdash; {status}</li>')
    parts.append('</ul>')
    return ''.join(parts)


def build_email(grid, summary_lines, footnotes, grid1yr=None, recommendations_html='', freshness_html=''):
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
{freshness_html}
{summary_html}
<table style="border-collapse:collapse;margin:10px 0;font-size:12px;">{h1}{h2}{rows}</table>
<h3 style="margin:14px 0 4px;font-size:14px;">Comments</h3>
{notes_html}
{recommendations_html}
</body></html>"""


def main():
    n, msg = append_today()
    n_ss, msg_ss = append_prebuilt(SAFESTORE_LATEST, 'Safestore')
    n_sk, msg_sk = append_prebuilt(STORAGEKING_LATEST, 'Storage King')
    changes = detect_changes()
    if changes:
        summary = ['<b>Changes today:</b> ' + '; '.join(changes[:8]) + ('…' if len(changes) > 8 else '')]
    else:
        summary = ['<b>No day-over-day price changes</b> in the automated sources '
                   '(Shurgard, Storage King, Safestore, Make Space, Big Top).']
    # Shurgard promo-expiry watch: surface the CURRENT rolling special-rate
    # end date, derived from today's/most-recent Shurgard row only -- not a
    # hardcoded date. (Bug found 2026-08-27: this used to hardcode
    # 'ends 21/08/2026' from the very first scrape on 2026-08-15. Shurgard's
    # promo end date is a rolling ~1-week window that slides forward on the
    # live site every few days -- it moved to 27/08/2026 by 2026-08-20 and
    # kept rolling since, but the hardcoded string match kept matching
    # against that one-time historical Aug 15 row forever, since old rows
    # never leave history.csv. Result: the email kept saying "ends 21 Aug
    # 2026" for a week after that date had passed and the site had moved on
    # three times over. Fixed to read the most recent Shurgard row's own
    # promo text and extract whatever date is actually there today.)
    shurgard_rows = [r for r in read_hist() if r['competitor'] == 'Shurgard Basildon']
    if shurgard_rows:
        latest_shurgard_date = max(r['date'] for r in shurgard_rows)
        latest_texts = ' '.join(
            r['notes'] + ' ' + r['promo_text']
            for r in shurgard_rows if r['date'] == latest_shurgard_date
        )
        m = re.search(r'ends (\d{2})/(\d{2})/(\d{4})', latest_texts)
        if m:
            dd, mm, yyyy = m.groups()
            try:
                month_name = datetime(int(yyyy), int(mm), int(dd)).strftime('%-d %b %Y')
            except ValueError:
                month_name = f'{dd}/{mm}/{yyyy}'
            summary.append(f'<b>Watch:</b> Shurgard special rates end {month_name} '
                            f'(per {latest_shurgard_date}\'s sweep -- this is a rolling '
                            f'promo window, check it hasn\'t moved again before relying on it).')
    footnotes = [
        '* Shurgard: swept daily; Discount/Duration parsed from the live web promo.',
        '** Storage King: swept daily as of 2026-08-26 (stealth Chromium, passes a one-time Cloudflare Turnstile checkbox — no CAPTCHA-solving). If a run is blocked or returns too few sizes to trust, the row is skipped and the last-known price carries forward instead.',
        '*** Safestore: swept daily as of 2026-08-26 via the full quote wizard with a marked test identity (stealth Chromium; robots.txt disallows these pages — an explicit, informed policy decision, see README). If every size comes back "call store" in one run (a block/rate-limit signature), that run is discarded and the last-known price carries forward instead.',
        '† Make Space (Billericay): swept daily; intro offers vary by unit — some sizes get no intro discount at all, so trust the per-size Discount cell, not a blanket headline.',
        '‡ Safestore 1yr columns: 12-month fixed-term quotes, swept daily as of 2026-08-26. "1yr promo" = intro weekly rate for the first 52 weeks; "1yr follow on rate" = ongoing discounted weekly rate. Carried forward until refreshed; blank where no 1-yr quote exists.',
        'Big Top: hybrid (v4, 25 Aug 2026) — sizes currently listed on bigtopselfstorage.com update daily from the live site; sizes not shown (out of stock) keep their last-known rate (15 Aug 2026 rate card baseline). Weekly figures outside £5–£600 are rejected as mis-parses and fall back to last-known too. Homepage vs /pricing lead-offer wording still differs (£1/6wks vs 50% off/8wks, unresolved) so the Discount/Duration columns still show fixed £1/6-week copy pending that decision. If a known Storeganise rate change doesn’t show up here, the live site likely hasn’t been republished yet — check the site, don’t assume the sweep is wrong.',
    ]
    grid = load_grid()
    grid1 = load_1yr()
    raise_ops, at_risk = build_recommendations(grid)
    recommendations_html = render_recommendations_html(raise_ops, at_risk)
    freshness = freshness_status()
    freshness_html = render_freshness_html(freshness)
    html = build_email(grid, summary, footnotes, grid1, recommendations_html, freshness_html)
    open(os.path.join(OUTDIR, 'email.html'), 'w').write(html)
    open(os.path.join(OUTDIR, 'subject.txt'), 'w').write(f'Basildon competitor prices — {TODAY}')
    stale = [f['name'] for f in freshness if not f['is_fresh']]
    print(f'{msg}; {msg_ss}; {msg_sk}; {len(changes)} changes; grid sizes={len(grid)}; '
          f'1yr sizes={len(grid1)}; recommendations: {len(raise_ops)} raise, {len(at_risk)} at-risk; '
          f'stale today: {stale if stale else "none"}')


if __name__ == '__main__':
    main()
