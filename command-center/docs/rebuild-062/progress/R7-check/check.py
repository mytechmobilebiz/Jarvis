"""R7 check runner: runs every test items.py names on i22 and on HEAD, one pytest per file and tree, and records each
test's outcome (results-<tree>.json). Trees:
  HEAD: the rebuilt worktree (command-center/backend);
  I:    i22 (28e9c3d) with HEAD's tests/ overlaid (i22's product, migrations and app; HEAD's tests and helpers);
  O:    i22 with its own tests/ and only HEAD's oracle self-test files added (the oracle under test is i22's).
Usage: python check.py setup <i22_worktree> <o_dir>
       python check.py run <tree> <backend_dir> <db_label> <out.json>
       python check.py table <results_head.json> <results_i22.json> > table.md
Environment: PY (the venv python), HERMES_SRC; TEST_DATABASE_URL and the CR URL are set per tree from <db_label>."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from items import CONTROLS, ITEMS, MATRIX  # noqa: E402

HEAD = os.path.abspath(os.path.join(HERE, "../../../../backend"))
SELFTESTS = ("selftest_kit.py",)
DIRS = {"I": "integration", "U": "unit", "P": "property"}


def oracle_selftest(file: str) -> bool:
    return file.startswith("test_oracle_") and file != "test_oracle_properties.py"


def all_tests() -> list[str]:
    out: list[str] = []
    for it in ITEMS:
        if isinstance(it[3], list):
            out += it[3]
    for row in MATRIX:
        out += row[1]
    return sorted(set(out))


def split(t: str) -> tuple[str, str, str]:
    kind, rest = t.split(":", 1)
    file, _, fn = rest.partition("::")
    return kind, file, fn


def setup(i22: str, o_dir: str) -> None:
    be = os.path.join(i22, "command-center/backend")
    shutil.rmtree(os.path.join(be, "tests"))
    shutil.copytree(os.path.join(HEAD, "tests"), os.path.join(be, "tests"),
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    if os.path.exists(o_dir):
        shutil.rmtree(o_dir)
    os.makedirs(o_dir)
    subprocess.run("git -C %s archive 28e9c3d command-center | tar -x -C %s" % (i22, o_dir), shell=True, check=True,
                   executable="/bin/bash")
    prop = os.path.join(o_dir, "command-center/backend/tests/property")
    for f in os.listdir(os.path.join(HEAD, "tests/property")):
        if oracle_selftest(f) or f in SELFTESTS:
            shutil.copy(os.path.join(HEAD, "tests/property", f), os.path.join(prop, f))


def run(tree: str, backend: str, db: str, out: str) -> None:
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = "postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s" % db
    env["TEST_CREDIT_REPAIR_DATABASE_URL"] = "postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s_cr" % db
    env["REQUIRE_HERMES_SRC"] = "1"
    by_file: dict[tuple[str, str], list[str]] = {}
    for t in all_tests():
        kind, file, fn = split(t)
        if tree == "O" and not (kind == "P" and oracle_selftest(file)):
            continue
        if tree == "I" and kind == "P" and oracle_selftest(file):
            continue
        by_file.setdefault((kind, file), []).append(fn)
    results: dict[str, dict] = json.load(open(out)) if os.path.exists(out) else {}
    for (kind, file), fns in sorted(by_file.items()):
        rel = "tests/%s/%s" % (DIRS[kind], file)
        if all(("%s:%s::%s" % (kind, file, fn) if fn else "%s:%s" % (kind, file)) in results for fn in fns):
            continue  # resumable
        xml = out + ".%s.xml" % file
        nodes = [rel + ("::" + fn if fn else "") for fn in sorted(set(fns))]
        done = subprocess.run([env.get("PY", sys.executable), "-m", "pytest", "-p", "no:randomly", "-q", "-rN",
                               "--continue-on-collection-errors", "--junitxml=" + xml, *nodes],
                              cwd=backend, env=env, capture_output=True, text=True, timeout=7200)
        tail = (done.stdout.strip().splitlines() or [""])[-1]
        cases: dict[str, list[str]] = {}
        collect_error = ""
        if os.path.exists(xml):
            for tc in ET.parse(xml).getroot().iter("testcase"):
                name = re.sub(r"\[.*$", "", tc.get("name", ""))
                verdict = "P"
                for child in tc:
                    if child.tag in ("failure", "error"):
                        verdict = "F" if child.tag == "failure" else "E"
                        if not tc.get("name"):
                            collect_error = (child.get("message") or "")[:200]
                    elif child.tag == "skipped":
                        verdict = "S"
                if not tc.get("name") or tc.get("name") == file.replace(".py", ""):
                    collect_error = collect_error or "collection error"
                    continue
                cases.setdefault(name, []).append(verdict)
            os.remove(xml)
        if not cases and not collect_error and done.returncode != 0:
            collect_error = "no test ran: " + (done.stdout.strip().splitlines() or [""])[-1][:200]
        if "error" in tail and "collect" in done.stdout:
            m = re.search(r"E\s+(\w+Error: .{0,160})", done.stdout)
            collect_error = collect_error or (m.group(1) if m else "collection error")
        for fn in sorted(set(fns)):
            key = "%s:%s::%s" % (kind, file, fn) if fn else "%s:%s" % (kind, file)
            got = [v for n, vs in cases.items() if not fn or n == fn for v in vs]
            results[key] = {"outcomes": got, "collect_error": collect_error if not got else "", "summary": tail}
        with open(out, "w") as f:
            json.dump(results, f, indent=1, sort_keys=True)
        print(tree, file, tail, flush=True)


def verdict(res: dict | None) -> str:
    if res is None:
        return "not run"
    o = res["outcomes"]
    if not o:
        err = res["collect_error"] or "not collected"
        m = re.search(r"cannot import name '(\w+)' from '([\w.]+)'", err) or re.search(
            r"module '([\w.]+)' has no attribute '(\w+)'", err)
        if m and "cannot import" in err:
            err = "no `%s` in %s" % (m.group(1), m.group(2))
        elif m:
            err = "no `%s` in %s" % (m.group(2), m.group(1))
        return "fails at import (%s)" % err
    if all(v == "P" for v in o):
        return "passes" if len(o) == 1 else "passes (%d)" % len(o)
    bad = sum(v in ("F", "E") for v in o)
    if bad:
        return "fails (%d of %d)" % (bad, len(o))
    return "skipped (%d)" % len(o)


def table(head_json: str, i22_json: str, o_json: str) -> None:
    head = json.load(open(head_json))
    i22 = {**json.load(open(i22_json)), **json.load(open(o_json))}

    def show(tests: list[str]) -> tuple[str, str, bool, bool]:
        hv, iv = [], []
        i_fail = True
        h_pass = True
        for t in tests:
            h, i = head.get(t), i22.get(t)
            hs, is_ = verdict(h), verdict(i)
            h_pass &= hs.startswith("passes")
            if t not in CONTROLS:
                i_fail &= is_.startswith("fails")
            name = t.split("::")[-1] if "::" in t else t
            hv.append(hs)
            iv.append(is_ + (" [control]" if t in CONTROLS else ""))
        return "<br>".join(iv), "<br>".join(hv), i_fail, h_pass

    def name(t: str) -> str:
        kind, file, fn = split(t)
        return "`%s/%s%s`" % (DIRS[kind], file, "::" + fn if fn else "")

    print("| item | test | i22 | HEAD | note |")
    print("|---|---|---|---|---|")
    summary = {"ok": 0, "lane": 0, "none": 0, "bad": []}
    for iid, src, what, tests, note in ITEMS:
        if tests == "LANE" or tests == "VITEST":
            print("| %s (%s) %s | %s | see note | see note | %s |" % (iid, src, what, tests.lower(), note))
            summary["lane"] += 1
            continue
        if not tests:
            print("| %s (%s) %s | none | - | - | %s |" % (iid, src, what, note))
            summary["none"] += 1
            continue
        iv, hv, i_fail, h_pass = show(tests)
        ok = i_fail and h_pass
        summary["ok" if ok else "bad"] += 1 if ok else 0
        if not ok:
            summary["bad"].append(iid)
        print("| %s (%s) %s | %s | %s | %s | %s%s |" % (iid, src, what, "<br>".join(name(t) for t in tests), iv, hv,
                                                       "" if ok else "**CHECK** ", note))
    print()
    print("| matrix row | test | i22 | HEAD | note |")
    print("|---|---|---|---|---|")
    for guard, tests, note in MATRIX:
        if not tests:
            print("| %s | none | - | - | %s |" % (guard, note))
            summary["none"] += 1
            continue
        iv, hv, i_fail, h_pass = show(tests)
        ok = i_fail and h_pass
        if ok:
            summary["ok"] += 1
        else:
            summary["bad"].append(guard)
        print("| %s | %s | %s | %s | %s%s |" % (guard, "<br>".join(name(t) for t in tests), iv, hv,
                                               "" if ok else "**CHECK** ", note))
    print()
    print("<!-- summary %s -->" % json.dumps(summary))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "setup":
        setup(sys.argv[2], sys.argv[3])
    elif cmd == "run":
        run(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif cmd == "table":
        table(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "list":
        print("\n".join(all_tests()))
