#!/usr/bin/env python3
"""One-off: print what the SayGM and OpenRouter model lists look like, to fix field and ID matching."""
import json, os
from update_prices import get_json, items, SAYGM_URL, OPENROUTER_URL

sg = items(get_json(SAYGM_URL, os.environ.get("SAYGM_API_KEY")))
print(f"SayGM entries: {len(sg)}")
print("All SayGM ids:", sorted(str(m.get("id")) for m in sg))
for want in ("sonnet-5", "neon", "argon", "deepseek-v4", "qwen3", "gpt-6", "kimi", "glm"):
    hit = next((m for m in sg if want in str(m.get("id", "")).lower()), None)
    print(f"\n=== SayGM entry matching '{want}' ===")
    print(json.dumps(hit, indent=1)[:2500] if hit else "none")

orr = items(get_json(OPENROUTER_URL))
for want in ("deepseek-v4.1-flash", "deepseek-v4-1-flash"):
    for m in orr:
        if want in m.get("id", ""):
            print(f"\n=== OpenRouter {m['id']} ===")
            print(json.dumps({"id": m["id"], "pricing": m.get("pricing")}, indent=1))
