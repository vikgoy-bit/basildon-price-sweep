# Basildon Storage Price Sweep

Daily automated competitor price sweep for Big Top Self Storage, Basildon.
Runs a real browser (Playwright/Chromium) on GitHub Actions at 05:30 UK time,
commits results to `data/`, where Claude's daily tracker job picks them up.

## What it captures
- **Shurgard Basildon** — full unit list from the store page (sizes, regular
  vs special weekly rates, promos). Client-rendered, so it needs this real
  browser; robots.txt permits the page.
- **Storage King Basildon** — steps through the quote flow's size options.
  Where a price is gated behind a contact form, it fills a clearly-marked
  mystery-shop test identity (name "Test Test", phone 01234567894, email
  defaulting to vikgoy+test@gmail.com — override with a SWEEP_EMAIL repo
  secret/env var) and proceeds to the price. Standard marked-test practice;
  the name flags it so sales teams don't chase. Marketing-consent boxes are
  never ticked. Form-filling is hard-limited to an allowlist of hosts and
  can never touch Safestore.
- **Make Space Wickford** — household quote flow (robots-permitted), selecting
  the Wickford store and using the marked test identity where a contact form
  gates prices.
- **Big Top (own site)** — reserve/pricing pages as the benchmark.
- **Safestore** — deliberately **not** automated: its quote pages are
  disallowed by robots.txt. Promotions are tracked separately from its public
  store page by the Claude daily job.

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
- First run is part-recon: the extractor is deliberately generic and dumps
  each page's rendered text to `debug/` (kept 14 days as a workflow artifact)
  so selectors can be tightened after seeing real output.
- These sites may use bot protection (e.g. Cloudflare). If runs come back
  empty, that's the likely cause — check the debug artifact and tell Claude;
  options can be assessed then. No captcha-solving or protection-bypassing
  will be added.
