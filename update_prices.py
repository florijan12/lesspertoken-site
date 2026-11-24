#!/usr/bin/env python3
"""Refresh SayGM and OpenRouter prices in data/models.json.

  python update_prices.py --list      # show model IDs on both gateways that match ours
  python update_prices.py --inspect   # print one raw SayGM model entry (to check field names)
  python update_prices.py             # update prices, set today's date
  python update_prices.py --dry-run   # show changes without writing

Set SAYGM_API_KEY if SayGM's /v1/models asks for a key. OpenRouter's list is public.
Prices that move by more than 60% are skipped as a safety check; pass --force to accept them.
"""
import argparse, datetime as dt, json, os, pathlib, re, sys, urllib.request

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data/models.json"
SAYGM_URL = "https://api.saygm.com/v1/models"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"

IN_KEYS = ("prompt", "input", "input_price", "prompt_price", "input_per_million")
OUT_KEYS = ("completion", "output", "output_price", "completion_price", "output_per_million")
CACHE_KEYS = ("input_cache_read", "cached_input", "cache_read", "input_cached", "cached_prompt")


def get_json(url, key=None):
    req = urllib.request.Request(url, headers={"User-Agent": "lesspertoken/1.0", "Accept": "application/json"})
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def per_million(v):
    """Accept per-token or per-million numbers/strings and return USD per 1M tokens."""
    if v is None or v == "":
        return None
    v = float(v)
    if v < 0:
        return None
    return round(v * 1_000_000, 6) if v < 1e-4 else round(v, 6)


def find_prices(obj, depth=0):
    """Find the first dict (breadth-first) that has an input-like and an output-like key."""
    if depth > 4 or not isinstance(obj, dict):
        return None
    pick = lambda keys: next((obj[k] for k in keys if k in obj), None)
    i, o = pick(IN_KEYS), pick(OUT_KEYS)
    if i is not None and o is not None and not isinstance(i, (dict, list)):
        return {"in": per_million(i), "out": per_million(o), "cache": per_million(pick(CACHE_KEYS))}
    for k in ("pricing", "price", "prices", "effective", "saygm", "rates"):
        if isinstance(obj.get(k), dict):
            r = find_prices(obj[k], depth + 1)
            if r:
                return r
    for v in obj.values():
        if isinstance(v, dict):
            r = find_prices(v, depth + 1)
            if r:
                return r
    return None


def saygm_prices(e):
    """SayGM lists prices in nano-dollars per 1M tokens under pricing.dimensions."""
    dim = (e.get("pricing") or {}).get("dimensions") or {}
    if "input_per_mtok_ndollars" in dim and "output_per_mtok_ndollars" in dim:
        nd = lambda k: None if dim.get(k) is None else round(dim[k] / 1e9, 6)
        return {"in": nd("input_per_mtok_ndollars"), "out": nd("output_per_mtok_ndollars"),
                "cache": nd("cache_read_per_mtok_ndollars")}
    return find_prices(e)


def items(payload):
    return payload.get("data", payload if isinstance(payload, list) else [])


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    data = json.loads(DATA.read_text())
    sg = items(get_json(SAYGM_URL, os.environ.get("SAYGM_API_KEY")))
    orr = items(get_json(OPENROUTER_URL))
    sg_by = {m.get("id"): m for m in sg}
    or_by = {m.get("id"): m for m in orr}

    if a.inspect:
        print(json.dumps(sg[0] if sg else {}, indent=2))
        return
    if a.list:
        for m in data["models"]:
            key = norm(m["name"])
            sgm = [i for i in sg_by if i and key in norm(i)]
            orm = [i for i in or_by if i and key in norm(i.split("/")[-1])]
            print(f"{m['name']}\n  ours:       saygm={m['saygm_id']}  openrouter={m['openrouter_id']}\n  SayGM has:  {sgm or '-'}\n  OpenRouter: {orm or '-'}")
        return

    changed, warnings, problems = [], [], []

    def apply(m, side, new):
        if not new or new["in"] is None or new["out"] is None:
            problems.append(f"{m['name']}: no {side} price found in API entry")
            return
        old = m[side]
        for k in ("in", "out", "cache"):
            nv = new.get(k)
            if nv is None:
                continue
            ov = old.get(k)
            if ov and not a.force and abs(nv - ov) / ov > 0.6:
                problems.append(f"{m['name']} {side}.{k}: {ov} -> {nv} held back (moved >60%; check it, then run with --force)")
                continue
            if ov != nv:
                changed.append(f"{m['name']} {side}.{k}: {ov} -> {nv}")
                old[k] = nv

    for m in data["models"]:
        e = sg_by.get(m["saygm_id"])
        if e:
            m["id_verified"] = True
            if e.get("tier"):
                m["tier"] = e["tier"]
            apply(m, "saygm", saygm_prices(e))
        else:
            m["id_verified"] = False
            problems.append(f"{m['name']}: SayGM ID '{m['saygm_id']}' not found (run --list)")
        e = or_by.get(m["openrouter_id"])
        if e:
            apply(m, "openrouter", find_prices(e))
            m["or_estimate"] = False if m["maker"] != "Open models" else m.get("or_estimate", True)
        else:
            problems.append(f"{m['name']}: OpenRouter ID '{m['openrouter_id']}' not found (run --list)")

    print("\n".join(changed) or "No price changes.")
    if warnings:
        print("\nNotes:\n  " + "\n  ".join(warnings))
    if problems:
        # Never stamp today's date on prices we couldn't confirm. Nothing is saved, the run fails,
        # and the live site keeps its previous (honest) date until someone looks.
        print("\nNOT SAVED. These prices could not be confirmed:\n  " + "\n  ".join(problems), file=sys.stderr)
        sys.exit(1)
    if not a.dry_run:
        data["checked"] = dt.date.today().isoformat()
        DATA.write_text(json.dumps(data, indent=2) + "\n")
        print(f"\nSaved {DATA.name}, checked = {data['checked']}")


if __name__ == "__main__":
    main()
