"""R2 mutation check: each guard (or a pair, for a compensated one) switched off on its own copy of the worktree's
backend; its killing test must fail. Usage: python r2mut.py [names...]; results appended to results.tsv."""
import os, shutil, subprocess, sys
WT = "/home/user/Jarvis/.worktrees/brain-062C/command-center"
S = os.path.dirname(os.path.abspath(__file__))
PY = "/home/user/rebuild/venv/bin/python"
B = "app/brain/"
SQL = "app/migrations/0008_brain_withhold_identity.sql"
I = "tests/integration/"
U = "tests/unit/"
H = I + "test_brain_hold_r2_062.py"
D1 = I + "test_brain_groups_d1_062.py"
MC = I + "test_brain_mutation_closure_c_062.py"
RU = I + "test_brain_rollback_updates_062.py"
TRIG_EXEMPT = ("              AND e.source_row_id = NEW.source_message_id AND NEW.source_message_id <> ''\n"
               "                  AND (e.kind = 'release' OR (e.kind = 'left_readable' AND w.state = 'member'))")
# (name, [(file, old, new), ...], tests)
M = [
 ("hgr:sweep_off", [(B + "review.py", "        held += await hold_group(session, anchor, found[anchor], texts=texts, all_in_hand=all_in_hand, actor=actor)", "        pass")],
  [H + "::test_hgr_a_withhold_holds_every_row_of_its_group_with_its_evidence"]),
 ("hgr:exempt_off", [(B + "groups.py", "        if await identity.exempt_covers(s, anchor, sid, row):", "        if False:")],
  [H + "::test_hgr_a_withhold_holds_every_row_of_its_group_with_its_evidence"]),
 ("hgr:none_all_in_hand_off", [(B + "review.py", "    all_in_hand = (await admin_anchors(session)) <= set(texts)", "    all_in_hand = True")],
  [H + "::test_hgr_none_needs_every_withheld_text_in_hand"]),
 ("hgr:withhold_texts_off", [(B + "review.py", "texts={root: w_content}, ip=ip)", "texts={}, ip=ip)")],
  [H + "::test_hgr_a_withhold_holds_every_row_of_its_group_with_its_evidence"]),
 ("hgr:import_group_holds_off", [(B + "importer.py", "            self._group_holds(ss, msgs, plans, rows)  # option C", "            pass  # option C")],
  [H + "::test_hgr_an_import_of_the_decision_backup_holds_a_group_conversation_it_stores_first"]),
 ("hgr:import_release_any_group", [(B + "importer.py", "                if self._released_here(anchor, ss, m, rows.get(m.key)):\n                    continue\n                member =", "                if any(self._released_here(a, ss, m, rows.get(m.key)) for a in anchors):\n                    break\n                member =")],
  [H + "::test_hgr_a_release_under_one_group_never_frees_a_row_of_two"]),
 ("hgr:settle_group_via_off", [(B + "importer.py", "                if mt.stone.via in brain_groups.GROUP_VIAS:", "                if False:")],
  [D1 + "::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits"]),
 ("hgr:seed_group_vias_off", [(B + "groups.py", "        if st.session and st.certain and st.group in ids.by_key and st.via not in GROUP_VIAS:", "        if st.session and st.certain and st.group in ids.by_key:")],
  [U + "test_brain_hold_r2.py::test_a_kept_hold_held_only_with_its_conversation_never_seeds_a_group"]),
 ("hgr:uid_group_vias_off", [(B + "groups.py", "        if st.uid and st.certain and st.group in ids.by_key and st.via not in GROUP_VIAS:", "        if st.uid and st.certain and st.group in ids.by_key:")],
  [U + "test_brain_hold_r2.py::test_a_kept_hold_held_only_with_its_conversation_never_seeds_a_group"]),
 ("gd1:updated_copies_gone_off", [(B + "review.py", "            \"OR x.target_id::text IN (SELECT id FROM gone))\"),", "            \")\"),")],
  [D1 + "::test_skc_d_the_older_text_of_a_group_row_changed_then_deleted_keeps_no_text_in_any_rollback_copy"]),
 ("m18:hgr_updated_copies_off", [(B + "review.py", "    out[\"updated_copies_blanked\"] = await hold_updated_copies(session, sids, texts=texts)", "    out[\"updated_copies_blanked\"] = 0")],
  [RU + "::test_a_rollback_never_restores_a_pruned_tool_row_that_echoed_the_withheld_text[legacy]"]),
 ("hgr:deleted_copies_off", [(B + "review.py", "    out[\"deleted_copies_blanked\"] = await hold_deleted_copies(session, sids, texts=texts)", "    out[\"deleted_copies_blanked\"] = 0")],
  [D1 + "::test_skc_d_the_older_text_of_a_group_row_changed_then_deleted_keeps_no_text_in_any_rollback_copy"]),
 ("hgr:related_copies_off", [(B + "review.py", "        n += await _hold_related_copies(session, texts, deleted=False)", "        n += 0")],
  [MC + "::test_a_rolled_back_carrier_changed_in_place_comes_back_held_on_its_own"]),
 ("m18:v_f8_rollback_scope", [(B + "importer.py", "                or not flag.get(\"anchor\") or not key or key in ids.by_key):", "                or True):")],
  [MC + "::test_a_rolled_back_carrier_changed_in_place_comes_back_held_on_its_own"]),
 ("hgr:list_unlisted_off", [(B + "groups.py", "    sids = sorted(members)\n    if not sids:\n        return 0", "    sids = sorted(members)\n    if True:\n        return 0")],
  [H + "::test_hgr_the_startup_repair_and_a_rollback_hold_the_group_rows"]),
 ("hgr:rollback_sweep_off", [(B + "importer.py", "    group_holds = await review.hold_group_rows(session, actor=actor, cause=\"rollback\")", "    group_holds = {}")],
  [H + "::test_hgr_the_startup_repair_and_a_rollback_hold_the_group_rows"]),
 ("hgr:decisions_apply_sweep_off", [(B + "importer.py", "            swept = await review.hold_group_rows(s, actor=decider.email, cause=via)", "            swept = {}")],
  [H + "::test_hgr_restore_s_removals_only_file_holds_the_group_rows"]),
 ("hgr:legacy_sweep_off", [(B + "importer.py", "        await review.hold_group_rows(s, actor=actor, cause=\"legacy-reset\")  # option C", "        pass  # option C")],
  [H + "::test_hgr_the_legacy_reset_holds_the_group_rows"]),
 ("m18:trig_release_any_group", [(SQL, "                WHERE e.anchor_key = w.anchor_key AND e.source_session_id = w.source_session_id\n                  AND e.source_row_id = NEW.source_message_id", "                WHERE e.source_session_id = w.source_session_id\n                  AND e.source_row_id = NEW.source_message_id")],
  [MC + "::test_trig_release_any_group_a_release_under_one_group_never_frees_a_row_of_two"]),
 ("m18:trig_release_not_exact", [(SQL, "                  AND brain_exact(e.role, e.tool_call_id, e.source_at, e.source_uid, NEW.role, NEW.tool_call_id,\n                                  NEW.at, brain_row_uid(NEW.source_uid, NEW.source_key)))", "                  )")],
  [MC + "::test_trig_release_not_exact_a_release_never_frees_another_message_at_its_row_id"]),
 ("r2:trig_group_off", [(SQL, "        IF v_anchor IS NOT NULL THEN\n            v_via := 'group';", "        IF false THEN\n            v_via := 'group';")],
  [H + "::test_hgr_the_database_guard_holds_a_group_row_any_code_writes_readable"]),
 ("hgr_verify_uncertain_off", [(SQL, "AND NEW.source_message_id <> ''\n                  AND (e.kind = 'release' OR (e.kind = 'left_readable' AND w.state = 'member'))", "AND NEW.source_message_id <> ''\n                  AND (e.kind IN ('release', 'left_readable'))")],
  [MC + "::test_hgr_verify_uncertain_a_left_readable_choice_never_frees_a_row_an_uncertain_link_holds"]),
 ("r2:r10_off", [(SQL, "     WHERE NOT m.withheld AND m.source_key <> w.anchor_key\n       AND NOT EXISTS (\n           SELECT 1 FROM brain_withhold_exemptions e\n            WHERE e.anchor_key = w.anchor_key AND e.source_session_id = w.source_session_id\n              AND e.source_row_id = m.source_message_id AND m.source_message_id <> ''\n              AND (e.kind = 'release' OR (e.kind = 'left_readable' AND w.state = 'member'))\n              AND brain_exact(e.role, e.tool_call_id, e.source_at, e.source_uid, m.role, m.tool_call_id, m.at,\n                              brain_row_uid(m.source_uid, m.source_key)))\n     ORDER BY m.id, w.anchor_key;", "     WHERE false\n     ORDER BY m.id, w.anchor_key;")],
  [H + "::test_r10_the_repair_holds_a_readable_row_of_a_group_conversation_and_hides_its_title"]),
 ("r2:r10_titles_off", [(SQL, "    SELECT count(*) INTO n_group_titles FROM hid;", "    SELECT 0 INTO n_group_titles;")],
  [H + "::test_r10_the_repair_holds_a_readable_row_of_a_group_conversation_and_hides_its_title"]),
 ("r2:counter_readable_off", [(SQL, "        'group_rows_readable', (SELECT count(DISTINCT m.id) FROM brain_withheld_sessions w", "        'group_rows_readable', (SELECT 0 * count(DISTINCT m.id) FROM brain_withheld_sessions w")],
  [H + "::test_r10_the_repair_holds_a_readable_row_of_a_group_conversation_and_hides_its_title"]),
 ("r2:status_counters_off", [(B + "review.py", "            \"group_rows_held\": int(p.get(\"group_rows_held\") or 0),", "            \"group_rows_held\": 0,")],
  [H + "::test_hgr_decision_status_counts_the_group_rows"]),
 # compensated by R10 in the same repair: shown with the double
 ("r2:repair_sweep_off+r2:r10_off", [(B + "identity.py", "    for k, v in (await hold_group_rows(s, actor=actor, cause=cause)).items():\n        py[k] += v", "    pass"),
   (SQL, "     WHERE NOT m.withheld AND m.source_key <> w.anchor_key\n       AND NOT EXISTS (\n           SELECT 1 FROM brain_withhold_exemptions e\n            WHERE e.anchor_key = w.anchor_key AND e.source_session_id = w.source_session_id\n              AND e.source_row_id = m.source_message_id AND m.source_message_id <> ''\n              AND (e.kind = 'release' OR (e.kind = 'left_readable' AND w.state = 'member'))\n              AND brain_exact(e.role, e.tool_call_id, e.source_at, e.source_uid, m.role, m.tool_call_id, m.at,\n                              brain_row_uid(m.source_uid, m.source_key)))\n     ORDER BY m.id, w.anchor_key;", "     WHERE false\n     ORDER BY m.id, w.anchor_key;")],
  [H + "::test_hgr_the_startup_repair_and_a_rollback_hold_the_group_rows"]),
 ("r2:repair_sweep_off", [(B + "identity.py", "    for k, v in (await hold_group_rows(s, actor=actor, cause=cause)).items():\n        py[k] += v", "    pass")],
  [H + "::test_hgr_the_startup_repair_and_a_rollback_hold_the_group_rows"]),
]
only = sys.argv[1:]
db = "r2m"
env = dict(os.environ, TEST_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s" % db, HERMES_SRC="", REQUIRE_HERMES_SRC="")
out = open(os.path.join(S, "results.tsv"), "a")
for name, edits, tests in M:
    if only and name not in only:
        continue
    root = os.path.join(S, "copy")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    shutil.copytree(os.path.join(WT, "backend"), os.path.join(root, "backend"), ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    shutil.copytree(os.path.join(WT, "migration"), os.path.join(root, "migration"), ignore=shutil.ignore_patterns("__pycache__"))
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
    r = subprocess.run([PY, "-m", "pytest", "-p", "no:randomly", "-q", "-x", *tests], cwd=os.path.join(root, "backend"), env=env, capture_output=True, text=True, timeout=1800)
    last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or " error" in l][-1:] or [r.stdout[-200:]]
    verdict = "CAUGHT" if r.returncode != 0 else "SURVIVED"
    print(name, verdict, last[0], flush=True)
    out.write("%s\t%s\t%s\t%s\t%s\n" % (name, ",".join(sorted({e[0] for e in edits})), verdict, last[0], " ".join(tests))); out.flush()
shutil.rmtree(os.path.join(S, "copy"), ignore_errors=True)
