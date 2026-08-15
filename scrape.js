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

// A stray rejected promise (e.g. from a response listener) must degrade to a
// warning, not crash the whole run with exit code 1.
process.on('unhandledRejection', (e) => {
  try { OUT.warnings.push('unhandledRejection: ' + String(e).split('\n')[0]); } catch (err) {}
});

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
  phone: '07845475523',
  email: process.env.SWEEP_EMAIL || 'baas123123+test@gmail.com',
};
const FORM_FILL_ALLOWED_HOSTS = ['www.storageking.co.uk', 'www.bigtopselfstorage.com', 'www.makespaceselfstorage.co.uk'];

/**
 * TESTING HOLDS (2026-08-15): Make Space and Big Top are captured and stable,
 * so they're paused while Storage King is being debugged — no point re-quoting
 * them on every test run. WHEN TESTING IS DONE: set every flag to true to
 * activate the full daily sweep.
 */
const RUN = {
  shurgard: true,     // stable (API) — cheap, leave on
  storageking: true,  // under test
  makespace: false,   // HOLD — complete, re-enable for production
  bigtop: false,      // HOLD — complete, re-enable for production
};

const dataDir = path.join(__dirname, 'data');
const dbgDir = path.join(dataDir, 'debug'); // inside data/ so the workflow commits it
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
      let t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      // If the innermost price element lacks the unit size, climb to the card
      // that contains both (sizes usually live in a sibling heading).
      let cardText = t;
      if (!sizeRe.test(t)) {
        let p = el.parentElement;
        for (let depth = 0; p && depth < 6; depth++, p = p.parentElement) {
          const pt = (p.innerText || '').replace(/\s+/g, ' ').trim();
          if (pt.length > 1200) break; // too broad — would span multiple cards
          if (sizeRe.test(pt)) { cardText = pt; break; }
        }
      }
      const prices = (t.match(/£\s*\d+(?:\.\d{1,2})?/g) || []).map(x => parseFloat(x.replace(/[£,\s]/g, '')));
      const size = (cardText.match(sizeRe) || [])[1] || null;
      const per = (t.match(priceRe) || [])[1] || null;
      const promo = /first month|% off|£1\b|free/i.test(cardText)
        ? (cardText.match(/[^.]*?(?:first month|% off|£1\b|free)[^.]*/i) || [''])[0].trim() : '';
      return { text: cardText.slice(0, 300), size_sqft: size ? parseFloat(size) : null, prices, per, promo };
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

  // PRIMARY: their own units API (discovered 2026-08-14, robots-permitted) —
  // returns every unit; the page only renders "best deal" cards by default.
  try {
    const resp = await page.request.get('https://www.shurgard.com/en-gb/api/stores/73/units?searchTerm=null');
    const apiData = await resp.json();
    const units = ((apiData.stores || [])[0] || {}).units || [];
    for (const u of units) {
      const promoType = (u.promotion || {}).promotionType || '';
      const p = u.pricing || {};
      // strikethroughPrice is the STANDARD rate as an HTML string, e.g.
      // "£45.<div class='sup'>23</div>" — extract pounds + decimals.
      let rack = null;
      const sm = String(p.strikethroughPrice || '').match(/£\s*(\d+)\.?[^\d]*(\d{2})?/);
      if (sm) rack = parseFloat(sm[1] + '.' + (sm[2] || '00'));
      const disc = (p.discount || {});
      const endDate = disc.discountEndDate ? `; special rate ends ${disc.discountEndDate}` : '';
      OUT.observations.push({
        competitor: 'Shurgard Basildon', metric: 'unit',
        size_sqft: parseFloat(u.sizeDefault) || null,
        rack_rate: rack,
        offer_rate: p.priceRaw != null ? Math.round(p.priceRaw * 100) / 100 : null,
        welcome_rate: p.welcomePriceRaw ?? null,
        per: 'week',
        promo: (promoType === 'FirstMonth' ? '£1 first month' : promoType === 'FiftyPercentOff' ? '50% off first month' : promoType) + endDate,
        source: 'https://www.shurgard.com/en-gb/api/stores/73/units',
        raw: `${u.isLocker ? 'Locker ' : ''}${u.sizeDefault} sq ft id=${u.id}`,
      });
    }
    if (units.length) OUT.warnings.push(`Shurgard: API returned ${units.length} units (full ladder).`);
  } catch (e) {
    OUT.warnings.push(`Shurgard units API failed (${String(e).split('\n')[0]}) — falling back to page cards.`);
  }

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
  dump('storageking-clickables', await page.evaluate(() =>
    Array.from(document.querySelectorAll('a,button,[role="button"],input[type="submit"],label'))
      .map(e => `${e.tagName}.${(e.className || '').toString().slice(0, 60)}: ${((e.innerText || e.value || '')).replace(/\s+/g, ' ').trim().slice(0, 90)}`)
      .filter(t => t.length > 8).join('\n')));
  await page.close();

  // Confirmed structure (2026-08-14 debug): sizes are radio LABELs
  // ("label.radio" with text like "25 sq.ft") in a flickity carousel, plus a
  // single Continue button that only works once a size radio is selected.
  //
  // 2026-08-15 finding: the initial load always shows the radios, but any
  // RE-navigation in the same browser state renders 0 of them — their quote
  // app remembers the session and resumes elsewhere. So every size gets a
  // FRESH ISOLATED context (own cookies/storage), like a first-time visitor.
  const browser = ctx.browser();
  const SK_SIZES = ['10 sq.ft', '25 sq.ft', '35 sq.ft', '50 sq.ft', '75 sq.ft', '100 sq.ft', '150 sq.ft', '200 sq.ft'];
  let failDumps = 0;
  for (let i = 0; i < SK_SIZES.length; i++) {
    const label = SK_SIZES[i];
    let c2 = null;
    try {
      c2 = await browser.newContext({
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 BigTopPriceCheck/1.0',
        locale: 'en-GB', timezoneId: 'Europe/London',
      });
      await c2.route('**/*', route =>
        ['image', 'font', 'media'].includes(route.request().resourceType()) ? route.abort() : route.continue());
      const page = await c2.newPage();
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
      await acceptCookies(page);
      await settle(page, { quick: true });
      await page.waitForSelector('label.radio', { timeout: 15000 }).catch(() => {});
      // 2026-08-15 finding: the labels ARE present (26 of them) but Playwright's
      // text filter matches none — their textContent carries hidden text beyond
      // the visible size. Select by visible innerText via the page's own DOM.
      const clicked = await page.evaluate((lbl) => {
        const els = Array.from(document.querySelectorAll('label.radio'));
        const el = els.find(e => ((e.innerText || '').replace(/\s+/g, ' ').trim()).startsWith(lbl));
        if (!el) return { ok: false, labels: els.map(e => (e.innerText || '').replace(/\s+/g, ' ').trim()).slice(0, 30) };
        el.scrollIntoView({ block: 'center' });
        el.click();
        const input = el.querySelector('input[type="radio"]') ||
          (el.getAttribute('for') && document.getElementById(el.getAttribute('for')));
        if (input) { input.click(); input.checked = true; input.dispatchEvent(new Event('change', { bubbles: true })); }
        return { ok: true };
      }, label);
      if (!clicked.ok) {
        OUT.warnings.push(`Storage King: no visible label starting "${label}"; page labels: ${JSON.stringify(clicked.labels).slice(0, 300)}`);
        if (failDumps++ < 2) dump(`storageking-loopfail-${label.replace(/\W+/g, '')}`, await page.evaluate(() => document.body.innerText));
        await c2.close();
        continue;
      }
      await page.waitForTimeout(700);
      // Diagnostic: did the size click actually select the radio?
      const radioChecked = await page.evaluate(() => document.querySelectorAll('input[type="radio"]:checked').length);
      if (radioChecked === 0) OUT.warnings.push(`Storage King "${label}": DOM click did not check any radio — their app may need real input events.`);
      // Continue: try Playwright first, fall back to a DOM click (2026-08-15:
      // the Playwright click times out even though the button exists).
      const cont = page.locator('button:has-text("Continue"), a:has-text("Continue")').first();
      await cont.scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
      try {
        await cont.click({ timeout: 5000 });
      } catch (e) {
        const domCont = await page.evaluate(() => {
          const els = Array.from(document.querySelectorAll('button, a, input[type="submit"]'));
          const el = els.find(x => /continue/i.test((x.innerText || x.value || '')) && x.offsetParent !== null);
          if (!el) return false;
          el.scrollIntoView({ block: 'center' });
          el.click();
          return true;
        });
        if (!domCont) throw new Error('Continue button not clickable via Playwright or DOM');
      }
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
      // Order-independent fallback: some quote pages print "per week" before
      // the £ figure, which the pattern above misses.
      if ((!priceMatch || !priceMatch.length) && /week|month|\/wk|\/mo/i.test(text2)) {
        priceMatch = text2.match(/£\s*\d+(?:\.\d{1,2})?/g);
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
      OUT.warnings.push(`Storage King: size "${label}" failed: ${String(e).split('\n')[0]}`);
    } finally {
      try { if (c2) await c2.close(); } catch (e2) {}
    }
  }
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
  dump('makespace-clickables', await page.evaluate(() =>
    Array.from(document.querySelectorAll('a,button,select,option,[role="button"],[role="option"],label,input'))
      .map(e => `${e.tagName}.${(e.className || '').toString().slice(0, 60)}: ${((e.innerText || e.value || e.placeholder || '')).replace(/\s+/g, ' ').trim().slice(0, 90)}`)
      .filter(t => t.length > 8).join('\n')));

  // Confirmed (2026-08-14 debug): their quote flow has NO Wickford store — the
  // "Wickford" landing page is an SEO page served by the BILLERICAY facility
  // (stores: Billericay, Horsham, Clapton Hackney). Select Billericay wherever
  // a store choice appears, and record the competitor as Make Space (Billericay).
  // Their details form (confirmed 2026-08-14): required Title select, First/Surname,
  // Telephone, Email, PostCode, preferred-store select (Billericay/Clapton/Horsham)
  // and a duration select. Fill every select sensibly, store = Billericay.
  async function fillMakespaceDetails() {
    try {
      const selects = page.locator('select');
      const n = await selects.count();
      for (let s = 0; s < n; s++) {
        const sel = selects.nth(s);
        try {
          const opts = await sel.locator('option').allTextContents();
          const current = await sel.inputValue().catch(() => '');
          const idxB = opts.findIndex(o => /billericay/i.test(o));
          if (idxB >= 0) { await sel.selectOption({ index: idxB }); continue; }
          const idxMr = opts.findIndex(o => /^\s*Mr\s*$/i.test(o));
          if (idxMr >= 0) { await sel.selectOption({ index: idxMr }); continue; }
          // any other required select (e.g. duration): pick the first real option
          if (!current && opts.length > 1) await sel.selectOption({ index: 1 });
        } catch (e) {}
      }
      const pc = page.locator('input[name*="post" i], input[id*="post" i], input[placeholder*="post" i]').first();
      if (await pc.count()) await pc.fill('SS14 3AB', { timeout: 2000 }).catch(() => {}); // Basildon-area test postcode
      return true;
    } catch (e) { return false; }
  }
  // Confirmed quote-page layout (2026-08-14, from the run that reached it):
  //   "50% off for 12 weeks / Special Offer Price: per week for the first 12 weeks £17.49 /
  //    Ongoing Price: per week £49.98 £34.98"
  // Their page prints "per week" BEFORE the £ figure, so order-independent
  // regexes anchored on the labels are used, not generic £/week patterns.
  const MS_SIZES = ['10 sq ft', '16 sq ft', '20 sq ft', '25 sq ft', '35 sq ft', '50 sq ft', '75 sq ft',
    '100 sq ft', '125 sq ft', '150 sq ft', '175 sq ft', '200 sq ft', '250 sq ft']; // full carousel (13 sizes)
  for (const sizeLabel of MS_SIZES) {
    try {
      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
      await acceptCookies(page);
      await settle(page, { quick: true });
      // exact match so "25 sq ft" can't hit "125 sq ft"/"250 sq ft"
      const sizeEl = page.getByText(new RegExp('^\\s*' + sizeLabel + '\\s*$')).first();
      await sizeEl.scrollIntoViewIfNeeded({ timeout: 3000 }).catch(() => {});
      let sized = false;
      try { await sizeEl.click({ timeout: 4000 }); sized = true; }
      catch (e) {
        // The card may be off-screen in the carousel — page through it and retry.
        for (let n = 0; n < 5 && !sized; n++) {
          try { await page.locator('button:has-text("›"), .flickity-button.next, [aria-label="Next"]').first().click({ timeout: 1500 }); } catch (e2) { break; }
          await page.waitForTimeout(300);
          try { await sizeEl.click({ timeout: 1500 }); sized = true; } catch (e3) {}
        }
        if (!sized) await sizeEl.click({ timeout: 2000, force: true });
      }
      await page.waitForTimeout(600);
      await page.locator('a:has-text("Get a Quote For This Room"), button:has-text("Get a Quote For This Room")').first().click({ timeout: 5000 });
      await settle(page, { quick: true });
      await fillMakespaceDetails();
      await fillContactForm(page);
      await clickNext(page);
      await settle(page, { quick: true });
      const text = await page.evaluate(() => document.body.innerText);
      dump('makespace-quote-' + sizeLabel.replace(/\W+/g, ''), text);
      const intro = text.match(/Special Offer Price:?[\s\S]{0,140}?£\s*([\d,.]+)/i);
      const ongoing2 = text.match(/Ongoing Price:?[\s\S]{0,140}?£\s*([\d,.]+)[\s\S]{0,50}?£\s*([\d,.]+)/i);
      const ongoing1 = ongoing2 || text.match(/Ongoing Price:?[\s\S]{0,140}?£\s*([\d,.]+)/i);
      if (intro || ongoing1) {
        const rack = ongoing1 ? parseFloat(ongoing1[1].replace(/,/g, '')) : null;
        const disc = ongoing2 ? parseFloat(ongoing2[2].replace(/,/g, '')) : null;
        OUT.observations.push({
          competitor: 'Make Space (Billericay)', metric: 'quote_after_test_form',
          size_sqft: parseInt(sizeLabel), rack_rate: rack, offer_rate: disc ?? rack,
          intro_rate: intro ? parseFloat(intro[1].replace(/,/g, '')) : null, per: 'week',
          promo: (text.match(/\d+%\s*off[^£\n]{0,50}/i) || [''])[0].trim(),
          source: page.url(),
          raw: `intro=${intro && intro[1]} ongoing_std=${ongoing1 && ongoing1[1]} ongoing_web=${(ongoing2 && ongoing2[2]) || ''}`,
        });
      } else {
        OUT.warnings.push(`Make Space ${sizeLabel}: quote flow completed but no prices parsed — see dump makespace-quote-${sizeLabel.replace(/\W+/g, '')}.txt`);
      }
    } catch (e) {
      OUT.warnings.push(`Make Space ${sizeLabel}: ${String(e).split('\n')[0]}`);
    }
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
  if (RUN.shurgard) await withBudget('Shurgard', 180000, () => shurgard(ctx));
  if (RUN.storageking) await withBudget('Storage King', 420000, () => storageKing(ctx));
  if (RUN.makespace) await withBudget('Make Space', 720000, () => makeSpace(ctx)); // 13 sizes × ~45s
  if (RUN.bigtop) await withBudget('Big Top', 180000, () => bigTop(ctx));
  Object.entries(RUN).filter(([, v]) => !v).forEach(([k]) => OUT.warnings.push(`${k}: ON HOLD (testing) — not run this sweep.`));
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
