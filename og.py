#!/usr/bin/env python3
"""Render static/og.png (1200x630 share image) from data/models.json.

Needs Playwright with Chromium:  pip install playwright && playwright install chromium
Run after big price changes:      python og.py
"""
import asyncio, json, pathlib
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).parent


def page_html():
    data = json.loads((ROOT / "data/models.json").read_text())
    site = json.loads((ROOT / "data/site.json").read_text())
    fee = data["openrouter_card_fee"]
    by = {m["slug"]: m for m in data["models"]}
    blend = lambda m: (10 * m["saygm"]["in"] + 2 * m["saygm"]["out"], (10 * m["openrouter"]["in"] + 2 * m["openrouter"]["out"]) * (1 + fee))
    save = lambda m: 1 - blend(m)[0] / blend(m)[1]
    # Two big-name models plus the open model with the biggest real saving (never one where SayGM costs more)
    opens = sorted((m for m in data["models"] if m["maker"] == "Open models" and save(m) > 0), key=save, reverse=True)
    pick = ["claude-sonnet-5-5", "gpt-6-2-sol"] + [m["slug"] for m in opens[:1]]
    rows, mx = [], 0
    for s in pick:
        m = by[s]
        o = (10 * m["openrouter"]["in"] + 2 * m["openrouter"]["out"]) * (1 + fee)
        g = 10 * m["saygm"]["in"] + 2 * m["saygm"]["out"]
        rows.append((m["name"], o, g, round((1 - g / o) * 100)))
        mx = max(mx, o, g)
    bars = "".join(
        f'<div class="r"><div class="n">{n}</div><div class="b"><i style="width:{o/mx*100:.1f}%;background:#c3cad2"></i>'
        f'<i style="width:{g/mx*100:.1f}%;background:#101418"></i></div><div class="p{" t" if p >= 50 else ""}">−{p}%</div></div>'
        for n, o, g, p in rows)
    fav = (ROOT / "static/favicon.svg").read_text()
    best = max(m_[3] for m_ in rows)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@600;800&display=swap" rel="stylesheet">
<style>
body{{margin:0;width:1200px;height:630px;background:#fff;color:#101418;font-family:Manrope,system-ui,sans-serif;display:flex;flex-direction:column;padding:56px 64px;box-sizing:border-box}}
.top{{display:flex;align-items:center;gap:14px;font-weight:800;font-size:28px}} .top svg{{width:44px;height:44px}}
h1{{font-size:68px;line-height:1.02;letter-spacing:-.03em;margin:28px 0 30px;font-weight:800}}
.r{{display:grid;grid-template-columns:300px 1fr 150px;align-items:center;gap:20px;padding:10px 0;border-top:2px solid #e3e6ea;font-size:26px;font-weight:600}}
.b{{display:grid;gap:6px}} .b i{{display:block;height:16px}}
.p{{text-align:right;font-weight:800;font-size:40px;color:#0f6b3a;letter-spacing:-.03em}} .p.t{{background:#ffd84a;color:#101418;padding:0 8px;justify-self:end}}
.foot{{margin-top:auto;font-size:20px;color:#5b6673}}
</style></head><body>
<div class="top">{fav}{site['brand']}</div>
<h1>SayGM vs OpenRouter:<br>up to {max(round((1-(10*m['saygm']['in']+2*m['saygm']['out'])/((10*m['openrouter']['in']+2*m['openrouter']['out'])*(1+fee)))*100) for m in data['models'])}% cheaper</h1>
{bars}
<div class="foot">Monthly cost at 10M input + 2M output tokens, incl. OpenRouter's 5.5% fee. Prices {data['checked']}.</div>
</body></html>"""


async def main(executable=None):
    async with async_playwright() as p:
        b = await p.chromium.launch(**({"executable_path": executable} if executable else {}))
        pg = await b.new_page(viewport={"width": 1200, "height": 630})
        await pg.set_content(page_html())
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=str(ROOT / "static/og.png"))
        await b.close()
    print("Wrote static/og.png")


if __name__ == "__main__":
    import sys
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
