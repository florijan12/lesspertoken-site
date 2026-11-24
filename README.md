# LessPerToken: SayGM vs OpenRouter site

A small static site: one comparison page, one page per model, a sitemap, and scripts that keep prices fresh and add your own test results.

```
data/models.json    prices and model IDs (the only file the daily job changes)
data/site.json      brand, domain, referral link, author
data/bench.json     your test results (created by bench.py)
templates/          page templates
static/             logo, favicon, share image
build.py            data + templates -> dist/
update_prices.py    pulls live prices from SayGM and OpenRouter
bench.py            sends the same prompts through both gateways and records speed, success and cost
og.py               redraws static/og.png from current prices
```

## 1. One-time setup (15 minutes)

1. `pip install -r requirements.txt`
2. Edit `data/site.json`:
   - `domain`: your site, already set to `https://lesspertoken.com` (no trailing slash)
   - `author.name`, `author.bio`, `author.url`: a real person. Google weighs this heavily for review pages. Leave `name` empty to hide the author box.
   - `brand`: the site name, "LessPerToken" (also the text in `static/logo.svg`).
3. Check model IDs: `python update_prices.py --list`
   It shows the IDs SayGM and OpenRouter actually use next to ours. Fix any `saygm_id` / `openrouter_id` in `data/models.json` that don't match.
   If SayGM asks for a key, `export SAYGM_API_KEY=...` first.
4. `python update_prices.py` — pulls live prices. Once an ID matches, the model page shows the real ID in its code sample.
   First run: if prices look wrong, run `python update_prices.py --inspect` and send the output to whoever maintains this, so the field names can be adjusted.
5. `python build.py` and open `dist/index.html`.

## 2. Add your own test results (the biggest credibility boost)

```
export SAYGM_API_KEY=...  OPENROUTER_API_KEY=...
python bench.py --location "Ljubljana, Slovenia"
python build.py
```

It prints a worst-case spend (about $0.20 for the default four models at 10 requests each) and asks before sending anything.
A "How we test" table then appears on the main page and on each tested model's page. Re-run monthly; results merge by model.
Add expensive models only if you want to: `--models claude-fable-5-2 gpt-6-2-astra`.

## 3. Publish with daily price refresh (Cloudflare Workers, free)

The site runs as the Cloudflare Worker `rapid-dust-d52a` on `lesspertoken.com`. `wrangler.jsonc` tells Cloudflare to serve the `dist/` folder.

1. Push this folder to a GitHub repo (private is fine).
2. In Cloudflare: My Profile → API Tokens → Create token → template **"Edit Cloudflare Workers"**. Copy your **Account ID** from the Workers & Pages overview.
3. GitHub repo → Settings → Secrets and variables → Actions → add `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` (and `SAYGM_API_KEY` if the models list needs a key).
4. Actions tab → "Refresh prices and deploy" → **Run workflow**. Check the site afterwards.

From then on it refreshes prices every day at 04:17 UTC, commits the change, rebuilds and deploys. No more manual uploads.
Price jumps over 60% are held back and shown as warnings in the run log (safety check against bad data).

Manual deploy from your computer instead: `npm i -g wrangler`, `wrangler login`, `python build.py`, `wrangler deploy`.

## 4. After launch

- Submit `https://lesspertoken.com/sitemap.xml` in Google Search Console.
- Redraw the share image after big price changes: `pip install playwright && playwright install chromium && python og.py`.
- Gemini 4 Argon's promo price ends 1 December 2026. The daily refresh picks up the new price; delete its `note` in `data/models.json` afterwards.
- Add a model: copy an entry in `data/models.json`, give it a new `slug`, set its IDs, run `update_prices.py` and `build.py`.
- Use a separate SayGM referral link per channel (Reddit, HN, Discord) to see which one brings signups; the page uses `referral_url` from `site.json`.
