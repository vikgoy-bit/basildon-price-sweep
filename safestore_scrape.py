#!/usr/bin/env python3
"""
Safestore Basildon quote-flow scraper (daily Actions sweep).

Steps through the full quote wizard for every unit size the store actually
offers, for both a 3-month and a 12-month term, and writes the results to
data/safestore-latest.json for build_report.py to fold into history.csv.

Policy: see README.md "Policy note (2026-08-26)" — Safestore's robots.txt
disallows the pages this script reads; the repo owner made an informed,
explicit decision to automate it anyway. This script does not solve
CAPTCHAs, rotate IPs, or use residential proxies — it uses a stealth
Chromium build (patchright) to avoid the plain-headless fingerprint that
trips reCAPTCHA v3's bot score, and otherwise behaves like a real user
filling in a real form with a clearly-marked test identity.

Degrades safely: if a size's fetch fails after retries, or if EVERY size
comes back with no price (a strong signal of a block/rate-limit rather than
a real simultaneous withdrawal of every unit), this script emits no
observations for that duration rather than fabricating "not available"
rows — build_report.py's history.csv-based grid then naturally falls back
to the last-known row for anything not refreshed today.
"""
import json, os, re, shutil, sys, time
import random
from datetime import datetime, timezone
from patchright.sync_api import sync_playwright

BASE_URL = 'https://www.safestore.co.uk/get-a-quote/?siteid=0USAFESTORE-BASILD&returnurl=%2Fresults%2F%3Ftype%3Dlocation%26title%3Dbasildon'

# Fixed 2026-09-12 (v2/v3, identity content): Vikas hypothesized the
# repeated HTTP 500s / reCAPTCHA rejections were caused by the CONTENT of
# our test identity -- "Test Test", an email containing "test" or a "+tag",
# and postcode "TS1 1ST" are all textbook spam/bot-form signals. We tested
# this directly: swapping "Test Test"/"+tag" for a realistic-looking name,
# a Gmail dot-address (see below), and a genuine-format UK postcode/mobile
# DID let some submissions through that had been failing consistently for
# weeks.
#
# HONEST CAVEAT (do not overstate this fix): a follow-up 19-submission A/B
# test the same day, holding the identity STYLE constant and varying only
# browser instance/size/timing, came back roughly 50/50 (10 success / 9
# reCAPTCHA-failed) with no clean deterministic pattern by browser
# freshness, size, or timing. This is consistent with how reCAPTCHA v3
# actually works -- it returns a continuous, noisy risk score and the site
# picks a threshold, so automated traffic lands in a probabilistic middle
# band rather than a cleanly on/off gate. The identity-realism change is a
# genuine improvement (definitely doesn't hurt, plausibly helps some), but
# there is NO known change that makes this 100% reliable, and future
# maintainers should not assume one exists. With scrape_one()'s built-in
# 3-attempt retry, an independent ~50% per-attempt success rate works out
# to roughly 85-90% success per size per run -- "usually works, sometimes
# doesn't" is the correct expectation to set, not "fixed".
_FIRST_NAMES = ['Oliver', 'James', 'Daniel', 'Thomas', 'Harry', 'Jack', 'Charlie',
                'Emily', 'Sophie', 'Amelia', 'Grace', 'Chloe', 'Lucy', 'Hannah']
_LAST_NAMES = ['Bennett', 'Foster', 'Hughes', 'Palmer', 'Reid', 'Sharp', 'Walsh',
               'Carter', 'Marsh', 'Ellis', 'Grant', 'Doyle', 'Fox', 'Chapman']
# Real Essex-area outward postcode prefixes (Basildon store's own catchment
# area) paired with a plausible-looking inward code -- looks like a normal
# local customer, not an obviously synthetic "TS1 1ST" test value.
_POSTCODE_PREFIXES = ['SS14', 'SS13', 'SS15', 'SS16', 'CM11', 'CM12', 'RM10', 'RM17']

_first = random.choice(_FIRST_NAMES)
_last = random.choice(_LAST_NAMES)
_email_num = random.randint(1000, 9999)
_postcode = f"{random.choice(_POSTCODE_PREFIXES)} {random.randint(1,9)}{random.choice('ABDEFGHJLNPQRSTUWXYZ')}{random.choice('ABDEFGHJLNPQRSTUWXYZ')}"
_phone_suffix = f"{random.randint(0, 99999999):08d}"

_email_local = 'baas123123'
_dot_pos = random.randint(2, len(_email_local) - 2)
_email_with_dot = _email_local[:_dot_pos] + '.' + _email_local[_dot_pos:]

TEST_IDENTITY = {
    "firstName": _first,
    "lastName": _last,
    # IMPORTANT: must route to an inbox we actually control, never a
    # guessed real person's address. Avoids "+tag" addressing (a
    # well-known disposable/tracking-email signal some anti-fraud systems
    # flag) by using Gmail's dot-insensitivity instead: gmail.com ignores
    # dots in the local-part, so inserting one at a random position still
    # delivers to the exact same real inbox (baas123123@gmail.com) while
    # looking like an ordinary address to the receiving site.
    "email": os.environ.get("SWEEP_EMAIL", f"{_email_with_dot}@gmail.com"),
    "postcode": os.environ.get("SWEEP_POSTCODE", _postcode),
    "phone": os.environ.get("SWEEP_PHONE", f"079{_phone_suffix}"),
}

# Confirmed real sizes Basildon offers (verified 2026-08-26). If Safestore
# changes its unit mix at this store, this list needs a manual refresh —
# sizes not in this list are simply never attempted (no false "not
# available" claims about sizes that were never checked).
AVAILABLE_SIZES = [10, 16, 25, 35, 50, 75, 100, 125, 150, 175, 200, 250, 500]

DURATIONS = {
    "3months": "radio-90-quote",
    "1year": "radio-365-quote",
}

PROFILE_ROOT = "/tmp/safestore-sweep-profiles"

EXTRACT_JS = """() => {
    const priceBoxes = Array.from(document.querySelectorAll('.c-storage-quote__buy-price')).map(e => {
        const h3 = e.querySelector('h3');
        const label = h3 ? h3.textContent.trim() : null;
        const strongEls = Array.from(e.querySelectorAll('strong'));
        const strongs = strongEls.map(s => s.textContent.trim());
        return {label, strongs};
    });
    const info = document.querySelector('.c-storage-quote__buy__info');
    let headline = null, redLine = null, thenLine = null;
    if (info) {
        const priceSpan = info.querySelector('#price, .u-price--highlight');
        headline = priceSpan ? priceSpan.textContent.trim() : null;
        const redP = info.querySelector('p.text-base.u-text-color--red, p.u-text-color--red');
        redLine = redP ? redP.textContent.trim() : null;
        const smallEl = info.querySelector('small');
        thenLine = smallEl ? smallEl.textContent.trim() : null;
    }
    const noPriceMsg = document.body.textContent.includes('Thanks for your quote request');
    return {priceBoxes, headline, redLine, thenLine, noPriceMsg};
}"""


def click_visible_next(pg):
    loc = pg.locator("button[data-enquiry-nav-next]")
    for i in range(loc.count()):
        el = loc.nth(i)
        if el.is_visible():
            el.click(force=True)
            return True
    return False


def safe_click_label(pg, selector, timeout=15000):
    """Click a label by selector, waiting for it to actually become
    visible/attached first instead of relying on a fixed sleep before the
    click. Root cause fix (2026-09-05): the old code paired
    wait_for_timeout() with click(force=True), which skips Playwright's
    normal actionability checks -- when a wizard step's fade-in/slide
    animation ran even slightly slower than the hardcoded delay (varies by
    machine load, cold vs warm browser profile, first-run vs retry), the
    label would exist in the DOM but not yet be visible, and force=True
    would still attempt the click anyway and raise 'Element is not
    visible'. That single click failure aborted the whole quote for that
    size, which is why a single bad night could plausibly wipe out most or
    all size/duration combinations at once (looks exactly like a site
    block from the outside, but confirmed via live debugging on
    2026-09-05 that the site itself was serving a completely normal,
    unblocked quote flow the whole time).
    Fix: wait_for_selector(state='visible') before clicking, and drop
    force=True so Playwright's built-in actionability checks (visible,
    stable, receives events, enabled) do the waiting instead of a guess.
    One retry with a fresh wait if the first attempt still times out,
    since occasionally the reveal genuinely just needs a bit longer.
    """
    try:
        pg.wait_for_selector(selector, state="visible", timeout=timeout)
    except Exception:
        pg.wait_for_selector(selector, state="visible", timeout=timeout)
    pg.click(selector)


def accept_cookies(pg):
    try:
        pg.click("button:has-text('Accept all')", timeout=4000)
        pg.wait_for_selector("button:has-text('Accept all')", state="hidden", timeout=4000)
    except Exception:
        pass


def get_quote_for(pg, size, duration_radio_id, debug=False):
    pg.goto(BASE_URL, timeout=60000)
    pg.wait_for_load_state("domcontentloaded")
    accept_cookies(pg)

    safe_click_label(pg, "label[for=radio-personal-quote]")
    click_visible_next(pg)
    if debug: print("  [debug] passed Type step")

    # Size step: wait for the carousel to actually render before querying it
    pg.wait_for_selector(".c-quote__slide[data-enquiry-size]", state="visible", timeout=15000)
    clicked = pg.evaluate(f"""() => {{
        const nodes = Array.from(document.querySelectorAll('.c-quote__slide[data-enquiry-size]'));
        const target = nodes.find(n => n.getAttribute('data-enquiry-size')==='{size}' && !n.closest('.slick-cloned'));
        if(target){{ target.click(); return true; }}
        return false;
    }}""")
    if not clicked:
        if debug: print("  [debug] FAILED at size click (target size not found)")
        return None
    # The size click above is a raw JS .click() dispatched via evaluate()
    # (needed to reliably hit the correct carousel slide, since the visible
    # one can be a '.slick-cloned' duplicate that Playwright's own locator
    # API would happily click without erroring, just on the wrong element).
    # Because it's a JS-dispatched event rather than a real Playwright
    # action, it doesn't participate in Playwright's auto-wait/actionability
    # tracking -- the site's own JS needs a brief moment to react (enable
    # the size's selected state, wire up the next step's content) before
    # "Next" is clicked. Root-caused via live debugging 2026-09-05: without
    # this pause, clicking Next immediately after the JS click can advance
    # the wizard's step indicator before the site has actually activated the
    # chosen size, landing on a Duration step where every duration label
    # exists in the DOM but is still hidden (misread for over a week as the
    # site blocking/rate-limiting the scraper -- it wasn't; screenshots
    # confirmed a completely normal, unblocked flow throughout).
    pg.wait_for_timeout(400)
    click_visible_next(pg)
    if debug: print("  [debug] passed Size step")

    safe_click_label(pg, f"label[for={duration_radio_id}]")
    click_visible_next(pg)
    if debug: print("  [debug] passed Duration step")

    safe_click_label(pg, "label[for=radio-91-lead]")
    click_visible_next(pg)
    if debug: print("  [debug] passed When step")

    pg.wait_for_selector("#inputFirstName", state="visible", timeout=15000)
    pg.click("#inputFirstName"); pg.type("#inputFirstName", TEST_IDENTITY["firstName"], delay=60)
    pg.click("#inputSurname"); pg.type("#inputSurname", TEST_IDENTITY["lastName"], delay=60)
    pg.click("#inputEmail"); pg.type("#inputEmail", TEST_IDENTITY["email"], delay=60)
    pg.click("#inputPostcode"); pg.type("#inputPostcode", TEST_IDENTITY["postcode"], delay=60)
    pg.click("#inputContactNumber"); pg.type("#inputContactNumber", TEST_IDENTITY["phone"], delay=60)
    if debug: print("  [debug] filled details form")

    yq = pg.query_selector("button:has-text('Your Quote')")
    if not yq:
        if debug: print("  [debug] FAILED: 'Your Quote' button not found")
        return None
    # Fixed 2026-09-12: force=True bypasses Playwright's normal
    # actionability checks AND can change how the click event is
    # dispatched. Confirmed via live network-response debugging that
    # force-clicking this specific button caused a full-page navigation
    # to /Error/HandleError/500 (Safestore's own server-side error page,
    # tracked by a full pageview beacon), whereas a normal (non-forced)
    # click that lets the site's own JS handler intercept the click
    # fires an ordinary AJAX POST to /personaldetailsstep instead --
    # the page's actual designed submission path. Dropping force=True
    # so Playwright waits for the button to be genuinely clickable and
    # dispatches a real click event the page's own handler can catch.
    yq.click()
    pg.wait_for_timeout(5000)
    if debug: print("  [debug] clicked Your Quote, URL now:", pg.url)

    # Distinguish a genuine Safestore-side server fault from a normal
    # in-flight redirect. Found via live debugging 2026-09-05: for over a
    # week every single fetch failed with the generic "fetch failed after
    # retries" message, which looked exactly like a bot block/rate-limit
    # from the outside. Traced it step by step and found the ENTIRE wizard
    # (Type -> Size -> Duration -> When -> Details) was working perfectly
    # every time -- no captcha, no block page, completely normal site
    # behaviour -- right up until the final "Your Quote" submission, which
    # was landing on https://www.safestore.co.uk/Error/HandleError/500
    # (a real HTTP 500, Safestore's own server-side error page) instead of
    # redirecting to the results page. This is a fault on Safestore's end,
    # not something fixable in this scraper -- flag it distinctly so a
    # future run/maintainer doesn't waste time re-diagnosing it as a block.
    if "Error/HandleError/500" in pg.url:
        if debug: print("  [debug] Safestore server error (HTTP 500) on submission -- not a block, a real site-side fault")
        return {"safestore_server_error": True}

    if "storage-quote" not in pg.url:
        pg.wait_for_timeout(3000)
        if "storage-quote" not in pg.url:
            if "Error/HandleError/500" in pg.url:
                if debug: print("  [debug] Safestore server error (HTTP 500) on submission -- not a block, a real site-side fault")
                return {"safestore_server_error": True}
            if debug: print("  [debug] FAILED: URL never redirected to storage-quote. Final URL:", pg.url)
            return None

    return pg.evaluate(EXTRACT_JS)


def scrape_one(pg, size, duration_radio_id, attempts=3):
    """Run the quote flow for one size/duration using the given (already
    open) page, retrying by reloading the SAME page/session rather than
    tearing down and relaunching a brand-new empty browser profile each
    time. See main() for why: a fresh empty profile per attempt is a much
    stronger bot signal than a real user's single browsing session."""
    last_server_error = False
    for attempt in range(attempts):
        try:
            data = get_quote_for(pg, size, duration_radio_id)
            if data and data.get("safestore_server_error"):
                last_server_error = True
                time.sleep(6)
                continue
            if data:
                return data
        except Exception as e:
            print(f"  size {size} attempt {attempt+1} exception: {e}", file=sys.stderr)
        time.sleep(6)
    if last_server_error:
        return {"safestore_server_error": True}
    return None


def money(s):
    if not s:
        return None
    return s.replace("£", "").strip()


def to_float(s):
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def build_observation_3m(size, data):
    if data is None or data.get("noPriceMsg"):
        return None
    pb = data.get("priceBoxes") or []
    standard = next((b["strongs"][0] for b in pb if b.get("label") == "Standard Price" and b.get("strongs")), None)
    headline = data.get("headline")
    redLine = data.get("redLine")
    thenLine = data.get("thenLine")
    if not standard or not headline:
        return None
    if "£1 for the first" in headline:
        promo_offer = 1.00
        promo_text = headline.replace("🔥 ", "").strip()
    else:
        promo_offer = to_float(money(headline))
        if promo_offer is None:
            return None
        promo_text = redLine or thenLine or ""
    rack = to_float(money(standard))
    if rack is None:
        return None
    return {
        "competitor": "Safestore Basildon", "metric": "manual_quote",
        "size_sqft": size, "rack_rate": rack, "offer_rate": promo_offer,
        "per": "week", "promo": promo_text,
        "source": BASE_URL,
        "notes": "Daily Actions sweep (Personal, 3mo, inc VAT, excl StoreProtect/padlock, new customers)",
        # Added 2026-09-16 after finding a real staleness bug: build_report.py
        # used to stamp EVERY row it read with "today's date" regardless of
        # when the underlying observation was actually captured. Since a
        # good observation intentionally carries forward in
        # safestore-latest.json across ticks/days that don't get a fresh
        # price (see tick_main()'s merge logic), this silently relabeled
        # 2-day-old data as "fresh today" for THREE consecutive days
        # (2026-09-14 through -16) before being caught. This field lets
        # build_report.py use the observation's REAL capture date instead.
        "scraped_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        # Added 2026-09-16 per Vikas: a real timestamp (not just a date) so
        # same-day tiebreaking in build_report.py's load_grid() has an
        # explicit signal instead of relying only on file-append order.
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_observation_1y(size, data):
    if data is None or data.get("noPriceMsg"):
        return None
    pb = data.get("priceBoxes") or []
    discounted = next((b["strongs"][0] for b in pb if b.get("label") == "Discounted Price" and b.get("strongs")), None)
    headline = data.get("headline")
    redLine = data.get("redLine")
    if not discounted or not headline:
        return None
    promo_offer = to_float(money(headline))
    rack = to_float(money(discounted))
    if promo_offer is None or rack is None:
        return None
    return {
        "competitor": "Safestore Basildon", "metric": "manual_quote_1yr",
        "size_sqft": size, "rack_rate": rack, "offer_rate": promo_offer,
        "per": "week", "promo": redLine or "",
        "source": BASE_URL,
        "notes": "Daily Actions sweep (Personal, 1yr, inc VAT, excl StoreProtect/padlock, new customers)",
        "scraped_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


TICK_PROGRESS_PATH = "/opt/data/profiles/alex/state/safestore-tick-progress.json"


def _all_work_items():
    items = []
    for duration_key, radio_id, builder in [
        ("3months", DURATIONS["3months"], build_observation_3m),
        ("1year", DURATIONS["1year"], build_observation_1y),
    ]:
        for size in AVAILABLE_SIZES:
            items.append((duration_key, radio_id, builder, size))
    return items


def _load_tick_progress():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(TICK_PROGRESS_PATH):
        with open(TICK_PROGRESS_PATH) as f:
            state = json.load(f)
        if state.get("date") == today:
            return state
    # New day (or no progress file yet) -- start fresh.
    return {"date": today, "done": []}


def _save_tick_progress(state):
    os.makedirs(os.path.dirname(TICK_PROGRESS_PATH), exist_ok=True)
    with open(TICK_PROGRESS_PATH, "w") as f:
        json.dump(state, f, indent=2)


def tick_main(count=3):
    """Added 2026-09-15 per Vikas: instead of one big burst of 26
    submissions in ~15 minutes (which the reCAPTCHA-taint testing that same
    day showed is itself a big part of the problem -- no real visitor
    requests 26 quotes back-to-back), spread the day's sweep across many
    small hourly ticks that each submit only a couple of quotes, closer to
    genuine organic traffic volume/pacing.

    Each invocation submits up to `count` of the day's not-yet-done
    (duration, size) work items (tracked in TICK_PROGRESS_PATH, which
    resets automatically at UTC midnight), using ONE fresh browser session
    for the whole tick (count is small enough that no BATCH_SIZE rotation
    is needed within a single tick -- a handful of quotes in one sitting is
    exactly what a real visitor comparing sizes would do).

    Merges results into the EXISTING data/safestore-latest.json rather than
    overwriting it (a full run's main() always overwrites the whole file --
    that's correct there since it always covers everything in one go, but
    would wipe earlier ticks' progress here). Once a day's 26 items are all
    done, subsequent ticks that day are a fast no-op (checked before
    launching any browser, so a "nothing to do" tick costs nothing).
    """
    progress = _load_tick_progress()
    done_keys = set(progress["done"])
    all_items = _all_work_items()
    remaining = [item for item in all_items if f"{item[0]}:{item[3]}" not in done_keys]

    if not remaining:
        print(f"Safestore tick: today's full sweep ({len(all_items)} items) already complete -- nothing to do.")
        return

    batch = remaining[:count]
    print(f"Safestore tick: {len(remaining)} items remaining today, attempting {len(batch)} this run.")

    existing = {"observations": [], "warnings": []}
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    out_path = os.path.join(data_dir, "safestore-latest.json")
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                existing = json.load(f)
        except Exception:
            pass
    # Index existing observations by (metric, size) so we can replace only
    # the ones this tick actually refreshes, keeping everything else from
    # earlier ticks today untouched.
    obs_by_key = {}
    for obs in existing.get("observations", []):
        obs_by_key[(obs.get("metric"), obs.get("size_sqft"))] = obs
    metric_by_duration = {"3months": "manual_quote", "1year": "manual_quote_1yr"}

    tick_warnings = []

    with sync_playwright() as p:
        profile_dir, ctx, pg = new_session(p)
        try:
            for duration_key, radio_id, builder, size in batch:
                try:
                    data = scrape_one(pg, size, radio_id)
                except Exception as e:
                    print(f"  session page recovery after error: {e}", file=sys.stderr)
                    try:
                        pg.close()
                    except Exception:
                        pass
                    pg = ctx.new_page()
                    data = None

                key = (metric_by_duration[duration_key], size)
                if data is None:
                    tick_warnings.append(f"Safestore {duration_key} size {size}: fetch failed after retries")
                    done_keys.add(f"{duration_key}:{size}")  # don't retry endlessly within today
                    continue
                if data.get("safestore_server_error"):
                    tick_warnings.append(f"Safestore {duration_key} size {size}: reCAPTCHA v3 rejected submission (surfaces as HTTP 500 fallback page) -- probabilistic bot-detection, not a genuine server fault; see main()'s code comments")
                    done_keys.add(f"{duration_key}:{size}")
                    continue
                if data.get("noPriceMsg"):
                    tick_warnings.append(f"Safestore {duration_key} size {size}: 'call store' returned -- not written (ambiguous: could be a real stock-out or a soft block)")
                    done_keys.add(f"{duration_key}:{size}")
                    continue
                obs = builder(size, data)
                if obs:
                    obs_by_key[key] = obs
                    done_keys.add(f"{duration_key}:{size}")
                else:
                    tick_warnings.append(f"Safestore {duration_key} size {size}: fetched but could not parse price")
                    done_keys.add(f"{duration_key}:{size}")
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)

    out = {"observations": list(obs_by_key.values()), "warnings": tick_warnings}
    os.makedirs(data_dir, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    progress["done"] = sorted(done_keys)
    _save_tick_progress(progress)

    total_done = len([k for k in done_keys])
    print(f"Safestore tick complete: {total_done}/{len(all_items)} items done today so far "
          f"({len(obs_by_key)} observations in latest.json). {len(tick_warnings)} warning(s) this tick.")
    for w in tick_warnings:
        print("WARN:", w)


def new_session(p):
    profile_dir = f"{PROFILE_ROOT}/session-{int(time.time()*1000)}"
    shutil.rmtree(profile_dir, ignore_errors=True)
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=False,
        no_viewport=True,
        args=["--start-maximized"],
        proxy={"server": os.environ.get("SAFESTORE_PROXY", "")} if os.environ.get("SAFESTORE_PROXY") else None,
    )
    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
    return profile_dir, ctx, pg


# Added 2026-09-16 per Vikas, after confirming a full 26/26 clean run is
# achievable via a residential IP (see README.md "Why the daily Safestore
# scrape routes through a Tailscale exit node" for the full story): once
# reCAPTCHA v3 stops being the bottleneck, running the FULL 26-item sweep
# more than once a day serves no purpose -- prices don't change that
# often, and it's needless load on Safestore's real lead-capture form
# (each run is 26 genuine form submissions with fabricated contact
# details, even though they route to our own inbox).
#
# LAST_RUN_PATH tracks the most recent full-sweep OUTCOME (not just that
# it ran). A second invocation within 24h of a run that came back clean
# is skipped entirely (no browser launched, no submissions sent). If the
# last run had an issue (looked blocked/rate-limited, or came back with
# unusually few observations), the 24h cooldown does NOT apply -- retrying
# sooner is exactly what you want when the previous attempt likely failed
# for reasons that might not repeat (see the reCAPTCHA-v3-is-noisy note
# above). Bypass entirely with --force (e.g. for manual/live testing).
LAST_RUN_PATH = "/opt/data/profiles/alex/state/safestore-last-full-run.json"
FULL_RUN_COOLDOWN_HOURS = 24
# A run counts as "ok" (subject to the cooldown) only if it got a healthy
# majority of the 26 possible observations AND didn't trip either of the
# "looks blocked" safety valves below. Anything short of that is an
# "issue" -- always safe to retry immediately.
_MIN_OK_OBSERVATIONS = 20  # out of a possible 26 (13 sizes x 2 durations)


def _load_last_run():
    if not os.path.exists(LAST_RUN_PATH):
        return None
    try:
        with open(LAST_RUN_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _save_last_run(outcome, observations, warnings):
    os.makedirs(os.path.dirname(LAST_RUN_PATH), exist_ok=True)
    with open(LAST_RUN_PATH, "w") as f:
        json.dump({
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "outcome": outcome,  # "ok" or "issue"
            "observations": observations,
            "warnings": warnings,
        }, f, indent=2)


def _cooldown_active():
    """Returns (active: bool, reason: str) -- active=True means skip this run."""
    last = _load_last_run()
    if last is None:
        return False, "no previous run on record"
    if last.get("outcome") != "ok":
        return False, f"previous run ({last.get('timestamp')}) was flagged 'issue' -- retry always allowed"
    try:
        last_dt = datetime.strptime(last["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return False, "previous run timestamp unparseable -- treating as no record"
    elapsed_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    if elapsed_hours < FULL_RUN_COOLDOWN_HOURS:
        return True, (
            f"last clean full run was {elapsed_hours:.1f}h ago "
            f"({last['timestamp']}, {last.get('observations')} observations) -- "
            f"within the {FULL_RUN_COOLDOWN_HOURS}h cooldown, skipping"
        )
    return False, f"last clean run was {elapsed_hours:.1f}h ago -- cooldown expired"


def main(force=False):
    if not force:
        skip, reason = _cooldown_active()
        if skip:
            print(f"Safestore full sweep SKIPPED: {reason}")
            print("(pass --force to override, e.g. for manual/live testing)")
            return
        else:
            print(f"Safestore full sweep proceeding: {reason}")

    observations = []
    warnings = []

    # Fixed 2026-09-14 (v4): the v2/v3 "one session for the whole run" fix
    # (below history, kept for context) was itself wrong in a different way.
    # First real production run after it landed: sizes 10/16/25 (the FIRST
    # three submissions of the run) succeeded, then all 23 remaining
    # submissions in that same session failed. That's the reCAPTCHA-taint
    # pattern from same-day testing playing out exactly as predicted -- a
    # session's risk score degrades with repeated automated-looking
    # submissions and doesn't recover within that session. No real visitor
    # requests 26 quotes back-to-back in one sitting either, so "one huge
    # session" was still an obvious bot pattern, just a different one.
    #
    # Fix: cap how much taint any single browser session accumulates by
    # relaunching a FRESH context every BATCH_SIZE submissions (not every
    # single one -- that was the pre-2026-09-12 behaviour and looked even
    # more bot-like: zero browsing continuity, deep-link straight to
    # get-a-quote, submit, repeat). A handful of quotes per session is a
    # closer approximation of a real visitor comparing a few unit sizes in
    # one sitting.
    #
    # (v2/v3 history, 2026-09-12): originally a brand-new, completely empty
    # profile was launched for EVERY size x duration x retry attempt.
    # Switched to one shared session for the whole run, which improved
    # some things (removed force=True elsewhere, realistic identity) but
    # this specific "one huge session" design choice underperformed in
    # production -- see note above. BATCH_SIZE is the actual fix for that.
    BATCH_SIZE = 4

    work_items = []
    for duration_key, radio_id, builder in [
        ("3months", DURATIONS["3months"], build_observation_3m),
        ("1year", DURATIONS["1year"], build_observation_1y),
    ]:
        for size in AVAILABLE_SIZES:
            work_items.append((duration_key, radio_id, builder, size))

    # Per-duration counters for the existing safety-valve logic below.
    per_duration = {
        "3months": {"fetched": 0, "call_store": 0, "server_errors": 0, "pending": []},
        "1year": {"fetched": 0, "call_store": 0, "server_errors": 0, "pending": []},
    }

    with sync_playwright() as p:
        profile_dir, ctx, pg = new_session(p)

        for i, (duration_key, radio_id, builder, size) in enumerate(work_items):
            if i > 0 and i % BATCH_SIZE == 0:
                # Rotate to a fresh session/profile to cap accumulated taint.
                try:
                    ctx.close()
                except Exception:
                    pass
                shutil.rmtree(profile_dir, ignore_errors=True)
                profile_dir, ctx, pg = new_session(p)

            try:
                data = scrape_one(pg, size, radio_id)
            except Exception as e:
                # The shared page/context itself may occasionally end up
                # in a bad state (crashed target, closed page). Recover
                # by opening a fresh page in the SAME context/profile
                # (keeps session cookies/history) rather than aborting
                # the whole run.
                print(f"  session page recovery after error: {e}", file=sys.stderr)
                try:
                    pg.close()
                except Exception:
                    pass
                pg = ctx.new_page()
                data = None

            d = per_duration[duration_key]
            if data is None:
                warnings.append(f"Safestore {duration_key} size {size}: fetch failed after retries")
                continue
            if data.get("safestore_server_error"):
                # NOTE (corrected 2026-09-12): earlier debugging (2026-09-05)
                # concluded this HTTP 500 was a genuine fault on Safestore's
                # end, unrelated to bot detection. That was WRONG. Live
                # network instrumentation this session traced the actual
                # sequence: the wizard's AJAX submission to
                # /personaldetailsstep returns HTTP 400 {"message":
                # "recaptchaFailed"}, and the page's own client-side JS then
                # falls back to a traditional full-page form POST, which is
                # what lands on /Error/HandleError/500. So this 500 IS a
                # bot-detection outcome (reCAPTCHA v3 risk scoring), not an
                # independent server fault -- it just surfaces through a
                # generic error page instead of a clear "blocked" message.
                # Confirmed probabilistic/noisy (~50% per-attempt in a
                # 19-submission same-day test), not deterministically tied
                # to IP, identity content, or browser freshness alone --
                # see the TEST_IDENTITY comment above for the full test
                # history. Retrying (scrape_one()'s built-in 3 attempts)
                # genuinely helps here since each attempt is an independent
                # draw against that risk score, unlike a real outage where
                # retrying would be pointless.
                d["server_errors"] += 1
                warnings.append(f"Safestore {duration_key} size {size}: reCAPTCHA v3 rejected submission (surfaces as HTTP 500 fallback page) -- probabilistic bot-detection, not a genuine server fault; see code comments")
                continue
            d["fetched"] += 1
            if data.get("noPriceMsg"):
                d["call_store"] += 1
                continue
            obs = builder(size, data)
            if obs:
                d["pending"].append(obs)
            else:
                warnings.append(f"Safestore {duration_key} size {size}: fetched but could not parse price")

        try:
            ctx.close()
        except Exception:
            pass
        shutil.rmtree(profile_dir, ignore_errors=True)

    for duration_key, d in per_duration.items():
        fetched, call_store, server_errors, pending = d["fetched"], d["call_store"], d["server_errors"], d["pending"]

        # Safety valve: if every fetched size came back "call store", this
        # is almost certainly a block/rate-limit, not a genuine
        # simultaneous withdrawal of every unit. Discard the whole
        # duration's results so history.csv keeps yesterday's real
        # prices instead of getting wiped.
        if fetched > 0 and call_store == fetched:
            warnings.append(
                f"Safestore {duration_key}: ALL {fetched} fetched sizes returned "
                "'call store' -- likely blocked/rate-limited this run. "
                "Discarding results for this duration; history keeps last-known prices."
            )
            continue

        if server_errors == len(AVAILABLE_SIZES):
            warnings.append(
                f"Safestore {duration_key}: ALL {server_errors} sizes hit the reCAPTCHA/500 "
                "fallback this run -- an unusually bad run of the same probabilistic "
                "bot-detection scoring (see code comments above), not necessarily a "
                "genuine site outage. Nothing to fix in this scraper; a future run may "
                "score better -- there is no guaranteed fix on our side."
            )

        observations.extend(pending)

    out = {"observations": observations, "warnings": warnings}
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    out_path = os.path.join(data_dir, "safestore-latest.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Safestore sweep: {len(observations)} observations, {len(warnings)} warnings")
    for w in warnings:
        print("WARN:", w)

    # Record outcome for the 24h-cooldown check on the NEXT invocation.
    # "ok" (subject to cooldown) requires a healthy majority of possible
    # observations AND no active "looks blocked" safety-valve warning this
    # run. Anything else is "issue" -- next run won't be blocked by cooldown.
    blocked_signal = any(
        ("likely blocked/rate-limited" in w) or ("ALL " in w and "reCAPTCHA/500" in w)
        for w in warnings
    )
    outcome = "ok" if (len(observations) >= _MIN_OK_OBSERVATIONS and not blocked_signal) else "issue"
    _save_last_run(outcome, len(observations), warnings)
    print(f"Run outcome recorded: {outcome} ({len(observations)}/{len(AVAILABLE_SIZES) * 2} observations)")


if __name__ == "__main__":
    # Added 2026-09-15: `--tick[=N]` runs tick_main() (a small hourly
    # slice of the day's work, see its docstring) instead of the full
    # main() sweep. Kept as an opt-in flag rather than the default so the
    # existing Mon/Wed/Fri Hermes cron job (which expects one full run
    # covering all 26 items) is completely unaffected.
    #
    # Added 2026-09-16: `--force` bypasses the 24h same-day-cooldown check
    # in main() (see its comment) -- for manual/live testing only; the
    # scheduled cron invocation should never pass this.
    _force = "--force" in sys.argv
    if len(sys.argv) > 1 and sys.argv[1].startswith("--tick"):
        n = 3
        if "=" in sys.argv[1]:
            n = int(sys.argv[1].split("=", 1)[1])
        tick_main(count=n)
    else:
        main(force=_force)

