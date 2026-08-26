# Basildon Storage Price Sweep

Daily automated competitor price sweep for Big Top Self Storage, Basildon.
Runs a real browser (Playwright/Chromium) on GitHub Actions at 05:30 UK time,
commits results to `data/`, where Claude's daily tracker job picks them up.

## What it captures
- **Shurgard Basildon** — full unit list from the store page (sizes, regular
  vs special weekly rates, promos). Client-rendered, so it needs this real
  browser; robots.txt permits the page.
- **Storage King Basildon** — loads the "select a size" → "your price" quote
  flow. All sizes' prices render in one page load, no per-size form
  submission needed. A one-time Cloudflare Turnstile checkbox appears on a
  fresh browser profile; the automation clicks it (this is a standard
  interaction checkbox, not a CAPTCHA solve — no image/audio puzzles are
  attempted). No personal details are entered for this site's price step.
- **Make Space Wickford** — household quote flow (robots-permitted), selecting
  the Wickford store and using the marked test identity where a contact form
  gates prices.
- **Big Top (own site)** — reserve/pricing pages as the benchmark.
- **Safestore Basildon** — steps through the full quote wizard (Personal →
  size → duration → "not sure yet" → contact details) for both a 3-month and
  a 12-month term, across every unit size the store offers. Fills a
  clearly-marked mystery-shop test identity (name "Test Test", phone
  07845412125, email defaulting to a test address — override with a
  SWEEP_EMAIL repo secret/env var). Standard marked-test practice; the name
  flags it so sales teams don't chase. Marketing-consent boxes are never
  ticked. Form-filling for both Storage King and Safestore is hard-limited to
  an explicit allowlist of hosts.

  **Policy note (2026-08-26, informed decision, not an oversight):**
  Safestore's `robots.txt` disallows `/get-a-quote/`, `/storage-quote/*`, and
  `/results/` — the exact pages this sweep needs to read a price. Safestore
  was excluded from this repo for that reason through 2026-08-25, with
  Storage King's per-size quote flow *also* held back manually because an
  earlier Cloudflare block was hit in headless Playwright (see git history).
  The owner (Vikas) explicitly decided on 2026-08-26 to bring both into this
  automated sweep anyway, accepting the robots.txt conflict on Safestore as a
  known, deliberate tradeoff rather than a silent one. The scrape uses a
  stealth Chromium build (patchright) to avoid the headless-detection
  fingerprint that triggered the earlier Storage King block, and passes a
  Cloudflare Turnstile checkbox where one appears — it does not solve image
  or audio CAPTCHAs, evade IP bans, or use residential proxies. If either
  site starts hard-blocking this runner (e.g. GitHub Actions' datacenter IP
  range gets flagged, which is a real and different risk from a residential
  sandbox IP), the run degrades to "kept last-known price, no data this
  run" rather than fabricating a result — see `data/debug/` and the workflow
  log for diagnosis. No CAPTCHA-solving service or IP-rotation has been
  added; if blocking becomes persistent, the fallback is reverting to the
  manual/Claude-driven quote pattern already used on 2026-08-15 and
  2026-08-20 (see `history.csv` rows with `notes` mentioning "Claude in
  Chrome"), not silently faking numbers.

## Setup (one-time, ~5 minutes)
1. Create a GitHub account if you don't have one (github.com).
2. Create a new repository, e.g. `basildon-price-sweep`. **Public** is easiest
   (the Claude job reads results without auth). Private works too but needs a
   token shared with the tracker.
3. Upload the contents of this folder to the repository (on github.com:
   "uploading an existing file" link works from a phone; keep the folder
   structure, including `.github/workflows/daily.yml`).
4. Go to the repo's **Actions** tab → enable workflows → open "Daily price
   sweep" → **Run workflow** to test.
5. Send Claude the repository URL. The daily tracker job will be updated to
   fetch `data/latest.json`, merge it into the price history, and alert on
   changes.

## Honest caveats
- First run of any new site/step is part-recon: extractors dump rendered page
  text to `data/debug/` (kept 14 days as a workflow artifact) so selectors
  can be tightened after seeing real output.
- Sites may tighten bot protection at any time. If a run comes back with
  fewer observations than the last one for a given competitor, the report
  keeps last-known prices rather than showing a blank/zero — check
  `data/debug/` and the workflow log to diagnose, don't assume the sweep
  logic is wrong. No CAPTCHA-solving service, proxy rotation, or IP-ban
  evasion is used or planned; Cloudflare Turnstile *interaction* checkboxes
  (click-to-verify, not an image/audio puzzle) are clicked where Storage
  King presents one, and that is the extent of "bot protection handling"
  here.
- Safestore's `robots.txt` explicitly disallows the pages this sweep reads
  for that site (see the Safestore section above). This is a known,
  deliberate, owner-approved tradeoff as of 2026-08-26 — not an oversight.
  Revisit if Safestore starts actively blocking rather than merely
  disallowing in robots.txt, or if the owner's risk tolerance changes.
