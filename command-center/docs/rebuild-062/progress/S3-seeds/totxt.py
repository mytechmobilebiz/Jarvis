"""Seed results (runlist.sh jsonl, label ending 0a/0b = h0, 1a/1b = h1) as R5's rerun txt format. Usage: totxt.py <jsonl...>"""
import json, re, sys, collections
sys.argv = sys.argv  # noqa
SIGS = [
    ("A3-nib-second", r"^A3 .*second withhold.*evidence \['not_in_backup'\]"),
    ("A3-nib", r"^(A3|C3-evidence) .*evidence \['not_in_backup'\]"),
    ("A3-none", r"^(A3|C3-evidence) .*'none' "), ("A3-other", r"^(A3|C3-evidence) "),
    ("B7-restore", r"^B7 .*restore of a backup"), ("B7-rollback", r"^B7 .*rollback of X"), ("B7-reimport", r"^B7 .*re-import of X"),
    ("B1-changed", r"^B1 .*changed its text"), ("B1-disappeared", r"^B1 .*disappeared"), ("B1-readable", r"^B1 .*became readable"),
    ("O2-group-readable", r"^O2 .*never released"), ("O2-carrier", r"^O2 .*carries the withheld"),
]
def sig(p):
    for n, rx in SIGS:
        if re.search(rx, p): return n
    return p.split(" ")[0]
out = collections.defaultdict(dict)
for f in sys.argv[1:]:
    arm = 'h1' if re.search(r'1[ab]?\.jsonl$', f) else 'h0'
    for l in open(f):
        d = json.loads(l)
        out[d['seed']][arm] = sorted({sig(p) for p in d['problems']})
print("seed results")
for s in sorted(out):
    print(s, dict(sorted(out[s].items())))
