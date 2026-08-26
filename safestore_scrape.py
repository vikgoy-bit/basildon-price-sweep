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


def accept_cookies(pg):
    try:
        pg.click("button:has-text('Accept all')", timeout=4000)
        pg.wait_for_selector("button:has-text('Accept all')", state="hidden", timeout=4000)
    except Exception:
        pass


def get_quote_for(pg, size, duration_radio_id):
    pg.goto(BASE_URL, timeout=60000)
    pg.wait_for_timeout(2200)
    accept_cookies(pg)
    pg.wait_for_timeout(700)

    pg.click("label[for=radio-personal-quote]", force=True)
    pg.wait_for_timeout(500)
    click_visible_next(pg)
    pg.wait_for_timeout(1500)

    clicked = pg.evaluate(f"""() => {{
        const nodes = Array.from(document.querySelectorAll('.c-quote__slide[data-enquiry-size]'));
        const target = nodes.find(n => n.getAttribute('data-enquiry-size')==='{size}' && !n.closest('.slick-cloned'));
        if(target){{ target.click(); return true; }}
        return false;
    }}""")
    if not clicked:
        return None
    pg.wait_for_timeout(800)
    click_visible_next(pg)
    pg.wait_for_timeout(1500)

    pg.click(f"label[for={duration_radio_id}]", force=True)
    pg.wait_for_timeout(600)
    click_visible_next(pg)
    pg.wait_for_timeout(1500)

    pg.click("label[for=radio-91-lead]", force=True)
    pg.wait_for_timeout(600)
    click_visible_next(pg)
    pg.wait_for_timeout(1500)

    pg.click("#inputFirstName"); pg.type("#inputFirstName", TEST_IDENTITY["firstName"], delay=60)
    pg.click("#inputSurname"); pg.type("#inputSurname", TEST_IDENTITY["lastName"], delay=60)
    pg.click("#inputEmail"); pg.type("#inputEmail", TEST_IDENTITY["email"], delay=60)
    pg.click("#inputPostcode"); pg.type("#inputPostcode", TEST_IDENTITY["postcode"], delay=60)
    pg.click("#inputContactNumber"); pg.type("#inputContactNumber", TEST_IDENTITY["phone"], delay=60)
    pg.wait_for_timeout(1200)

    yq = pg.query_selector("button:has-text('Your Quote')")
    if not yq:
        return None
    yq.click(force=True)
    pg.wait_for_timeout(5000)

    if "storage-quote" not in pg.url:
        pg.wait_for_timeout(3000)
        if "storage-quote" not in pg.url:
            return None

    return pg.evaluate(EXTRACT_JS)


def scrape_one(size, duration_radio_id, attempts=2):
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
            if data:
                return data
        except Exception as e:
            print(f"  size {size} attempt {attempt+1} exception: {e}", file=sys.stderr)
            shutil.rmtree(profile_dir, ignore_errors=True)
        time.sleep(6)
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
        pending = []
        for size in AVAILABLE_SIZES:
            data = scrape_one(size, radio_id)
            if data is None:
                warnings.append(f"Safestore {duration_key} size {size}: fetch failed after retries")
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
