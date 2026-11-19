"""S6 fresh seeds vs R5: signatures per seed and arm. Usage: compare.py <R5 rerun txt> <s6 jsonl...> [--problems]"""
import ast, collections, json, re, sys
SIGS = [
    ("A3-nib-second", r"^A3 .*second withhold.*evidence \['not_in_backup'\]"),
    ("A3-nib", r"^(A3|C3-evidence) .*evidence \['not_in_backup'\]"),
    ("A3-none", r"^(A3|C3-evidence) .*'none' "), ("A3-other", r"^(A3|C3-evidence) "),
    ("B7-restore", r"^B7 .*restore of a backup"), ("B7-rollback", r"^B7 .*rollback of X"), ("B7-reimport", r"^B7 .*re-import of X"),
    ("B1-changed", r"^B1 .*changed its text"), ("B1-disappeared", r"^B1 .*disappeared"), ("B1-readable", r"^B1 .*became readable"),
    ("O2-group-readable", r"^O2 .*never released"), ("O2-carrier", r"^O2 .*carries the withheld"),
    ("O2-copy", r"^O2 .*rollback copy"),
]
def sig(p):
    for n, rx in SIGS:
        if re.search(rx, p): return n
    return p.split(" ")[0]
args = [a for a in sys.argv[1:] if not a.startswith("--")]
before = {}
for l in open(args[0]):
    p = l.split(' ', 1)
    if len(p) == 2 and p[0].isdigit():
        before[int(p[0])] = ast.literal_eval(p[1])
after = collections.defaultdict(dict); probs = collections.defaultdict(dict)
for f in args[1:]:
    arm = 'h1' if re.search(r'f1[ab]?\.jsonl$', f) else 'h0'
    for l in open(f):
        d = json.loads(l)
        after[d['seed']][arm] = sorted({sig(p) for p in d['problems']}); probs[d['seed']][arm] = d['problems']
runs = sum(len(a) for a in after.values())
fail_runs = sum(1 for a in after.values() for v in a.values() if v)
bad = sorted(s for s, a in after.items() if any(a.values()))
print('seed runs %d, seeds %d, failing seeds %d, failing runs %d' % (runs, len(after), len(bad), fail_runs))
cnt = collections.defaultdict(set)
for s, a in after.items():
    for arm, ss in a.items():
        for x in ss: cnt[x].add((s, arm))
for k, xs in sorted(cnt.items(), key=lambda x: -len(x[1])):
    print('%-20s %4d runs %3d seeds  e.g. %s' % (k, len(xs), len({x[0] for x in xs}), ' '.join('%d%s' % x for x in sorted(xs)[:10])))
new = [s for s in bad if not any(before.get(s, {}).values())]
print('failing now, passing in both arms in R5 (final oracle rerun or first run):', len(new), new)
print('R5 failing seeds now passing in both arms:', sum(1 for s in before if any(before[s].values()) and s in after and not any(after[s].values())))
for s in bad:
    print(s, dict(after[s]), '| R5:', before.get(s, {}))
if '--problems' in sys.argv:
    for s in bad:
        for arm, ps in sorted(probs[s].items()):
            for p in ps: print('%d %s %s' % (s, arm, p[:400]))
