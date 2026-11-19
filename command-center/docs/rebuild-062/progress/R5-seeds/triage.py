"""Group the fresh seeds' problems by signature (rebuild R5). Usage: python triage.py <dir with *.jsonl> [--seeds]"""
import collections, glob, json, re, sys

SIGS = [
    ("A3-nib-second", r"^A3 .*second withhold.*evidence \['not_in_backup'\]"),
    ("A3-nib", r"^(A3|C3-evidence) .*evidence \['not_in_backup'\]"),
    ("A3-none", r"^(A3|C3-evidence) .*'none' "),
    ("A3-other", r"^(A3|C3-evidence) "),
    ("B7-restore", r"^B7 .*restore of a backup"),
    ("B7-rollback", r"^B7 .*rollback of X"),
    ("B7-reimport", r"^B7 .*re-import of X"),
    ("B1-changed", r"^B1 .*changed its text"),
    ("B1-disappeared", r"^B1 .*disappeared"),
    ("B1-readable", r"^B1 .*became readable"),
    ("B1-other", r"^B1 "),
    ("O2-group-readable", r"^O2 .*never released"),
    ("O2-carrier", r"^O2 .*carries the withheld"),
    ("O3", r"^O3 "), ("O8", r"^O8 "), ("O1", r"^O1 "), ("O6", r"^O6 "), ("O11", r"^O11 "),
    ("A1", r"^A1 "), ("A2", r"^A2 "), ("A4", r"^A4 "), ("A5", r"^A5 "),
    ("B2", r"^B2 "), ("B3", r"^B3 "), ("B4", r"^B4 "), ("B5", r"^B5 "), ("B6", r"^B6 "),
    ("review", r"^review "),
]


def sig(p):
    for name, rx in SIGS:
        if re.search(rx, p):
            return name
    return p.split(" ")[0]


def main():
    by_sig = collections.defaultdict(set)
    seeds = collections.defaultdict(dict)
    n = 0
    for f in sorted(glob.glob(sys.argv[1] + "/*.jsonl")):
        arm = "h1" if "f1" in f else "h0"
        for line in open(f):
            d = json.loads(line)
            n += 1
            ss = sorted({sig(p) for p in d["problems"]})
            seeds[d["seed"]][arm] = ss
            for s in ss:
                by_sig[s].add((d["seed"], arm))
    bad = {s for s, a in seeds.items() if any(a.values())}
    print("seed runs %d, seeds %d, failing seeds %d, failing runs %d" % (
        n, len(seeds), len(bad), sum(1 for a in seeds.values() for v in a.values() if v)))
    for s, xs in sorted(by_sig.items(), key=lambda x: -len(x[1])):
        print("%-20s %4d runs  e.g. %s" % (s, len(xs), " ".join("%d%s" % x for x in sorted(xs)[:8])))
    if "--seeds" in sys.argv:
        for s in sorted(bad):
            print(s, seeds[s])


main()
