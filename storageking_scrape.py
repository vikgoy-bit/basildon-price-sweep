#!/usr/bin/env python3
"""
Storage King Basildon quote-flow scraper (daily Actions sweep).

Loads the "select a size" -> "your price" quote flow. All sizes' prices
render in a single page load (no per-size form submission needed). A
one-time Cloudflare Turnstile checkbox appears on a fresh browser profile;
this script clicks it (a standard interaction checkbox -- no image/audio
CAPTCHA solving). No personal details are entered for this site's price
step.

Writes data/storageking-latest.json for build_report.py to fold into
history.csv. Degrades safely: a failed or implausibly-small fetch produces
no observations rather than fabricating rows, so history.csv naturally
falls back to the last-known prices.
"""
import json, os, re, shutil, sys, time
from patchright.sync_api import sync_playwright

BASE_URL = 'https://www.storageking.co.uk/get-a-quote/select-a-size/?store=basildon'
PROFILE_ROOT = "/tmp/storageking-sweep-profiles"

# Sizes actually offered at Basildon (verified 2026-08-26). Refresh manually
# if Storage King changes its unit mix at this store.
KNOWN_SIZES = [10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 75, 100, 125, 135, 150, 175, 200, 225, 250, 300]

EXTRACT_JS = """() => {
    const opts = Array.from(document.querySelectorAll('.store-storage-unit-option'));
    return opts.map(o => {
        const sizeEl = o.querySelector('p.is-blue.is-size-3, p.is-size-3');
        const priceEl = o.querySelector('p.title');
        const promoTextEl = Array.from(o.querySelectorAll('p')).find(p => /off your first|thereafter/i.test(p.textContent));
        return {
            sizeText: sizeEl ? sizeEl.textContent.trim() : null,
            priceText: priceEl ? priceEl.textContent.trim() : null,
            promoText: promoTextEl ? promoTextEl.textContent.trim() : null,
        };
    });
}"""


def scrape_all(attempts=3):
    for attempt in range(attempts):
        profile_dir = f"{PROFILE_ROOT}/attempt-{attempt}"
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
                pg.goto(BASE_URL, timeout=60000)
                pg.wait_for_timeout(2500)

                try:
                    frame = pg.frame_locator("iframe[src*='challenges.cloudflare.com']").first
                    frame.locator("input[type=checkbox], .cb-lb, #challenge-stage").first.click(timeout=5000, force=True)
                    pg.wait_for_timeout(4000)
                except Exception:
                    pass

                try:
                    btn = pg.get_by_role("button", name=re.compile("accept", re.I))
                    btn.first.click(timeout=5000, force=True)
                except Exception:
                    pass
                pg.wait_for_timeout(1000)

                pg.click("label:has(#SizeId_50)", force=True)
                pg.wait_for_timeout(800)
                btn = pg.get_by_role("button", name="Continue")
                btn.first.click(force=True)
                pg.wait_for_timeout(3000)

                data = pg.evaluate(EXTRACT_JS)
                ctx.close()
            shutil.rmtree(profile_dir, ignore_errors=True)
            if data:
                return data
        except Exception as e:
            print(f"  attempt {attempt+1} exception: {e}", file=sys.stderr)
            shutil.rmtree(profile_dir, ignore_errors=True)
        time.sleep(8)
    return None


def build_observation(size, item):
    price_text = item.get("priceText")
    promo_text = item.get("promoText")
    if not price_text or not promo_text:
        return None
    m_offer = re.search(r"£\s*([\d.]+)", price_text)
    m_rack = re.search(r"then £\s*([\d.]+) per week", promo_text)
    if not m_offer or not m_rack:
        return None
    offer = float(m_offer.group(1))
    rack = float(m_rack.group(1))
    promo_desc = promo_text.split(", then")[0].strip()
    return {
        "competitor": "Storage King Basildon", "metric": "manual_quote",
        "size_sqft": size, "rack_rate": rack, "offer_rate": offer,
        "per": "week", "promo": promo_desc + ", billed monthly",
        "source": BASE_URL,
        "notes": "Daily Actions sweep (VAT inc, excl padlock/insurance)",
    }


def main():
    observations = []
    warnings = []

    data = scrape_all()
    if data is None:
        warnings.append("Storage King: fetch failed after retries; no observations this run.")
        out = {"observations": [], "warnings": warnings}
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "storageking-latest.json"), "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print("Storage King sweep: 0 observations, 1 warning")
        print("WARN:", warnings[0])
        return

    by_size = {}
    for item in data:
        st = item.get("sizeText")
        if not st:
            continue
        try:
            size = int(st.split()[0])
        except (ValueError, IndexError):
            continue
        by_size[size] = item

    for size in KNOWN_SIZES:
        item = by_size.get(size)
        if item is None:
            continue
        obs = build_observation(size, item)
        if obs:
            observations.append(obs)
        else:
            warnings.append(f"Storage King size {size}: fetched but could not parse price")

    # Safety valve: if we got essentially nothing back (e.g. page structure
    # changed or a block page slipped through), don't wipe good history.
    if len(observations) < max(3, len(KNOWN_SIZES) // 3):
        warnings.append(
            f"Storage King: only {len(observations)}/{len(KNOWN_SIZES)} sizes parsed "
            "-- likely a bad fetch. Discarding results; history keeps last-known prices."
        )
        observations = []

    out = {"observations": observations, "warnings": warnings}
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "storageking-latest.json"), "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print(f"Storage King sweep: {len(observations)} observations, {len(warnings)} warnings")
    for w in warnings:
        print("WARN:", w)


if __name__ == "__main__":
    main()
