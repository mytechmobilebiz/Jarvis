"""Pick the recorded seeds (rebuild R5): seeds that pass in both live-history arms, a few per option C shape."""
import collections, glob, json, re, sys

runs = collections.defaultdict(dict)
for f in glob.glob(sys.argv[1] + "/*.jsonl"):
    arm = "h1" if "f1" in f else "h0"
    for line in open(f):
        d = json.loads(line)
        runs[d["seed"]][arm] = d
FEATURES = {
    "delegate in a group": lambda d: any(x.startswith("delegate ") for x in d["log"]) and any("group " in n and not n.split("group ")[1].startswith("0") for n in d["notes"]),
    "markerless child": lambda d: any(x.startswith("legacy_reset") for x in d["log"]),
    "carrier holds outside groups": lambda d: any(re.search(r"carrier holds [1-9]", n) for n in d["notes"]),
    "uncertain conversations": lambda d: any(re.search(r"\(uncertain [1-9]", n) for n in d["notes"]),
    "session_search read of a withheld conversation": lambda d: any(x.startswith("session_search") for x in d["log"]),
    "pass-2 summary line": lambda d: any(x.startswith("summarize") for x in d["log"]),
    "short second withheld text": lambda d: len(d["withholds"]) > 1 and len(" ".join(d["withholds"][-1][2].split())) < 16,
    "restore round trip": lambda d: d["seed"] % 25 == 0,
    "one-click release exercised": lambda d: any(re.search(r"B6 exercised in .*: 200, [1-9]", n) for n in d["notes"]),
    "two withholds": lambda d: len(d["withholds"]) >= 2,
}
picked = collections.OrderedDict()
for name, fn in FEATURES.items():
    n = 0
    for seed in sorted(runs):
        arms = runs[seed]
        if len(arms) < 2 or any(a["problems"] for a in arms.values()):
            continue
        if all(fn(a) for a in arms.values()):
            picked.setdefault(seed, []).append(name)
            n += 1
            if n >= 3:
                break
for s, why in picked.items():
    print(s, "; ".join(why))
print("RECORDED_SEEDS =", tuple(picked))
