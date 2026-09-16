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
from datetime import datetime, timezone
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


def scrape_all(attempts=5):
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
                pg.wait_for_timeout(2000)

                # Fixed 2026-09-12 (same root cause as the Safestore fix a
                # week earlier): the old code did a fixed 4s sleep after
                # clicking the Cloudflare Turnstile checkbox, then blindly
                # force-clicked the size label. Turnstile's challenge
                # resolution time varies (sometimes well past 4s), so the
                # size selector often hadn't rendered yet, and force=True
                # suppressed Playwright's own actionability wait that would
                # otherwise have caught this.
                #
                # Fixed again 2026-09-15: the v2 fix above (wait for the
                # checkbox to be visible, click it, then wait_for_load_state
                # networkidle once) was STILL unreliable. Live debugging
                # found TWO separate issues:
                #
                # 1) frame_locator().click() against this specific
                #    Cloudflare checkbox is unreliable -- repeated live
                #    tests showed a real mouse-coordinate click on the
                #    checkbox's actual on-screen position resolves the
                #    challenge consistently and near-instantly, while
                #    frame_locator's own click() would sometimes leave the
                #    page stuck on "Just a moment..." indefinitely. Now
                #    dispatches a genuine OS-level mouse click at the
                #    checkbox's bounding-box centre instead.
                #
                # 2) `networkidle` can report "quiet" while the page's
                #    title is still literally "Just a moment..." --
                #    Cloudflare sometimes does a slow follow-up
                #    redirect/DOM-swap AFTER the network goes idle, not
                #    tied to any network event we can wait on. Fixed by
                #    polling the page's own <title> directly instead.
                try:
                    frame = pg.frame_locator("iframe[src*='challenges.cloudflare.com']").first
                    cb = frame.locator("input[type=checkbox], .cb-lb, #challenge-stage").first
                    cb.wait_for(state="visible", timeout=10000)
                    box = cb.bounding_box()
                    if box:
                        pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    else:
                        cb.click(timeout=5000)  # fallback if bounding_box fails
                    for _ in range(20):  # up to ~20s, polling every 1s
                        if "Just a moment" not in pg.title():
                            break
                        pg.wait_for_timeout(1000)
                    # Even after the title clears, give the real page's own
                    # JS a brief moment to finish rendering before we start
                    # querying for elements on it.
                    pg.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                # Fixed 2026-09-15 (v2, real root cause found): the cookie
                # consent widget's own "Accept All"/"Decline All" button
                # click was the ACTUAL trigger for the "General Error" page
                # -- confirmed via a controlled test: a plain page reload
                # (no cookie click) never errors, but clicking EITHER
                # cookie button (tested 3-for-3 reproducible) causes the
                # widget's own JS to reload the page, and that specific
                # reload gets a genuine HTTP 500 from Storage King's own
                # server. This has nothing to do with bot detection --
                # confirmed it's their cookie-consent integration's own
                # bug on this specific reload path.
                #
                # Fix: don't click either cookie button at all. Remove the
                # consent widget's DOM node directly via JS instead, which
                # dismisses it visually without triggering its buggy
                # click-handler-driven reload. Live-tested: 100% success
                # (vs. 0/6 clicking either button) across repeated clean
                # single attempts.
                try:
                    removed = pg.evaluate("""() => {
                        const sel = document.querySelector(
                            '#cookiescript_injected, .cookiescript_injected, '
                            + '[class*=cookie-script], [id*=cookiescript]'
                        );
                        if (sel) { sel.remove(); return true; }
                        return false;
                    }""")
                    if not removed:
                        # Fallback for a differently-branded consent widget:
                        # try the old click-based approach rather than doing
                        # nothing, since we can't be sure this covers every
                        # cookie-banner variant Storage King might show.
                        btn = pg.get_by_role("button", name=re.compile("accept", re.I))
                        btn.first.click(timeout=5000)
                except Exception:
                    pass

                # Fixed 2026-09-15: Storage King's own backend intermittently
                # serves a genuine "General Error" page for this URL (their
                # own error copy: "We are working to resolve this issue...
                # Please checkback later") -- confirmed via live screenshot,
                # NOT a bot block or Cloudflare artifact. Without this
                # check, the old code silently waited the full 20s for a
                # size label that will never appear on an error page,
                # burning time before the outer retry loop (attempts=5)
                # kicks in. Detect it immediately and raise to retry with a
                # fresh profile right away -- reloading the same error page
                # often just serves the same error again, so a full fresh
                # attempt is the right response, not a same-page reload.
                if "General Error" in pg.title():
                    raise RuntimeError("Storage King General Error page (their own backend fault, not a block) -- retrying with fresh profile")

                size_label = pg.locator("label:has(#SizeId_50)").first
                size_label.wait_for(state="visible", timeout=20000)
                # The size label can get detached-and-replaced once more
                # right as the page finishes settling post-Cloudflare; a
                # short retry loop absorbs that without a long fixed sleep.
                # Re-resolve the locator fresh each attempt (not just
                # re-click the same stale handle) since the element may
                # have been torn out and replaced by an equivalent one.
                for click_attempt in range(4):
                    try:
                        size_label = pg.locator("label:has(#SizeId_50)").first
                        size_label.wait_for(state="visible", timeout=8000)
                        size_label.click(timeout=8000)
                        break
                    except Exception:
                        if click_attempt == 3:
                            raise
                        pg.wait_for_timeout(1500)
                btn = pg.get_by_role("button", name="Continue")
                btn.first.wait_for(state="visible", timeout=10000)
                btn.first.click()
                pg.wait_for_timeout(2000)

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
        "scraped_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
