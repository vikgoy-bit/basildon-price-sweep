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
from patchright.sync_api import sync_playwright

BASE_URL = 'https://www.safestore.co.uk/get-a-quote/?siteid=0USAFESTORE-BASILD&returnurl=%2Fresults%2F%3Ftype%3Dlocation%26title%3Dbasildon'

TEST_IDENTITY = {
    "firstName": "Test",
    "lastName": "Test",
    "email": os.environ.get("SWEEP_EMAIL", "baas123123+test@gmail.com"),
    "postcode": "TS1 1ST",
    "phone": "07845412125",
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
    yq.click(force=True)
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


def scrape_one(size, duration_radio_id, attempts=3):
    last_server_error = False
    for attempt in range(attempts):
        profile_dir = f"{PROFILE_ROOT}/{duration_radio_id}-{size}-{attempt}"
        shutil.rmtree(profile_dir, ignore_errors=True)
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=False,
                    no_viewport=True,
                    args=["--start-maximized"],
                )
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                data = get_quote_for(pg, size, duration_radio_id)
                ctx.close()
            shutil.rmtree(profile_dir, ignore_errors=True)
            if data and data.get("safestore_server_error"):
                # Don't short-circuit on a server 500 the way we do for real
                # data -- it's plausibly transient, so keep retrying through
                # all attempts in case Safestore's backend recovers. Only
                # remember that this is why we're retrying, so if EVERY
                # attempt ends in a 500, we can report that specifically
                # instead of the generic "fetch failed after retries".
                last_server_error = True
                time.sleep(6)
                continue
            if data:
                return data
        except Exception as e:
            print(f"  size {size} attempt {attempt+1} exception: {e}", file=sys.stderr)
            shutil.rmtree(profile_dir, ignore_errors=True)
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

    for duration_key, radio_id, builder in [
        ("3months", DURATIONS["3months"], build_observation_3m),
        ("1year", DURATIONS["1year"], build_observation_1y),
    ]:
        fetched = 0
        call_store = 0
        server_errors = 0
        pending = []
        for size in AVAILABLE_SIZES:
            data = scrape_one(size, radio_id)
            if data is None:
                warnings.append(f"Safestore {duration_key} size {size}: fetch failed after retries")
                continue
            if data.get("safestore_server_error"):
                # Distinct from a bot block or a code bug: Safestore's own
                # server returned an HTTP 500 processing the submission.
                # Confirmed via live debugging 2026-09-05 -- the wizard
                # itself works fine every time (no captcha/block), it's
                # specifically the final "Your Quote" POST that 500s on
                # their end. Retrying immediately within the same run is
                # unlikely to help if their backend is genuinely down; the
                # per-size retry loop in scrape_one() already tries 3 times
                # with a 6s gap, so if it's still erroring after that,
                # move on rather than burning the whole run's time budget.
                server_errors += 1
                warnings.append(f"Safestore {duration_key} size {size}: Safestore server error (HTTP 500) on submission -- not a block, a fault on their end")
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

        # Safety valve: if every fetched size came back "call store", this is
        # almost certainly a block/rate-limit, not a genuine simultaneous
        # withdrawal of every unit. Discard the whole duration's results so
        # history.csv keeps yesterday's real prices instead of getting wiped.
        if fetched > 0 and call_store == fetched:
            warnings.append(
                f"Safestore {duration_key}: ALL {fetched} fetched sizes returned "
                "'call store' -- likely blocked/rate-limited this run. "
                "Discarding results for this duration; history keeps last-known prices."
            )
            continue

        if server_errors == len(AVAILABLE_SIZES):
            warnings.append(
                f"Safestore {duration_key}: ALL {server_errors} sizes hit Safestore's own "
                "HTTP 500 server error on submission -- their site appears to be down/broken "
                "for this quote flow right now, not a block on our end. Nothing to fix in this "
                "scraper; retry on the next scheduled run once Safestore's backend recovers."
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


if __name__ == "__main__":
    main()
