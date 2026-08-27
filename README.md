# Basildon Storage Price Sweep

Daily automated competitor price sweep for Big Top Self Storage, Basildon.
Runs a real browser (Playwright/Chromium) on GitHub Actions at ~05:37 UK
time, commits results to `data/`, where Claude's daily tracker job picks
them up.

## What it captures
- **Shurgard Basildon** — full unit list from the store page (sizes, regular
  vs special weekly rates, promos). Client-rendered, so it needs this real
  browser; robots.txt permits the page. Scraped by GitHub Actions itself.
- **Make Space Wickford** — household quote flow (robots-permitted), selecting
  the Wickford store and using the marked test identity where a contact form
  gates prices. Scraped by GitHub Actions itself.
- **Big Top (own site)** — reserve/pricing pages as the benchmark. Scraped by
  GitHub Actions itself.
- **Storage King Basildon** — loads the "select a size" → "your price" quote
  flow. All sizes' prices render in one page load, no per-size form
  submission needed.
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
  or audio CAPTCHAs, evade IP bans, or use residential proxies.

  **Architecture note (2026-08-26, updated after same-day testing):**
  `safestore_scrape.py` and `storageking_scrape.py` live in this repo but
  are **not run by GitHub Actions**. A live test (workflow run 32972108615)
  proved both sites are Cloudflare/reCAPTCHA-blocked specifically on
  GitHub-hosted runners' Azure datacenter IP ranges — Storage King returned
  an explicit "Sorry, you have been blocked" Cloudflare page, Safestore
  failed 100% of 26 attempts with the same all-or-nothing signature. This
  is a network/IP-reputation problem, not a code or click-count issue
  (Storage King in particular is a single page load with zero per-size
  clicks, and still got blocked). Both scripts' safe-degradation logic
  worked correctly under the real failure — zero fabricated data, clear
  warnings, `history.csv` untouched — but the scrape itself needs a working
  IP to succeed at all.

  The fix: both scripts now run on a separate machine with a working IP
  (currently: Vikas's Hermes/Alex automation host), invoked by
  `~/.hermes/scripts/push-safestore-storageking-to-repo.sh` on that
  machine's own Mon/Wed/Fri schedule. That script scrapes, then commits and
  pushes `data/safestore-latest.json` / `data/storageking-latest.json`
  straight to `main`. GitHub Actions' `build_report.py` step just reads
  whatever is already committed at those paths when it runs — same as any
  other data source on disk. If that data goes stale (the other machine's
  cron didn't run, or a scrape came back empty), the report grid simply
  falls back to the last-known price, exactly like a blocked/failed
  in-Actions scrape would.

## GitHub Actions schedule reliability (added 2026-08-27)
On 2026-08-27, the `schedule` trigger (`cron: "30 5 * * *"`) was silently
skipped by GitHub for a full day — no run, no queued attempt, nothing in
the Actions history — despite the workflow being active and the repo
healthy. GitHub's own docs acknowledge scheduled triggers can be delayed
or dropped under platform load, especially at contended times like the
exact hour/half-hour every repo in the world tends to use. Two fixes:
1. The cron was moved to `37 5 * * *` — off the contended mark, cheap
   mitigation, no guarantee.
2. A watchdog now runs independently of GitHub Actions (so it can't share
   the same failure mode): `~/.hermes/scripts/gh-actions-schedule-watchdog.sh`
   on the Hermes/Alex host, scheduled via Hermes's own cron (not GitHub's).
   It checks the most recent `schedule`-triggered run via the GitHub API;
   if it's older than 26 hours, it force-triggers the workflow via
   `workflow_dispatch` and reports the intervention. Silent when the
   schedule fired on time, so it can run frequently without noise.

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
- Storage King and Safestore are scraped OFF GitHub Actions (see
  "Architecture note" above) because GitHub's runner IPs are confirmed
  blocked by both sites' anti-bot protection. If their `-latest.json`
  files stop updating, check the other machine's cron (currently the
  Mon/Wed/Fri Telegram-triggered job on Vikas's Hermes/Alex host), not
  this repo's own Actions runs.

