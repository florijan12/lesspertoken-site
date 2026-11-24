#!/usr/bin/env python3
"""Send the same prompts through SayGM and OpenRouter and record speed, reliability and cost.

  export SAYGM_API_KEY=...  OPENROUTER_API_KEY=...
  python bench.py --location "Ljubljana, Slovenia"                 # default models, 10 requests each
  python bench.py --models claude-sonnet-5-5 gemini-4-neon --n 20

Shows an estimated spend and asks before sending anything. Results go to data/bench.json,
which build.py turns into the "How we test" table. Run it again any time; results merge by model.
"""
import argparse, datetime as dt, json, os, pathlib, random, statistics, sys, time

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Install the OpenAI SDK first: pip install openai")

ROOT = pathlib.Path(__file__).parent
DEFAULT = ["claude-sonnet-5-5", "gpt-6-2-sol", "gemini-4-neon", "deepseek-v4-1-flash-1017"]
PROMPTS = [
    "Explain in five sentences how a hash map handles collisions.",
    "Write a Python function that returns the n-th Fibonacci number iteratively, with a docstring.",
    "Summarise the trade-offs between REST and GraphQL for a small team, as six bullet points.",
]
APPROX_PROMPT_TOKENS = 40


def run_one(client, model, prompt, max_tokens, extra):
    t0 = time.perf_counter()
    ttft, text, usage = None, [], None
    stream = client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens,
        temperature=0, stream=True, stream_options={"include_usage": True}, extra_body=extra or None)
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
            if ttft is None:
                ttft = time.perf_counter() - t0
            text.append(chunk.choices[0].delta.content)
        if getattr(chunk, "usage", None):
            usage = chunk.usage
    total = time.perf_counter() - t0
    if not text:
        raise RuntimeError("empty reply")
    return ttft, total, usage


def price_cost(usage, p, fee):
    if not usage:
        return None
    return (usage.prompt_tokens * p["in"] + usage.completion_tokens * p["out"]) / 1e6 * (1 + fee)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=DEFAULT, help="model slugs from data/models.json")
    ap.add_argument("--n", type=int, default=10, help="requests per model and gateway")
    ap.add_argument("--max-tokens", type=int, default=300)
    ap.add_argument("--location", default="", help="where you run this from, shown on the page")
    ap.add_argument("--yes", action="store_true", help="skip the spend confirmation")
    a = ap.parse_args()

    data = json.loads((ROOT / "data/models.json").read_text())
    fee = data["openrouter_card_fee"]
    by = {m["slug"]: m for m in data["models"]}
    missing = [s for s in a.models if s not in by]
    if missing:
        sys.exit(f"Unknown slugs: {missing}")
    for s in a.models:
        if not by[s].get("id_verified"):
            print(f"Warning: {by[s]['name']} SayGM ID not verified yet; run update_prices.py first.", file=sys.stderr)

    est = sum((APPROX_PROMPT_TOKENS * by[s]["saygm"]["in"] + a.max_tokens * by[s]["saygm"]["out"]) / 1e6 * a.n +
              (APPROX_PROMPT_TOKENS * by[s]["openrouter"]["in"] + a.max_tokens * by[s]["openrouter"]["out"]) / 1e6 * a.n * (1 + fee)
              for s in a.models)
    print(f"{len(a.models)} models x 2 gateways x {a.n} requests. Worst-case spend about ${est:.2f}.")
    if not a.yes and input("Continue? [y/N] ").strip().lower() != "y":
        return

    gw = {
        "saygm": (OpenAI(base_url="https://api.saygm.com/v1", api_key=os.environ["SAYGM_API_KEY"]), "saygm_id", None, 0.0),
        "openrouter": (OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"]), "openrouter_id", {"usage": {"include": True}}, fee),
    }
    out_path = ROOT / "data/bench.json"
    bench = json.loads(out_path.read_text()) if out_path.exists() else {"results": {}}

    for s in a.models:
        m = by[s]
        res = {g: {"ttft": [], "total": [], "ok": 0, "cost": [], "src": set()} for g in gw}
        jobs = [(g, PROMPTS[i % len(PROMPTS)]) for i in range(a.n) for g in gw]
        random.shuffle(jobs)  # interleave gateways so a slow minute hits both
        for g, prompt in jobs:
            client, idk, extra, g_fee = gw[g]
            try:
                ttft, total, usage = run_one(client, m[idk], prompt, a.max_tokens, extra)
                r = res[g]
                r["ok"] += 1
                r["ttft"].append(ttft)
                r["total"].append(total)
                billed = getattr(usage, "cost", None) if usage else None
                if billed is not None:
                    r["cost"].append(float(billed) * (1 + g_fee))
                    r["src"].add("billed")
                else:
                    c = price_cost(usage, m[g], g_fee)
                    if c is not None:
                        r["cost"].append(c)
                        r["src"].add("tokens x price")
            except Exception as e:
                print(f"  {m['name']} via {g}: {type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
        bench["results"][s] = {g: {
            "ttft_median_s": round(statistics.median(r["ttft"]), 3) if r["ttft"] else None,
            "total_median_s": round(statistics.median(r["total"]), 3) if r["total"] else None,
            "success_rate": round(r["ok"] / a.n, 3),
            "cost_per_request_usd": round(statistics.mean(r["cost"]), 6) if r["cost"] else None,
            "cost_source": ", ".join(sorted(r["src"])) or None,
        } for g, r in res.items()}
        sg, orr = bench["results"][s]["saygm"], bench["results"][s]["openrouter"]
        print(f"{m['name']}: first token {sg['ttft_median_s']}s vs {orr['ttft_median_s']}s, "
              f"success {sg['success_rate']:.0%} vs {orr['success_rate']:.0%}")

    bench.update({"date": dt.date.today().isoformat(), "n": a.n, "max_tokens": a.max_tokens,
                  "location": a.location or bench.get("location", "")})
    out_path.write_text(json.dumps(bench, indent=2) + "\n")
    print(f"Saved {out_path}. Run python build.py to put the results on the page.")


if __name__ == "__main__":
    main()
