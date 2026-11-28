#!/usr/bin/env python3
"""Build the static site from data/ and templates/ into dist/.

Usage:  python build.py            # writes dist/
        python build.py --out DIR  # writes somewhere else
"""
import argparse, base64, datetime as dt, html, json, pathlib, shutil, statistics
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = pathlib.Path(__file__).parent
GROUP_ORDER = ["Anthropic", "OpenAI", "Google", "Open models"]
MIX_IN, MIX_OUT = 10, 2  # millions of tokens, the 5:1 mix used for "You save"


def money(x):
    """$2.11, $0.052, $0.001; 'Not published' for None."""
    if x is None:
        return "Not published"
    from decimal import Decimal, ROUND_HALF_UP
    q = lambda v, places: Decimal(repr(v)).quantize(Decimal(places), rounding=ROUND_HALF_UP)
    if x >= 0.1:
        return f"${q(x, '0.01'):,}"
    s = str(q(x, "0.001")).rstrip("0")
    if len(s.split(".")[1]) < 2:
        s = str(q(x, "0.01"))
    return "$" + s


def human_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%B %Y')}"


def enrich(m, fee):
    o, s, l = m["openrouter"], m["saygm"], m["list"]
    eff = lambda v: None if v is None else v * (1 + fee)
    or_blend = (MIX_IN * o["in"] + MIX_OUT * o["out"]) * (1 + fee)
    sg_blend = MIX_IN * s["in"] + MIX_OUT * s["out"]
    m["save"] = round((1 - sg_blend / or_blend) * 100)
    # Honest labels when OpenRouter's cheapest route beats SayGM
    m["cheaper"] = m["save"] > 0
    m["save_badge"] = f"−{m['save']}%" if m["save"] > 0 else ("same" if m["save"] == 0 else f"+{-m['save']}%")
    m["or_blend"], m["sg_blend"] = round(or_blend, 4), round(sg_blend, 4)
    m["f"] = {
        "list_in": money(l["in"]), "list_out": money(l["out"]), "list_cache": money(l.get("cache")),
        "or_in": money(eff(o["in"])), "or_out": money(eff(o["out"])), "or_cache": money(eff(o.get("cache"))),
        "sg_in": money(s["in"]), "sg_out": money(s["out"]), "sg_cache": money(s.get("cache")),
    }
    # Big clouds: cheapest standard-tier price across AWS Bedrock, Google Vertex AI and Azure.
    # Billed on the cloud invoice, so no card fee.
    names = {"aws": "AWS Bedrock", "google": "Google Vertex AI", "azure": "Azure"}
    short = {"aws": "AWS", "google": "Google", "azure": "Azure"}
    cl = m.get("clouds") or {}
    rows = []
    for c in ("aws", "google", "azure"):
        if c in cl:
            r = cl[c]
            rows.append({"key": c, "name": names[c], "region": "global" if r["region"] == "global" else r["region"],
                         "blend": MIX_IN * r["in"] + MIX_OUT * r["out"],
                         "in": money(r["in"]), "out": money(r["out"]), "cache": money(r.get("cache"))})
    m["cloud_rows"] = rows
    if rows:
        best = min(r["blend"] for r in rows)
        top = [r for r in rows if abs(r["blend"] - best) < 1e-9]
        m["cloud"] = {"in": top[0]["in"], "out": top[0]["out"], "where": ", ".join(short[r["key"]] for r in top),
                      "sg_save": round((1 - sg_blend / best) * 100)}
    else:
        m["cloud"] = None
    m.setdefault("id_verified", False)
    return m


def model_work(m, fee):
    """Worked monthly costs for one model, from live prices: an agent with caching, a chat app, a nightly batch job."""
    L, O, S = m["list"], m["openrouter"], m["saygm"]

    def cost(p, i, o, hit=0.0, f=1.0):
        c = p.get("cache")
        hit = hit if c is not None else 0.0
        return (i * (1 - hit) * p["in"] + i * hit * (c or 0) + o * p["out"]) * f

    pct = lambda new, old: round((1 - new / old) * 100) if old else 0
    a = {"or_": cost(O, 50, 2, .85, 1 + fee), "sg": cost(S, 50, 2, .85), "sg_nocache": cost(S, 50, 2)}
    c = {"list": cost(L, 5, 1), "or_": cost(O, 5, 1, 0, 1 + fee), "sg": cost(S, 5, 1)}
    b = {"batch": cost(L, 20, 5) / 2, "or_": cost(O, 20, 5, 0, 1 + fee), "sg": cost(S, 20, 5)}
    v = {"list": cost(L, 100, 10), "or_": cost(O, 100, 10, 0, 1 + fee), "sg": cost(S, 100, 10)}
    fmt = lambda d: {k: money(x) for k, x in d.items()} | {"raw": d}
    tidy = lambda r: int(r) if r == int(r) else r
    cache_of = lambda p: None if p.get("cache") is None else tidy(round(p["cache"] / p["in"] * 100, 1))
    return {"agent": fmt(a), "chat": fmt(c), "batch": fmt(b), "volume": fmt(v),
            "agent_cache_pct": pct(a["sg"], a["sg_nocache"]), "agent_sg_vs_or": pct(a["sg"], a["or_"]),
            "agent_or_minus_sg": money(a["or_"] - a["sg"]), "batch_sg_cheaper": b["sg"] < b["batch"],
            "cache_ratio_list": cache_of(L), "cache_ratio_sg": cache_of(S), "cache_ratio_or": cache_of(O)}


def stale_notes(models, checked):
    """Label any gateway price that the daily check held back, with the date it was last confirmed."""
    names = {"saygm": "SayGM", "openrouter": "OpenRouter"}
    for m in models:
        old = [f"{names[s]} price last confirmed {human_date(m[s + '_checked'])}"
               for s in names if m.get(s + "_checked") and m[s + "_checked"] < checked]
        if old:
            m["note"] = " ".join(filter(None, [m.get("note"), "; ".join(old) + "."]))


SITE_NODES = []  # Organization + WebSite, filled in main()


def jsonld(graph):
    org_id = SITE_NODES[0]["@id"] if SITE_NODES else None
    if org_id:
        for node in graph:
            if isinstance(node.get("publisher"), dict) and node["publisher"].get("@type") == "Organization":
                node["publisher"] = {"@id": org_id}
            if isinstance(node.get("author"), dict) and node["author"].get("@type") == "Organization":
                node["author"] = {"@id": org_id}
    return json.dumps({"@context": "https://schema.org", "@graph": SITE_NODES + graph}, ensure_ascii=False).replace("</", "<\\/")


def faq_ld(faq):
    return {"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q["q"], "acceptedAnswer": {"@type": "Answer", "text": q["a"]}} for q in faq]}


def faq_item(q, a, a_html=None):
    return {"q": q, "a": a, "a_html": a_html or html.escape(a)}


def author_ld(site):
    a = site["author"]
    if a.get("name"):
        p = {"@type": "Person", "name": a["name"]}
        if a.get("url"):
            p["url"] = a["url"]
        return p
    return {"@type": "Organization", "name": site["brand"]}


def bench_rows(bench, models):
    if not bench:
        return None, []
    by = bench.get("results", {})
    rows = []
    fmt = lambda g: {
        "ttft": "–" if g.get("ttft_median_s") is None else f"{g['ttft_median_s']:.2f} s",
        "success": "–" if g.get("success_rate") is None else f"{round(g['success_rate'] * 100)}%",
        "cost100": "–" if g.get("cost_per_request_usd") is None else money(g["cost_per_request_usd"] * 100),
    }
    for m in models:
        r = by.get(m["slug"])
        if r and "saygm" in r and "openrouter" in r:
            rows.append({"slug": m["slug"], "name": m["name"], "saygm": fmt(r["saygm"]), "openrouter": fmt(r["openrouter"])})
    info = {"n": bench.get("n"), "date_human": human_date(bench["date"]), "location": bench.get("location", "")}
    return (info if rows else None), rows


def alternatives(alt, by_slug, fee):
    """Rows for the gateway price comparison: cost of a fixed workload on each gateway."""
    if not alt:
        return None
    tm, share = alt["workload"]["tokens_m"], alt["workload"]["input_share"]
    cols = [by_slug[s] for s in alt["models"] if s in by_slug]
    rows, notes = [], []
    for g in alt["gateways"]:
        cells = []
        for m in cols:
            if g.get("source") == "saygm":
                p = (m["saygm"]["in"], m["saygm"]["out"])
            elif g.get("source") == "openrouter":
                p = (m["openrouter"]["in"] * (1 + fee), m["openrouter"]["out"] * (1 + fee))
            else:
                p = g["prices"].get(m["slug"], "Not published")
            if isinstance(p, str):
                cells.append({"text": p, "cost": None})
                continue
            cost = tm * (share * p[0] + (1 - share) * p[1])
            note = g.get("notes", {}).get(m["slug"])
            mark = ""
            if note:
                mark = "*" * (len(notes) + 1)
                notes.append(f"{mark}{g['name']}, {m['name']}: {note}.")
            cells.append({"cost": cost, "cost_f": f"${round(cost):,}", "price_f": f"{money(p[0])} / {money(p[1])}", "mark": mark})
        rows.append({"name": g["name"], "cells": cells, "pricing": g["pricing"], "catalog": g["catalog"], "privacy": g["privacy"], "best": g["best"]})
    for j in range(len(cols)):
        vals = [r["cells"][j]["cost"] for r in rows if r["cells"][j]["cost"] is not None]
        for r in rows:
            r["cells"][j]["best"] = r["cells"][j]["cost"] is not None and r["cells"][j]["cost"] == min(vals)
    via = [g["name"] for g in alt["gateways"] if g.get("via_source")]
    own = [{"name": g["name"], **g["own_source"], "date": human_date(g["own_source"]["checked"])} for g in alt["gateways"] if g.get("own_source")]
    return {"cols": cols, "rows": rows, "notes": notes, "source": alt["source"], "via": via, "own": own,
            "source_date": human_date(alt["source"]["checked"]),
            "workload": f"{tm}M tokens, {round(share * 100)}% input and {round((1 - share) * 100)}% output"}


def token_examples(by_slug, fee):
    """Worked examples for the token-saving post, computed from live prices so they never go stale."""
    s, h = by_slug["claude-sonnet-5-5"], by_slug["claude-haiku-5-5"]
    L, G, O = s["list"], s["saygm"], s["openrouter"]
    pct = lambda new, old: round((1 - new / old) * 100)

    # 1. Coding agent: 50M input, 2M output a month, 85% of input served from cache
    ti, to, hit = 50, 2, 0.85
    no_cache = ti * L["in"] + to * L["out"]
    cached = ti * (1 - hit) * L["in"] + ti * hit * L["cache"] + to * L["out"]
    cached_or = (ti * (1 - hit) * O["in"] + ti * hit * O["cache"] + to * O["out"]) * (1 + fee)
    cached_sg = ti * (1 - hit) * G["in"] + ti * hit * G["cache"] + to * G["out"]
    agent = {"no_cache": money(no_cache), "cached": money(cached), "cached_or": money(cached_or),
             "cached_sg": money(cached_sg), "cache_pct": pct(cached, no_cache), "total_pct": pct(cached_sg, no_cache),
             "cache_ratio": round(L["cache"] / L["in"] * 100), "sg_vs_or": pct(cached_sg, cached_or)}

    # 2. Routing: support bot, 5M input + 1M output a month
    ri, ro = 5, 1
    son_cost, hai_cost = ri * L["in"] + ro * L["out"], ri * h["list"]["in"] + ro * h["list"]["out"]
    mixed = 0.8 * hai_cost + 0.2 * son_cost
    route = {"sonnet": money(son_cost), "haiku": money(hai_cost), "mixed": money(mixed), "mixed_pct": pct(mixed, son_cost),
             "ratio": round(L["in"] / h["list"]["in"])}

    # 3. Output length: 1,000 requests a day for 30 days, 800 vs 300 output tokens each
    out_long, out_short = 30_000 * 800 / 1e6, 30_000 * 300 / 1e6
    output = {"long_m": f"{out_long:g}", "short_m": f"{out_short:g}", "long": money(out_long * L["out"]),
              "short": money(out_short * L["out"]), "ratio": round(L["out"] / L["in"])}

    # 4. Batch: nightly job, 20M input + 5M output a month, 50% batch discount
    bi, bo = 20, 5
    live = bi * L["in"] + bo * L["out"]
    batch = {"live": money(live), "batch": money(live / 2)}

    # 5. Context: 20-turn chat, 500 tokens per turn. Full history vs last 4 turns + 300-token summary
    turn, n = 500, 20
    full = sum(turn * k for k in range(1, n + 1))
    trimmed = sum(turn * min(k, 4) + (300 if k > 4 else 0) for k in range(1, n + 1))
    context = {"full": f"{full:,}", "trimmed": f"{trimmed:,}", "pct": pct(trimmed, full), "last": f"{turn * n:,}"}

    return {"model": s["name"], "haiku": h["name"], "L": s["f"], "H": h["f"], "agent": agent, "route": route,
            "output": output, "batch": batch, "context": context, "save": s["save"]}


POSTS = [
    {"slug": "token-saving-basics", "template": "posts/token-saving-basics.html", "part": 1,
     "date": "2026-11-23",
     "title": "How to cut your LLM API bill: 6 token-saving techniques with examples",
     "short": "6 ways to cut your LLM API bill",
     "seo_title": "How to Cut Your LLM API Bill: 6 Token-Saving Techniques",
     "description": "Prompt caching, model routing, shorter outputs, batch jobs, trimmed context and the right gateway: six ways to cut your LLM API bill, with examples.",
     "og_head": "6 ways to cut your LLM API bill", "og_stat": "Caching, routing, shorter outputs, batching"},
    {"slug": "gpt-researcher-cost-audit", "template": "posts/gpt-researcher-audit.html", "part": 2,
     "date": "2026-11-25",
     "title": "We audited GPT Researcher's LLM calls: a caching miss from two quote marks, and 4 other leaks",
     "short": "Auditing GPT Researcher's token use",
     "seo_title": "GPT Researcher LLM Cost Audit: 5 Token Leaks Found",
     "description": "We audited the open-source GPT Researcher agent's LLM calls: a broken prompt cache, high reasoning effort on small jobs and paid retries, with fixes.",
     "og_head": "We audited GPT Researcher's LLM calls", "og_stat": "5 token leaks, one from two quote marks"},
    {"slug": "browser-use-cost-audit", "template": "posts/browser-use-audit.html", "part": 3,
     "date": "2026-11-26",
     "title": "We audited browser-use: Claude on OpenRouter or Bedrock pays full price for every step",
     "short": "Auditing browser-use's token use",
     "seo_title": "browser-use Cost Audit: No Prompt Caching on OpenRouter & Bedrock",
     "description": "browser-use only turns on prompt caching for Claude on the direct Anthropic API. On OpenRouter or Bedrock every step pays full price. The fix, and what it saves.",
     "og_head": "We audited browser-use's LLM calls", "og_stat": "Claude on OpenRouter or Bedrock: no prompt cache"},
    {"slug": "mem0-cost-audit", "template": "posts/mem0-audit.html", "part": 4,
     "date": "2026-11-28",
     "title": "We audited mem0: Claude users pay full price for an 8,400-token prompt on every save",
     "short": "Auditing mem0's token use",
     "seo_title": "mem0 Cost Audit: Uncached 8,400-Token Prompt on Claude",
     "description": "mem0 sends the same 8,400-token prompt on every memory save. OpenAI and Gemini cache it; Claude never does. The one-line fix, and what it saves.",
     "og_head": "We audited mem0's LLM calls", "og_stat": "8,400 tokens per save, never cached on Claude"},
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args()
    out = pathlib.Path(args.out)

    data = json.loads((ROOT / "data/models.json").read_text())
    site = json.loads((ROOT / "data/site.json").read_text())
    bench_path = ROOT / "data/bench.json"
    bench_raw = json.loads(bench_path.read_text()) if bench_path.exists() else None
    alt_path = ROOT / "data/alternatives.json"
    alt_raw = json.loads(alt_path.read_text()) if alt_path.exists() else None
    if bench_raw and not site.get("test_location"):
        site["test_location"] = bench_raw.get("location", "")

    fee = data["openrouter_card_fee"]
    models = [enrich(m, fee) for m in data["models"]]
    stale_notes(models, data["checked"])
    by_slug = {m["slug"]: m for m in models}
    for m in models:
        m["work"] = model_work(m, fee)
    groups = [{"name": g, "models": [m for m in models if m["maker"] == g]} for g in GROUP_ORDER]
    groups = [g for g in groups if g["models"]]
    max_save = max(m["save"] for m in models)
    domain = site["domain"].rstrip("/")
    for p in POSTS:  # the homepage lists posts too
        p["url"] = f"{domain}/blog/{p['slug']}/"
        p["date_human"] = human_date(p["date"])
    SITE_NODES[:] = [
        {"@type": "Organization", "@id": domain + "/#org", "name": site["brand"], "url": domain + "/",
         "logo": {"@type": "ImageObject", "url": domain + "/logo.svg"},
         "description": "Daily-checked price comparison of LLM API gateways.",
         "sameAs": site.get("same_as", [])},
        {"@type": "WebSite", "@id": domain + "/#website", "name": site["brand"], "url": domain + "/",
         "publisher": {"@id": domain + "/#org"}, "inLanguage": "en"}]
    base = ""
    checked_human = human_date(data["checked"])
    cdates = [m["clouds_checked"] for m in data["models"] if m.get("clouds_checked")]
    clouds_checked_human = human_date(min(cdates)) if cdates else None
    bench, brows = bench_rows(bench_raw, models)
    alt = alternatives(alt_raw, by_slug, fee)

    fav = (ROOT / "static/favicon.svg").read_text().strip()
    common = dict(
        site=site, data=data, base=base, checked_human=checked_human, clouds_checked_human=clouds_checked_human, year=data["checked"][:4],
        favicon_data="data:image/svg+xml;base64," + base64.b64encode(fav.encode()).decode(),
        mark_svg=fav.replace("<svg ", '<svg class="mark" aria-hidden="true" ', 1),
        models=models, bench=bench, calc=True,
    )
    refs = site.get("referral_urls", {})
    go_ref = {"home": "site", "model": "site", "about": "site", "audit": "site", "blog": "blog", "skill": "skill"}
    tagged = lambda gop: dict(site, gop=gop, referral_url=refs.get(go_ref[gop], site["referral_url"]))
    common["site"] = tagged("home")

    def js_data(start):
        return json.dumps({"fee": fee, "base": base, "start": start, "models": [
            {"slug": m["slug"], "n": m["name"], "c": bool(m.get("featured")),
             "si": m["saygm"]["in"], "so": m["saygm"]["out"], "sc": m["saygm"].get("cache"),
             "oi": m["openrouter"]["in"], "oo": m["openrouter"]["out"], "oc": m["openrouter"].get("cache")}
            for m in models]}).replace("</", "<\\/")

    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")), autoescape=select_autoescape(["html"]))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # ---- main comparison page ----
    son = by_slug.get("claude-sonnet-5-5")
    newest = [by_slug[s]["save"] for s in ("gpt-6-2-sol", "gemini-4-neon") if s in by_slug]
    pricier = [m["name"] for m in models if not m["cheaper"]]
    cheaper = ("Yes, for every model we track. " if not pricier else "For most models we track. ") + (
        f"Claude is about {son['save']}% cheaper" if son else "Claude, GPT and Gemini are cheaper")
    if newest:
        cheaper += f", the newest GPT-6.2 and Gemini 4 Neon about {min(newest)} to {max(newest)}%"
    cheaper += f", and some open models up to {max_save}%." if max_save > 0 else "."
    if pricier:
        cheaper += (f" On {', '.join(pricier)}, OpenRouter's cheapest provider is currently cheaper, "
                    "so check the table before switching those.")
    faq = [
        faq_item("Is SayGM cheaper than OpenRouter?", cheaper),
        faq_item("Is SayGM private?",
                 "Prompts are sealed inside an Intel TDX enclave where SayGM can't read them, and prompts and completions stay out of every record SayGM keeps. On Claude, GPT and Gemini, the model provider processes them under its API terms, as with any gateway. Confidential -tee models stay sealed through inference too.",
                 "Prompts are sealed inside an Intel TDX enclave where SayGM can't read them, and prompts and completions stay out of every record SayGM keeps. On Claude, GPT and Gemini, the model provider processes them under its API terms, as with any gateway. Confidential <code>-tee</code> models stay sealed through inference too."),
        faq_item("What are the best OpenRouter alternatives?",
                 "It depends on what you need. SayGM for the lowest prices on Claude and GPT, OpenRouter itself for the widest catalogue, Requesty for EU data residency, Venice for privacy-first use with end-to-end encrypted open models, and DeepInfra or Together AI for open models at volume or fine-tuning."),
        faq_item("Can SayGM prices go above the list price?", "No. Providers compete for every request, so prices follow the market but never go above the model maker's list price. The date at the top shows our latest check."),
        faq_item("Can I keep OpenRouter as a backup?", "Yes. SayGM uses the same API format, with pay as you go and no subscription, so you can keep your OpenRouter key as a ready fallback."),
        faq_item("Does SayGM have a free tier?", "No. There is also no subscription and no top-up fee. You pay per token."),
        faq_item("Does SayGM work with existing code?",
                 "Yes. Point your OpenAI SDK at https://api.saygm.com/v1, or your Anthropic or Gemini SDK at https://api.saygm.com, and use a SayGM key.",
                 "Yes. Point your OpenAI SDK at <code>https://api.saygm.com/v1</code>, or your Anthropic or Gemini SDK at <code>https://api.saygm.com</code>, and use a SayGM key."),
        faq_item("Do you earn money from this page?", "Yes. SayGM rewards us when someone signs up through our links and adds credit. Your price is the same either way."),
    ]
    url = domain + "/"
    claude_save = min(m["save"] for m in models if m["maker"] == "Anthropic")
    title_h = f"SayGM vs OpenRouter: Claude for {claude_save}% less, same models"
    page = {
        "title": "Cheaper Claude API: SayGM vs OpenRouter Prices (2026)",
        "description": f"A cheaper OpenRouter alternative: Claude costs about {claude_save}% less on SayGM. Daily-checked prices for Claude, GPT-6.2, Gemini and open models.",
        "og_title": title_h, "url": url,
        "jsonld": jsonld([
            {"@type": "Article", "headline": title_h, "dateModified": data["checked"], "datePublished": "2026-11-22",
             "author": author_ld(site), "publisher": {"@type": "Organization", "name": site["brand"], "logo": {"@type": "ImageObject", "url": domain + "/logo.svg"}},
             "image": domain + "/og.png", "mainEntityOfPage": url},
            faq_ld(faq)]),
    }
    noted = [m for m in models if m.get("note")]
    (out / "index.html").write_text(env.get_template("compare.html").render(
        **common, page=page, groups=groups, max_save=max_save, claude_save=claude_save, faq=faq, noted=noted,
        bench_rows=brows, alt=alt, js_data=js_data("claude-sonnet-5-5"), posts=sorted(POSTS, key=lambda p: p["date"], reverse=True)))

    # ---- one page per model ----
    for m in models:
        o, s = m["or_blend"], m["sg_blend"]
        tee_txt = (f"The standard route is sealed through SayGM's gateway, then the serving provider reads the prompt. A confidential -tee version of {m['name']} stays sealed through inference too, at a smaller discount."
                   if m.get("tee") else
                   f"SayGM's gateway can't read your prompt. The provider that runs {m['name']} processes it under its own API terms, as with any gateway. Optional guardrails can replace the personal data and secrets they recognise first.")
        below_list = ""
        if not m.get("or_estimate"):
            lb = MIX_IN * m["list"]["in"] + MIX_OUT * m["list"]["out"]
            below_list = f" and {round((1 - s / lb) * 100)}% below {m['maker']}'s list price"
        if m["cheaper"]:
            q1 = (f"Of the options we compare, SayGM: {m['f']['sg_in']} input and {m['f']['sg_out']} output per million tokens. "
                  f"That is {m['save']}% less than OpenRouter after its card fee{below_list}.")
            q2 = (f"How much would you save on {m['name']} at 10M input and 2M output tokens a month?",
                  f"About {money(o - s)} a month: {money(o)} on OpenRouter against {money(s)} on SayGM.")
        else:
            q1 = (f"Right now, OpenRouter's cheapest provider: {m['f']['or_in']} input and {m['f']['or_out']} output per million tokens "
                  f"including its card fee, against {m['f']['sg_in']} and {m['f']['sg_out']} on SayGM. Prices on open models move often, so check again before you switch.")
            q2 = (f"Is SayGM cheaper for {m['name']} at 10M input and 2M output tokens a month?",
                  f"Not at the moment: about {money(s)} on SayGM against {money(o)} on OpenRouter.")
        mfaq = [
            faq_item(f"What is the cheapest way to use {m['name']} through an API?", q1),
            faq_item(*q2),
            faq_item(f"Is {m['name']} on SayGM private?", tee_txt),
            faq_item("Do you earn money from this page?", "Yes. SayGM rewards us when someone signs up through our links and adds credit. Your price is the same either way."),
        ]
        murl = f"{domain}/models/{m['slug']}/"
        mtitle = (f"{m['name']} API: {m['save']}% cheaper on SayGM than OpenRouter" if m["cheaper"]
                  else f"{m['name']} API prices: SayGM vs OpenRouter")
        mpage = {
            "title": f"Cheapest {m['name']} API: SayGM vs OpenRouter (2026)",
            "description": (f"{m['name']} costs {m['f']['sg_in']} / {m['f']['sg_out']} per 1M tokens on SayGM vs {m['f']['or_in']} / {m['f']['or_out']} on OpenRouter with fees: "
                            + (f"{m['save']}% less." if m["cheaper"] else "OpenRouter is cheaper now.")),
            "og_title": mtitle, "url": murl,
            "jsonld": jsonld([
                {"@type": "Article", "headline": mtitle, "dateModified": data["checked"], "author": author_ld(site),
                 "publisher": {"@type": "Organization", "name": site["brand"]}, "image": domain + "/og.png", "mainEntityOfPage": murl},
                {"@type": "BreadcrumbList", "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "SayGM vs OpenRouter", "item": domain + "/"},
                    {"@type": "ListItem", "position": 2, "name": m["name"]}]},
                faq_ld(mfaq)]),
        }
        brow = next((r for r in brows if r["slug"] == m["slug"]), None)
        d = out / "models" / m["slug"]
        d.mkdir(parents=True)
        (d / "index.html").write_text(env.get_template("model.html").render(
            **{**common, "site": tagged("model")}, page=mpage, m=m, faq=mfaq, bench_row=brow, js_data=js_data(m["slug"]), bs=by_slug))

    # ---- blog ----
    ex = token_examples(by_slug, fee)
    sol = by_slug.get("gpt-6-2-sol")
    sol_below = round((1 - (MIX_IN * sol["saygm"]["in"] + MIX_OUT * sol["saygm"]["out"]) / (MIX_IN * sol["list"]["in"] + MIX_OUT * sol["list"]["out"])) * 100) if sol else None
    blog_common = {k: v for k, v in common.items() if k != "calc"}
    blog_common["site"] = tagged("blog")
    for p in POSTS:
        p["url"] = f"{domain}/blog/{p['slug']}/"
        p["date_human"] = human_date(p["date"])
    for p in POSTS:
        post_image = f"{domain}/og-{p['slug']}.png" if (ROOT / "static" / f"og-{p['slug']}.png").exists() else None
        ppage = {
            "title": p.get("seo_title", p["title"]), "description": p["description"], "og_title": p["title"], "url": p["url"],
            "image": post_image,
            "jsonld": jsonld([
                {"@type": "BlogPosting", "headline": p["title"], "datePublished": p["date"], "dateModified": data["checked"],
                 "author": author_ld(site), "publisher": {"@type": "Organization", "name": site["brand"]},
                 "image": post_image or domain + "/og.png", "mainEntityOfPage": p["url"]},
                {"@type": "BreadcrumbList", "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Blog", "item": domain + "/blog/"},
                    {"@type": "ListItem", "position": 2, "name": p["short"]}]}]),
        }
        d = out / "blog" / p["slug"]
        d.mkdir(parents=True)
        (d / "index.html").write_text(env.get_template(p["template"]).render(
            **blog_common, calc=False, page=ppage, post=p, posts=POSTS, ex=ex, sol=sol, sol_below=sol_below, bs=by_slug))
    bpage = {"title": f"Token-saving blog | {site['brand']}",
             "description": "Practical ways to cut LLM API costs: caching, routing, batching and pricing, with worked examples.",
             "og_title": f"{site['brand']} blog: spend less per token", "url": domain + "/blog/",
             "jsonld": jsonld([{"@type": "Blog", "name": f"{site['brand']} blog", "url": domain + "/blog/"}])}
    (out / "blog" / "index.html").write_text(env.get_template("blog.html").render(
        **blog_common, calc=False, page=bpage, posts=POSTS))

    # ---- free skill page ----
    surl = domain + "/skill/"
    stitle = "LLM cost audit: free AI skill that finds wasted tokens"
    spage = {"title": f"Free LLM Cost Audit Skill for Claude | {site['brand']}",
             "description": "Free Agent Skill that scans your code's LLM API calls for wasted tokens (caching, routing, batching) and estimates each fix's monthly saving.",
             "og_title": stitle, "url": surl,
             "jsonld": jsonld([{"@type": "SoftwareApplication", "name": "llm-cost-audit", "applicationCategory": "DeveloperApplication",
                                "operatingSystem": "Any", "softwareVersion": "1.3", "license": "https://opensource.org/licenses/MIT",
                                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
                                "author": {"@type": "Organization", "name": site["brand"]}, "url": surl}])}
    (out / "skill").mkdir()
    (out / "skill" / "index.html").write_text(env.get_template("skill.html").render(
        **{**blog_common, "site": tagged("skill")}, calc=False, page=spage, skill_prices_human=human_date("2026-11-26")))

    # ---- about page ----
    aurl = domain + "/about/"
    apage = {"title": f"About {site['brand']}: how we check prices and earn money",
             "description": "Who runs LessPerToken, how the daily price checks work, how the site earns money, and how to reach us or report a wrong price.",
             "og_title": f"About {site['brand']}", "url": aurl,
             "jsonld": jsonld([{"@type": "AboutPage", "name": f"About {site['brand']}", "url": aurl,
                                "about": {"@id": domain + "/#org"}, "dateModified": data["checked"]}])}
    (out / "about").mkdir()
    (out / "about" / "index.html").write_text(env.get_template("about.html").render(
        **{**common, "site": tagged("about"), "calc": False}, page=apage, posts=POSTS, n_models=len(models)))

    # ---- free audit request page ----
    qurl = domain + "/audit/"
    qpage = {"title": "Free LLM Cost Audit of Your GitHub Repo | " + site["brand"],
             "description": "Send a public GitHub repo that calls Claude, GPT or Gemini. We read its LLM calls by hand and reply with fixes and what each saves per month. Free.",
             "og_title": "Free LLM cost audit of your repo", "url": qurl,
             "jsonld": jsonld([{"@type": "WebPage", "name": "Free LLM cost audit", "url": qurl,
                                "publisher": {"@id": domain + "/#org"}}])}
    (out / "audit").mkdir()
    (out / "audit" / "index.html").write_text(env.get_template("audit.html").render(
        **{**common, "site": tagged("audit"), "calc": False}, page=qpage, posts=POSTS))

    # ---- /go/ links: one tiny page per button, so analytics counts clicks per placement ----
    spots = ["hero", "switch", "calc", "footer", "toc"]
    go_tpl = ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow">'
              '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Opening SayGM</title>'
              '<meta http-equiv="refresh" content="3;url={u}"></head>'
              '<body style="font:18px/1.5 system-ui,sans-serif;padding:48px 24px;text-align:center">'
              '<p>Opening SayGM…</p><p><a href="{u}" rel="sponsored nofollow">Continue to SayGM</a></p>'
              '<script>addEventListener("load",function(){{setTimeout(function(){{location.replace("{u}")}},400)}})</script>'
              '</body></html>')
    for gop, key in go_ref.items():
        u = html.escape(refs.get(key, site["referral_url"]), quote=True)
        for spot in spots:
            d = out / "go" / f"{gop}-{spot}"
            d.mkdir(parents=True)
            (d / "index.html").write_text(go_tpl.format(u=u))

    # ---- sitemap, robots, static files ----
    urls = ([domain + "/"] + [f"{domain}/models/{m['slug']}/" for m in models]
            + [domain + "/blog/"] + [p["url"] for p in POSTS] + [surl, aurl, qurl])
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
        "".join(f"  <url><loc>{u}</loc><lastmod>{data['checked']}</lastmod></url>\n" for u in urls) + "</urlset>\n")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nDisallow: /go/\nSitemap: {domain}/sitemap.xml\n")
    for f in (ROOT / "static").iterdir():
        shutil.copy(f, out / f.name)
    print(f"Built {len(urls)} pages into {out}")


if __name__ == "__main__":
    main()
