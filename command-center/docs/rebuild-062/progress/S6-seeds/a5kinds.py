"""Transitions in A5 / B7 'rows differ' lists (S6 triage). Usage: a5kinds.py <jsonl...> (best with the uncapped scratch oracle)"""
import ast, collections, json, re, sys
def side(s):
    s = s.strip()
    if s == 'None': return 'absent'
    t = ast.literal_eval(s)
    return 'held' if t[0] else 'readable'
for f in sys.argv[1:]:
    for l in open(f):
        d = json.loads(l)
        for p in d['problems']:
            m = re.match(r'^(A5|B7) seed (\d+): (.*?): (\d+) rows differ: (.*)$', p, re.S)
            if not m: continue
            c = collections.Counter(); ex = {}
            for item in m.group(5).split('; '):
                mm = re.match(r'^(\S+): (None|\(.*?\)) -> (None|\(.*\))$', item)
                if not mm: c['unparsed'] += 1; continue
                a, b = side(mm.group(2)), side(mm.group(3))
                k = '%s->%s' % (a, b)
                if a == b == 'readable': k += ' (text %s)' % ('same' if ast.literal_eval(mm.group(2))[3] == ast.literal_eval(mm.group(3))[3] else 'differs')
                c[k] += 1; ex.setdefault(k, item[:200])
            print(d['seed'], m.group(1), m.group(3)[:60], m.group(4), dict(c))
            for k, v in ex.items():
                if k.startswith('held->readable') or 'differs' in k or k == 'absent->readable': print('   e.g.', k, v)
