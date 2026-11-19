"""Before/after per signature for the S4 class seeds (A3-other, A3-none, A3-nib, B6, A4, B4). Usage: compare.py <before rerun txt> <jsonl...>"""
import ast, glob, json, re, sys, collections
sys.path.insert(0, '/home/user/Jarvis/.worktrees/brain-062C/command-center/docs/rebuild-062/progress/R5-seeds')
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
before = {}
for l in open(sys.argv[1]):
    p = l.split(' ', 1)
    if len(p) == 2 and p[0].isdigit():
        before[int(p[0])] = ast.literal_eval(p[1])
after = collections.defaultdict(dict)
for f in sys.argv[2:]:
    arm = 'h1' if re.search(r'1[ab]?\.jsonl$', f) else 'h0'
    for l in open(f):
        d = json.loads(l)
        after[d['seed']][arm] = sorted({sig(p) for p in d['problems']})
cls = lambda ss: [s for s in ss if s in ('A3-other', 'A3-none', 'A3-nib', 'B6', 'A4', 'B4')]
cnt = collections.Counter()
for s in sorted(after):
    for arm in ('h0', 'h1'):
        b = before.get(s, {}).get(arm, [])
        a = after[s].get(arm)
        if a is None: continue
        cnt['runs'] += 1
        for k in set(b) | set(a):
            cnt[(k, 'before')] += k in b
            cnt[(k, 'after')] += k in a
        cnt['fail before'] += bool(b); cnt['fail after'] += bool(a)
        cnt['class before'] += bool(cls(b)); cnt['class after'] += bool(cls(a))
print('runs', cnt['runs'], 'failing runs before/after', cnt['fail before'], cnt['fail after'], 'class (A3-other/A3-none/A3-nib/B6/A4/B4) runs before/after', cnt['class before'], cnt['class after'])
for k in sorted({k[0] for k in cnt if isinstance(k, tuple)}):
    print('%-20s before %3d after %3d' % (k, cnt[(k, 'before')], cnt[(k, 'after')]))
seeds_fail = sorted(s for s in after if any(after[s].get(a) for a in after[s]))
print('seeds failing after (any arm):', len(seeds_fail), 'of', len(after))
if "--all" in sys.argv[1:] or True:
    for s in sorted(after):
        a = after[s]
        if any(cls(v) for v in a.values()):
            print(s, {k: v for k, v in a.items()})
