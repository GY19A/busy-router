"""Offline eval of the busy-router classifier against hand labels.

Gate, fixed before any run: A->C == 0 (a correction never leaves the running task),
accuracy >= 0.90, and accuracy above the majority-class control.

Usage: BUSY_ROUTER_ENDPOINT=... BUSY_ROUTER_MODEL=... BUSY_ROUTER_KEY_ENV=... \
       python eval.py labels.jsonl results.json
"""
import collections, importlib.util, json, os, pathlib, sys, time

here = pathlib.Path(__file__).resolve().parent
plugin = os.environ.get("BUSY_ROUTER_PLUGIN", str(here.parent / "busy-router" / "__init__.py"))
spec = importlib.util.spec_from_file_location("busy_router", plugin)
br = importlib.util.module_from_spec(spec); spec.loader.exec_module(br)
endpoint, model = os.environ["BUSY_ROUTER_ENDPOINT"], os.environ["BUSY_ROUTER_MODEL"]
key = br._api_key(os.environ.get("BUSY_ROUTER_KEY_ENV", "OPENAI_API_KEY"))

rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8")]
out, conf, lat = [], collections.Counter(), []
for r in rows:
    t = time.monotonic()
    try:
        lab, probs = br.classify(r["task"], r["msg"], endpoint, model, key, timeout=15)
    except Exception as e:
        lab, probs = "ERR", {"err": str(e)}
    lat.append((time.monotonic() - t) * 1000)
    conf[(r["label"], lab)] += 1
    out.append({**r, "pred": lab, "probs": {k: round(v, 3) for k, v in probs.items()} if lab != "ERR" else probs})
n = len(rows); ok = sum(1 for o in out if o["pred"] == o["label"])
maj = collections.Counter(r["label"] for r in rows).most_common(1)[0]
lat.sort()
print(f"n={n} acc={ok/n:.3f} majority_control({maj[0]})={maj[1]/n:.3f}  p50={lat[n//2]:.0f}ms p90={lat[int(n*.9)]:.0f}ms")
print("A->C (must be 0):", conf[("A", "C")], " C->A:", conf[("C", "A")])
for g in "ABCD":
    print(g, {p: conf[(g, p)] for p in "ABCD" if conf[(g, p)]})
for o in out:
    if o["pred"] != o["label"]:
        print(f"  MISS gold={o['label']} pred={o['pred']} {o['probs']} | {o['msg'][:70]}")
json.dump(out, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
