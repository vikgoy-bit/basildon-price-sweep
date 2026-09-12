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
    }


def main():
    observations = []
    warnings = []

    # Fixed 2026-09-12: previously launched a brand-new, completely empty
    # browser profile for EVERY size x duration x retry attempt (dozens per
    # run), each jumping straight to a deep get-a-quote link and submitting
    # within seconds. Vikas tested the identical flow manually via one
    # normal browsing session and it worked fine -- Safestore's server
    # isn't generally down. The most likely trigger for our repeated
    # HTTP 500s is that our traffic pattern looks nothing like a real
    # visitor: no browsing history, no session continuity, an
    # instantly-resubmitted identical identity, over and over.
    #
    # Now: ONE persistent browser profile/session for the entire run (all
    # sizes, both durations), same as a real person requesting quotes for
    # several sizes in one sitting would do. A fresh profile is only used
    # as a last resort if the whole session becomes unusable (crashed
    # context), not as the default per-attempt behaviour.
    profile_dir = f"{PROFILE_ROOT}/session"
    shutil.rmtree(profile_dir, ignore_errors=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            no_viewport=True,
            args=["--start-maximized"],
            proxy={"server": os.environ.get("SAFESTORE_PROXY", "")} if os.environ.get("SAFESTORE_PROXY") else None,
        )
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()

        for duration_key, radio_id, builder in [
            ("3months", DURATIONS["3months"], build_observation_3m),
            ("1year", DURATIONS["1year"], build_observation_1y),
        ]:
            fetched = 0
            call_store = 0
            server_errors = 0
            pending = []
            for size in AVAILABLE_SIZES:
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
                    server_errors += 1
                    warnings.append(f"Safestore {duration_key} size {size}: reCAPTCHA v3 rejected submission (surfaces as HTTP 500 fallback page) -- probabilistic bot-detection, not a genuine server fault; see code comments")
                    continue
                fetched += 1
                if data.get("noPriceMsg"):
                    call_store += 1
                    continue
                obs = builder(size, data)
                if obs:
                    pending.append(obs)
                else:
                    warnings.append(f"Safestore {duration_key} size {size}: fetched but could not parse price")

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

        ctx.close()
    shutil.rmtree(profile_dir, ignore_errors=True)

    out = {"observations": observations, "warnings": warnings}
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    out_path = os.path.join(data_dir, "safestore-latest.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Safestore sweep: {len(observations)} observations, {len(warnings)} warnings")
    for w in warnings:
        print("WARN:", w)


if __name__ == "__main__":
    main()
