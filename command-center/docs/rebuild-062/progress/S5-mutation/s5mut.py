"""S5 mutation check (rebuild S5: the R7 gaps): each guard (or a pair, for a compensated one) switched off on its own
copy of the worktree's backend; its killing test must FAIL with an assertion (never an error or a collection problem).
Usage: python s5mut.py [names...]; results appended to results.tsv. Names follow the sources' matrix where it names
the guard (gd1:*, rel3:*, cr2:*, c2:*, m18:*) and r3:* for the rebuild's own (R3)."""
import os, shutil, subprocess, sys
WT = "/home/user/Jarvis/.worktrees/brain-062C/command-center"
S = os.path.dirname(os.path.abspath(__file__))
PY = "/home/user/rebuild/venv/bin/python"
ID, RV, GR, IM = "app/brain/identity.py", "app/brain/review.py", "app/brain/groups.py", "app/brain/importer.py"
CA = "tests/property/cc_adapter.py"
OR = "tests/property/oracle.py"
SQL = "app/migrations/0008_brain_withhold_identity.sql"
I5 = "tests/integration/test_brain_mutation_closure_s5_062.py"
IR = "tests/integration/test_brain_releases_r2_062.py"
ID1 = "tests/integration/test_brain_groups_d1_062.py"
U5 = "tests/unit/test_brain_mutation_closure_s5_062.py"
UC = "tests/unit/test_brain_carriers_r2_062.py"
P5 = "tests/property/test_oracle_rollback_copies_s5.py"
# the import's carrier pass (review.hold_carriers, run by every import's sweep with the backup's withheld texts in hand)
# skips the rows of conversations a group records, as if the group sweep alone had them (round-3 importer.py:1922)
GROUP_SKIP = (RV, "            if row.withheld:\n                continue\n            for anchor, rel in cands[int(row.id)]:",
              "            if row.withheld or await session.scalar(select(BrainWithheldSession.source_session_id).where(\n"
              "                    BrainWithheldSession.source_session_id == sid).limit(1)):\n                continue\n"
              "            for anchor, rel in cands[int(row.id)]:")
FOREIGN_OFF = (GR, "                if rel and not await identity.exempt_covers(s, other, sid, row):", "                if False:")
DEC_JSON_OFF = (ID, "    if _JSON_DOC.match(text) and (text.lstrip()[:1] != '\"' or \"\\\\\" in text):", "    if False:")
PH = "tests/property/test_hermes_derived3_lines.py"


def _r3_mutants(*names):
    """R3's mutants by name (its M list, read without running its loop)."""
    import ast
    tree = ast.parse(open(os.path.join(S, "..", "R3-mutation", "r3mut.py")).read())
    env = {"os": os, "__file__": os.path.join(S, "..", "R3-mutation", "r3mut.py")}
    for node in tree.body:
        if isinstance(node, ast.Assign) and not any(isinstance(t, ast.Name) and t.id in ("only", "db", "env", "out")
                                                    for t in node.targets):
            exec(compile(ast.Module([node], []), "r3mut", "exec"), env)
        elif isinstance(node, ast.Expr) and "M.append" in ast.unparse(node):
            exec(compile(ast.Module([node], []), "r3mut", "exec"), env)
    found = {n: (n, e, t) for n, e, t in env["M"]}
    return [found[n] for n in names]


M = [
 ("r3:release_on_evidence_carrier_off", [(GR, "RELEASE_ON_EVIDENCE = frozenset({identity.GROUP_HOLD, identity.UNCERTAIN_HOLD, identity.COPY_HOLD,\n                                 identity.CARRIER_HOLD})", "RELEASE_ON_EVIDENCE = frozenset({identity.GROUP_HOLD, identity.UNCERTAIN_HOLD, identity.COPY_HOLD})")],
  [I5 + "::test_s5_a_release_of_a_carrier_hold_covers_the_content_it_was_compared_on"]),
 ("rel3:imp_carriers_group_skip", [GROUP_SKIP],
  [I5 + "::test_s5_a_typed_release_of_an_uncertain_row_after_a_later_withhold_is_held_again_by_the_next_import"]),
 ("rel3:imp_carriers_group_skip(member test)", [GROUP_SKIP],
  [IR + "::test_rel3_typed_release_after_a_later_withhold_is_held_again_by_the_next_import"]),
 ("rel3:imp_carriers_group_skip+rel3:hgr_foreign_off", [GROUP_SKIP, FOREIGN_OFF],
  [IR + "::test_rel3_typed_release_after_a_later_withhold_is_held_again_by_the_next_import"]),
 ("rel3:flip_any_anchor", [(ID, "                            BrainWithholdExemption.anchor_key == group,\n                            BrainWithholdExemption.source_session_id == here.session,",
                            "                            BrainWithholdExemption.source_session_id == here.session,")],
  [IR + "::test_rel3_typed_release_after_a_later_withhold_is_held_again_by_the_next_import"]),
 ("gd1:p2_both_off", [(SQL, "                OR (x.before -> 'deleted_row' ->> 'session_id') IN (SELECT id::text FROM g)\n                OR (x.target_table = 'archive_messages' AND x.target_id::text IN (SELECT id FROM gone)))", "                )"),
                       (RV, "            \"OR x.target_id::text IN (SELECT id FROM gone))\"),", "            \")\"),")],
  [ID1 + "::test_skc_d_the_older_text_of_a_group_row_changed_then_deleted_keeps_no_text_in_any_rollback_copy",
   ID1 + "::test_p2_the_startup_repair_blanks_a_copy_of_a_changed_then_deleted_group_row"]),
 ("gd1:oracle_rollback_records_off", [(CA, "ROLLBACK_RECORDS = True", "ROLLBACK_RECORDS = False")],
  [ID1 + "::test_p2_the_oracle_names_the_conversation_of_a_copy_whose_row_was_deleted"]),
 ("s5:oracle_copies_check_off", [(OR, '    check_rollback_copies(run, where, st.get("copies"), group | gs["uncertain"], st["rows"])\n', "")],
  [P5 + "::test_s5_a_rollback_copy_of_a_group_row_with_text_is_reported"]),
 ("s5:oracle_copies_uncertain_off", [(OR, '    check_rollback_copies(run, where, st.get("copies"), group | gs["uncertain"], st["rows"])\n',
                                      '    check_rollback_copies(run, where, st.get("copies"), group, st["rows"])\n')],
  [P5 + "::test_s5_a_rollback_copy_in_an_uncertain_conversation_is_reported"]),
 ("s5:oracle_copy_released_any", [(OR, '        if released_ok(run, {"source_key": c.get("source_key"), "id": c.get("row_id"), "content": c.get("content")}):',
                                   '        if run.released.get(c.get("source_key")):')],
  [P5 + "::test_s5_a_released_rows_copy_is_admitted_only_with_exactly_the_released_text"]),
 ("s5:oracle_copy_key_place_off", [(OR, '        sid = c.get("sid") or keys.get(c.get("source_key"))', '        sid = c.get("sid")')],
  [P5 + "::test_s5_a_copy_its_source_key_places_in_the_group_is_reported_and_an_unplaced_one_is_noted"]),
 ("s5:oracle_copy_gone_name_off", [(CA, '"coalesce(sd.source_session_id, st.source_session_id, sg.source_session_id) "',
                                    '"coalesce(sd.source_session_id, st.source_session_id) "')],
  [ID1 + "::test_p2_the_oracle_names_the_conversation_of_a_copy_whose_row_was_deleted"]),
 # m18:dec_json_off: each of the five rebuilt closing tests on its own
] + [("m18:dec_json_off[%s]" % t.split("test_dec_json_")[1][:40], [DEC_JSON_OFF], [U5 + "::" + t]) for t in (
    "test_dec_json_a_short_w_as_a_whole_value_of_a_compact_json_list_is_identical",
    "test_dec_json_a_short_w_as_a_tool_call_argument_value_is_identical",
    "test_dec_json_a_short_w_in_a_session_search_result_is_identical",
    "test_dec_json_a_json_value_that_is_ws_beginning_is_a_cut_and_a_group_row_holding_it_is_never_none",
    "test_dec_json_a_short_w_as_a_whole_value_is_identical_in_a_group_rows_evidence")] + [
 ("cr2:quick_no_quote_bypass", [(ID, "        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or SNAPSHOT_LEAD.search(f) or _QUOTE_SHAPE.search(f):",
                                 "        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or SNAPSHOT_LEAD.search(f):")],
  [U5 + "::test_p2_a_cut_quoted_over_marked_lines_is_never_skipped_by_the_quick_filter"]),
 ("c2:imp_stored_none_every_off", [(RV, "    all_in_hand = (await admin_anchors(session)) <= set(texts)",
                                     "    all_in_hand = cause == \"import\" or (await admin_anchors(session)) <= set(texts)")],
  [I5 + "::test_s5_imp_stored_none_every_a_stored_group_row_the_import_sweep_holds_without_every_text_is_never_none"]),
 ("hgr:none_all_in_hand_off", [(RV, "    all_in_hand = (await admin_anchors(session)) <= set(texts)", "    all_in_hand = True")],
  [I5 + "::test_s5_imp_stored_none_every_a_stored_group_row_the_import_sweep_holds_without_every_text_is_never_none"]),
 # the N1 / N4 guards (R3's edits, progress/R3-mutation/r3mut.py) against the Hermes-generated lines
] + [(n + "[hermes_lines]", e, [PH]) for n, e, _t in _r3_mutants(
    "r3:spans_off", "c3:spans_per_line_off", "m18:dec_repr_off", "m18:dec_escape_off", "sk3:unrepr_cut_keep",
    "sk3:open_literal_off", "sk3:cut_per_line_off", "r3:cut_off", "r3:lead_cut_off")] + [
]
only = sys.argv[1:]
db = os.environ.get("S5M_DB", "s5m")
env = dict(os.environ, TEST_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s" % db,
           TEST_CREDIT_REPAIR_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s_cr" % db)
out = open(os.path.join(S, "results.tsv"), "a")
root = "/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/s5/mutcopy-%s" % db
for name, edits, tests in M:
    if only and name not in only:
        continue
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    shutil.copytree(os.path.join(WT, "backend"), os.path.join(root, "backend"),
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    if os.path.isdir(os.path.join(WT, "migration")):
        shutil.copytree(os.path.join(WT, "migration"), os.path.join(root, "migration"),
                        ignore=shutil.ignore_patterns("__pycache__"))
    bad = False
    for f, old, new in edits:
        path = os.path.join(root, "backend", f)
        text = open(path).read()
        n = text.count(old)
        if n != 1:
            print(name, "INVALID (%d matches in %s)" % (n, f), flush=True)
            out.write("%s\t%s\tINVALID %d\n" % (name, f, n)); out.flush()
            bad = True
            break
        open(path, "w").write(text.replace(old, new))
    if bad:
        continue
    r = subprocess.run([PY, "-m", "pytest", "-p", "no:randomly", "-q", "-rA", *tests], cwd=os.path.join(root, "backend"),
                       env=env, capture_output=True, text=True, timeout=3600)
    last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or " error" in l][-1:] or [r.stdout[-200:]]
    verdict = "CAUGHT" if (r.returncode == 1 and " failed" in last[0] and " error" not in last[0]) else (
        "SURVIVED" if r.returncode == 0 else "ERROR")
    print(name, verdict, last[0], flush=True)
    if verdict != "SURVIVED":
        fails = [l for l in r.stdout.splitlines() if l.startswith("FAILED") or l.startswith("ERROR") or l.startswith("E  ")]
        print("\n".join(fails[:12]), flush=True)
    out.write("%s\t%s\t%s\t%s\t%s\n" % (name, ",".join(sorted({e[0] for e in edits})), verdict, last[0], " ".join(tests))); out.flush()
shutil.rmtree(root, ignore_errors=True)
