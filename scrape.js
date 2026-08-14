/**
 * Basildon self-storage daily price sweep.
 * Runs a real (headless) browser via Playwright on GitHub Actions.
 *
 * Targets:
 *  - Shurgard Basildon store page (full client-rendered unit list, robots-permitted)
 *  - Storage King Basildon quote flow — ONLY as far as prices appear WITHOUT
 *    entering any personal details. This script never types into any input.
 *  - Big Top's own site (owner's benchmark).
 *  - Safestore is deliberately EXCLUDED: its /get-a-quote/ paths are disallowed
 *    by robots.txt and are not automated.
 *
 * Output: data/latest.json, data/prices-<date>.json, appends data/history.csv,
 * plus debug/*.txt dumps of rendered page text for selector refinement.
 */
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const today = new Date().toLocaleDateString('en-CA', { timeZone: 'Europe/London' }); // YYYY-MM-DD
const OUT = { date: today, observations: [], warnings: [] };

/**
 * Clearly-marked mystery-shop test identity (standard industry practice —
 * the name flags the enquiry as a test so sales teams don't chase it).
 * Email defaults to an address the owner controls; test@test.com is a real
 * third-party domain and many forms bounce-validate it.
 * Forms are ONLY ever filled on FORM_FILL_ALLOWED_HOSTS. Safestore is never
 * touched by this script (robots.txt disallows its quote pages).
 */
const TEST_IDENTITY = {
  firstName: 'Test',
  lastName: 'Test',
  fullName: 'Test Test',
  phone: '01234567894',
  email: process.env.SWEEP_EMAIL || 'vikgoy+test@gmail.com',
};
const FORM_FILL_ALLOWED_HOSTS = ['www.storageking.co.uk', 'www.bigtopselfstorage.com', 'www.makespaceselfstorage.co.uk'];

const dbgDir = path.join(__dirname, 'debug');
const dataDir = path.join(__dirname, 'data');
fs.mkdirSync(dbgDir, { recursive: true });
fs.mkdirSync(dataDir, { recursive: true });

function dump(name, text) {
  fs.writeFileSync(path.join(dbgDir, `${name}.txt`), text || '');
}

function money(s) {
  const m = String(s).replace(/,/g, '').match(/£\s*(\d+(?:\.\d{1,2})?)/);
  return m ? parseFloat(m[1]) : null;
}

async function acceptCookies(page) {
  for (const sel of ['#onetrust-accept-btn-handler', 'button:has-text("Accept all")',
    'button:has-text("Accept All")', 'button:has-text("Accept")', 'button:has-text("Allow all")',
    '[id*="cookie"] button', '[class*="cookie"] button:has-text("Accept")']) {
    try { await page.locator(sel).first().click({ timeout: 1500 }); return; } catch (e) {}
  }
}

async function settle(page, opts = {}) {
  const { scroll = false, quick = false } = opts;
  try { await page.waitForLoadState('networkidle', { timeout: quick ? 6000 : 12000 }); } catch (e) {}
  await page.waitForTimeout(quick ? 800 : 1500);
  if (scroll) {
    try {
      await page.evaluate(async () => {
        for (let y = 0; y <= document.body.scrollHeight; y += 800) {
          window.scrollTo(0, y);
          await new Promise(r => setTimeout(r, 80));
        }
        window.scrollTo(0, 0);
      });
    } catch (e) {}
    await page.waitForTimeout(800);
  }
}

/** Run a site function with a hard time budget so one hung site can't eat the workflow. */
async function withBudget(name, ms, fn) {
  let timer;
  const timeout = new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`budget of ${ms / 1000}s exceeded`)), ms); });
  try {
    await Promise.race([fn(), timeout]);
  } catch (e) {
    OUT.warnings.push(`${name}: ${String(e.message || e).split('\n')[0]}`);
  } finally { clearTimeout(timer); }
}

/** Fill a contact-details step with the marked test identity. Allowed hosts only. */
async function fillContactForm(page) {
  const host = new URL(page.url()).hostname;
  if (!FORM_FILL_ALLOWED_HOSTS.includes(host)) return false;
  let filled = 0;
  const fill = async (sel, val) => {
    try {
      const loc = page.locator(sel).first();
      if (await loc.count()) { await loc.fill(val, { timeout: 2000 }); filled++; }
    } catch (e) {}
  };
  await fill('input[name*="first" i], input[id*="first" i], input[placeholder*="first" i]', TEST_IDENTITY.firstName);
  await fill('input[name*="last" i], input[id*="last" i], input[placeholder*="last" i], input[name*="surname" i]', TEST_IDENTITY.lastName);
  // single full-name field (only if no first-name field matched)
  if (filled === 0) await fill('input[name="name" i], input[id="name" i], input[placeholder*="name" i]', TEST_IDENTITY.fullName);
  await fill('input[type="email"], input[name*="email" i]', TEST_IDENTITY.email);
  await fill('input[type="tel"], input[name*="phone" i], input[name*="tel" i], input[name*="mobile" i]', TEST_IDENTITY.phone);
  // Never tick marketing-consent checkboxes; only a required terms box if it blocks progress.
  return filled > 0;
}

async function clickNext(page) {
  for (const sel of ['button:has-text("See prices")', 'button:has-text("Get my quote")',
    'button:has-text("Get quote")', 'button:has-text("Continue")', 'button:has-text("Next")',
    'button[type="submit"]', 'input[type="submit"]']) {
    try { await page.locator(sel).first().click({ timeout: 2500 }); return true; } catch (e) {}
  }
  return false;
}

/** Generic extractor: find card-like elements containing a £/week or £/month price. */
async function extractPriceCards(page) {
  return await page.evaluate(() => {
    const priceRe = /£\s*\d+(?:\.\d{1,2})?\s*(?:\/|per\s*)?\s*(week|wk|month|mo)/i;
    const sizeRe = /(\d+(?:\.\d+)?)\s*(?:sq\.?\s?ft|ft²|sqft|square\s*feet)/i;
    const all = Array.from(document.querySelectorAll('div,li,article,section,a'));
    const hits = all.filter(el => {
      const t = el.innerText || '';
      return t.length < 900 && priceRe.test(t);
    });
    // keep innermost matching elements only
    const inner = hits.filter(el => !hits.some(o => o !== el && el.contains(o)));
    return inner.map(el => {
      const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      const prices = (t.match(/£\s*\d+(?:\.\d{1,2})?/g) || []).map(p => parseFloat(p.replace(/[£,\s]/g, '')));
      const size = (t.match(sizeRe) || [])[1] || null;
      const per = (t.match(priceRe) || [])[1] || null;
      const promo = /first month|% off|£1\b|free/i.test(t)
        ? (t.match(/[^.]*?(?:first month|% off|£1\b|free)[^.]*/i) || [''])[0].trim() : '';
      return { text: t, size_sqft: size ? parseFloat(size) : null, prices, per, promo };
    });
  });
}

async function shurgard(ctx) {
  const page = await ctx.newPage();
  const api = [];
  page.on('response', async res => {
    try {
      const ct = res.headers()['content-type'] || '';
      if (ct.includes('json')) {
        const body = await res.text();
        if (/price|unit|promo/i.test(body) && body.length > 300) api.push({ url: res.url(), body: body.slice(0, 20000) });
      }
    } catch (e) {}
  });
  const url = 'https://www.shurgard.com/en-gb/self-storage-uk/essex/basildon';
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await acceptCookies(page);
  await settle(page, { scroll: true });
  const cards = await extractPriceCards(page);
  dump('shurgard-page', await page.evaluate(() => document.body.innerText));
  fs.writeFileSync(path.join(dbgDir, 'shurgard-api.json'), JSON.stringify(api, null, 2));
  let n = 0;
  for (const c of cards) {
    if (!c.prices.length) continue;
    const rack = c.prices.length > 1 ? Math.max(...c.prices) : null;
    const offer = Math.min(...c.prices);
    OUT.observations.push({
      competitor: 'Shurgard Basildon', metric: 'unit', size_sqft: c.size_sqft,
      rack_rate: rack !== offer ? rack : null, offer_rate: offer, per: c.per || 'week',
      promo: c.promo, source: url, raw: c.text,
    });
    n++;
  }
  if (n === 0) OUT.warnings.push('Shurgard: no unit cards extracted — check debug/shurgard-page.txt and debug/shurgard-api.json');
  await page.close();
}

async function storageKing(ctx) {
  const page = await ctx.newPage();
  const url = 'https://www.storageking.co.uk/get-a-quote/select-a-size/?store=basildon';
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await acceptCookies(page);
  await settle(page);
  dump('storageking-step1', await page.evaluate(() => document.body.innerText));

  // Find size cards and their Continue buttons; click through WITHOUT entering data.
  const sizeButtons = await page.locator('button:has-text("Continue"), a:has-text("Continue")').count();
  if (sizeButtons === 0) {
    OUT.warnings.push('Storage King: no Continue buttons found on select-a-size — flow may have changed; see debug/storageking-step1.txt');
  }
  // Cap iterations: every size means a full page reload; 30 sizes blew the 20-min workflow budget.
  const maxSteps = Math.min(sizeButtons, 10);
  if (sizeButtons > maxSteps) OUT.warnings.push(`Storage King: ${sizeButtons} size options found, sweeping first ${maxSteps} to stay inside the time budget.`);
  for (let i = 0; i < maxSteps; i++) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await settle(page, { quick: true });
      const btn = page.locator('button:has-text("Continue"), a:has-text("Continue")').nth(i);
      // capture the size label from the nearest card
      let label = '';
      try { label = await btn.evaluate(b => (b.closest('div,li,article') || b).innerText.replace(/\s+/g, ' ').slice(0, 120)); } catch (e) {}
      await btn.click({ timeout: 5000 });
      await settle(page, { quick: true });
      const text = await page.evaluate(() => document.body.innerText);
      if (i < 3) dump(`storageking-step2-${i}`, text);
      let text2 = text;
      const asksDetails = await page.locator('input[type="email"], input[type="tel"], input[name*="name" i]').count();
      const priceRe = /£\s*\d+(?:\.\d{1,2})?\s*(?:\/|per\s*)?\s*(?:week|wk|month|mo)/gi;
      let priceMatch = text.replace(/\s+/g, ' ').match(priceRe);
      let viaForm = false;
      if ((!priceMatch || !priceMatch.length) && asksDetails > 0) {
        // Price gated behind contact details: fill the marked test identity and proceed.
        const didFill = await fillContactForm(page);
        if (didFill && await clickNext(page)) {
          await settle(page, { quick: true });
          text2 = await page.evaluate(() => document.body.innerText);
          if (i < 3) dump(`storageking-step3-${i}`, text2);
          priceMatch = text2.replace(/\s+/g, ' ').match(priceRe);
          viaForm = true;
        }
      }
      if (priceMatch && priceMatch.length) {
        const prices = priceMatch.map(money).filter(Boolean);
        OUT.observations.push({
          competitor: 'Storage King Basildon', metric: viaForm ? 'quote_after_test_form' : 'quote_step_price',
          size_label: label, prices, per: 'as shown', promo: '',
          source: page.url(), raw: priceMatch.join(' | '),
          gated_behind_contact_form: asksDetails > 0,
        });
      } else {
        OUT.warnings.push(`Storage King: size ${i} ("${label}") — no price found${asksDetails ? ' even after test-identity form fill' : ''}; see debug dumps.`);
      }
    } catch (e) {
      OUT.warnings.push(`Storage King: size ${i} click failed: ${String(e).split('\n')[0]}`);
    }
  }
  await page.close();
}

async function bigTop(ctx) {
  const page = await ctx.newPage();
  for (const url of ['https://www.bigtopselfstorage.com/reserve', 'https://www.bigtopselfstorage.com/pricing']) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await acceptCookies(page);
      await settle(page);
      dump('bigtop-' + url.split('/').pop(), await page.evaluate(() => document.body.innerText));
      const cards = await extractPriceCards(page);
      for (const c of cards) {
        if (!c.prices.length) continue;
        OUT.observations.push({
          competitor: 'Big Top (own)', metric: 'unit', size_sqft: c.size_sqft,
          rack_rate: c.prices.length > 1 ? Math.max(...c.prices) : null,
          offer_rate: Math.min(...c.prices), per: c.per || '', promo: c.promo, source: url, raw: c.text,
        });
      }
    } catch (e) {
      OUT.warnings.push(`Big Top ${url}: ${String(e).split('\n')[0]}`);
    }
  }
  await page.close();
}

async function makeSpace(ctx) {
  const page = await ctx.newPage();
  const url = 'https://www.makespaceselfstorage.co.uk/get-a-household-quote/?tool=home';
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await acceptCookies(page);
  await settle(page);
  dump('makespace-quote-step1', await page.evaluate(() => document.body.innerText));

  // Select the Wickford store if a store picker is present (dropdown or clickable option).
  try {
    const select = page.locator('select').first();
    if (await select.count()) {
      await select.selectOption({ label: /wickford/i }).catch(async () => {
        const opts = await select.locator('option').allTextContents();
        const idx = opts.findIndex(o => /wickford/i.test(o));
        if (idx >= 0) await select.selectOption({ index: idx });
      });
    } else {
      await page.locator('a:has-text("Wickford"), button:has-text("Wickford"), label:has-text("Wickford"), [role="option"]:has-text("Wickford")').first().click({ timeout: 4000 });
    }
    await settle(page);
  } catch (e) {
    OUT.warnings.push('Make Space: could not select Wickford store — see debug/makespace-quote-step1.txt');
  }

  // Walk up to 4 steps: click through, filling the marked test identity if a contact form gates prices.
  const priceRe = /£\s*\d+(?:\.\d{1,2})?\s*(?:\/|per\s*)?\s*(?:week|wk|month|mo)/gi;
  for (let step = 0; step < 4; step++) {
    const text = await page.evaluate(() => document.body.innerText);
    dump(`makespace-quote-step${step + 2}`, text);
    const priceMatch = text.replace(/\s+/g, ' ').match(priceRe);
    if (priceMatch && priceMatch.length) {
      const cards = await extractPriceCards(page);
      let n = 0;
      for (const c of cards) {
        if (!c.prices.length) continue;
        OUT.observations.push({
          competitor: 'Make Space Wickford', metric: 'quote_after_test_form', size_sqft: c.size_sqft,
          rack_rate: c.prices.length > 1 ? Math.max(...c.prices) : null,
          offer_rate: Math.min(...c.prices), per: c.per || 'week', promo: c.promo,
          source: page.url(), raw: c.text,
        });
        n++;
      }
      if (n === 0) OUT.observations.push({
        competitor: 'Make Space Wickford', metric: 'quote_after_test_form', size_sqft: null,
        rack_rate: null, offer_rate: null, per: '', promo: '',
        source: page.url(), raw: (priceMatch || []).join(' | '),
      });
      break;
    }
    const asksDetails = await page.locator('input[type="email"], input[type="tel"], input[name*="name" i]').count();
    if (asksDetails > 0) await fillContactForm(page);
    if (!(await clickNext(page))) {
      OUT.warnings.push(`Make Space: no way forward at quote step ${step + 1} and no prices found — see debug dumps.`);
      break;
    }
    await settle(page);
  }
  await page.close();
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 BigTopPriceCheck/1.0',
    locale: 'en-GB', timezoneId: 'Europe/London',
  });
  // Speed: skip images/fonts/media — we only need DOM text and JSON.
  await ctx.route('**/*', route =>
    ['image', 'font', 'media'].includes(route.request().resourceType()) ? route.abort() : route.continue());

  // Hard per-site budgets so a hung site can never blow the workflow's 20-min limit.
  await withBudget('Shurgard', 180000, () => shurgard(ctx));
  await withBudget('Storage King', 420000, () => storageKing(ctx));
  await withBudget('Make Space', 240000, () => makeSpace(ctx));
  await withBudget('Big Top', 180000, () => bigTop(ctx));
  await browser.close().catch(() => {});

  fs.writeFileSync(path.join(dataDir, 'latest.json'), JSON.stringify(OUT, null, 2));
  fs.writeFileSync(path.join(dataDir, `prices-${today}.json`), JSON.stringify(OUT, null, 2));

  const csvPath = path.join(dataDir, 'history.csv');
  const header = 'date,competitor,metric,size,rack_rate,offer_rate,per,promo,source\n';
  if (!fs.existsSync(csvPath)) fs.writeFileSync(csvPath, header);
  const esc = v => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`;
  const lines = OUT.observations.map(o =>
    [today, o.competitor, o.metric, o.size_sqft || o.size_label || '', o.rack_rate || '',
      o.offer_rate || (o.prices ? o.prices.join(';') : ''), o.per || '', o.promo || '', o.source]
      .map(esc).join(',')).join('\n');
  if (lines) fs.appendFileSync(csvPath, lines + '\n');

  console.log(`Done: ${OUT.observations.length} observations, ${OUT.warnings.length} warnings`);
  OUT.warnings.forEach(w => console.log('WARN:', w));
})();
