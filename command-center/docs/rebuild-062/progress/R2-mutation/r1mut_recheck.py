"""R1 mutation check: each guard switched off on its own copy of the worktree's backend; its killing test must fail."""
import os, shutil, subprocess, sys
WT = "/home/user/Jarvis/.worktrees/brain-062C/command-center"
S = os.path.dirname(os.path.abspath(__file__)); OUT = "r1-recheck.tsv"
PY = "/home/user/rebuild/venv/bin/python"
B = "app/brain/"
SQL = "app/migrations/0008_brain_withhold_identity.sql"
I = "tests/integration/"
U = "tests/unit/"
D1 = I + "test_brain_groups_d1_062.py"
MC = I + "test_brain_mutation_closure_c_062.py"
M = [
 ("m18:title_trig_groups", SQL, "AND (EXISTS (SELECT 1 FROM brain_withheld_sessions w WHERE w.source_session_id = NEW.source_session_id)", "AND (false", [MC + "::test_title_trigger_hides_the_title_of_a_group_conversation_by_membership_alone"]),
 ("m18:r9_off", SQL, "          JOIN brain_withheld_sessions w ON w.source_session_id = s.source_session_id\n    ), gone AS", "          JOIN brain_withheld_sessions w ON false\n    ), gone AS", [MC + "::test_r9_the_repair_blanks_a_rollback_copy_of_a_group_row_written_without_the_sweep"]),
 ("gd1:r9_copy_session_off", SQL, "                OR (x.before -> 'deleted_row' ->> 'session_id') IN (SELECT id::text FROM g)\n                OR (x.target_table = 'archive_messages' AND x.target_id::text IN (SELECT id FROM gone)))", "                )", [D1 + "::test_p2_the_startup_repair_blanks_a_copy_of_a_changed_then_deleted_group_row"]),
 ("m18:seed_pending_too", B + "groups.py", "        if st.session and st.certain and st.group in ids.by_key:\n            out[st.group].add(st.session[:200])", "        if st.session and st.group in ids.by_key:\n            out[st.group].add(st.session[:200])", [MC + "::test_a_pending_hold_never_seeds_a_group", U + "test_brain_groups.py::test_a_pending_hold_never_seeds_a_group"]),
 ("m18:j8_always_two", B + "groups.py", 'self.add_edge(other, sid, "provenance", uncertain=not count, copied=True)', 'self.add_edge(other, sid, "provenance", uncertain=count < J8_MIN_ROWS, copied=True)', [I + "test_brain_mutation_closure_r2_062.py::test_an_orphaned_branch_with_one_copied_row_joins_its_group_by_provenance"]),
 ("m18:j8_reset_copies", B + "groups.py", 'if n.lineage == "reset":  # a reset copies no rows', 'if False:  # a reset copies no rows', [MC + "::test_a_reset_with_copied_looking_rows_never_joins_by_copied_history"]),
 ("gd1:child_both_ways", B + "groups.py", 'BOTH_WAYS = frozenset({"continuation",', 'BOTH_WAYS = frozenset({"child", "continuation",', [D1 + "::test_d1_a_markerless_child_joins_from_the_parent_down_only"]),
 ("gd1:child_edge_off", B + "groups.py", "        if not parent or not child or parent == child:\n            return", "        if not parent or not child or parent == child or how == \"child\":\n            return", [D1 + "::test_d1_a_markerless_child_joins_from_the_parent_down_only"]),
 ("gd1:uncertain_off", B + "groups.py", "if other in out or e.uncertain or self.conflicting(other) or self._joined_past_its_parent(other, e):", "if other in out:", [D1 + "::test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins", D1 + "::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits"]),
 ("gd1:uncertain_no_spread", B + "groups.py", "            for other, e in self._steps(g, extra):\n                if other not in out:", "            for other, e in ():\n                if other not in out:", [D1 + "::test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins"]),
 ("gd1:link_parents_off", B + "groups.py", "        if how in PARENT_LINKS:\n            g.node(b).claims.add(a[:200])", "        if False:\n            g.node(b).claims.add(a[:200])", [D1 + "::test_uncertain_hermes_relinked_a_child_to_another_known_conversation"]),
 ("gd1:marker_links_off", B + "importer.py", "if ss.markers.get(marker) and marker not in ss.copied_markers:", "if False:", [D1 + "::test_a_delegated_sub_agent_and_a_reset_join_and_their_links_are_recorded"]),
 ("sk3_graph_marker_edges_off", B + "groups.py", "                if value and value[:200] != n.parent:\n                    g.add_edge(value, n.sid, MARKER_HOW[marker])", "                if False:\n                    g.add_edge(value, n.sid, MARKER_HOW[marker])", [D1 + "::test_a_marker_naming_a_withheld_conversation_joins_in_the_import_that_first_sees_it"]),
 ("sk3_archive_parent_edge_off", B + "groups.py", "        if n.parent:\n            n.claims.add(n.parent)\n            g.add_edge(n.parent, n.sid, LINEAGE_HOW.get(n.lineage, \"child\"))\n    for a, b, how", "        if n.parent:\n            n.claims.add(n.parent)\n    for a, b, how", [D1 + "::test_the_archive_lineage_links_a_child_whose_link_was_never_recorded"]),
 ("sk3_src_orphan_any_marker_off", B + "source.py", "counted = [k for k in FORK_MARKERS if markers.get(k) and (not parent or markers[k] == parent)]", "counted = [k for k in FORK_MARKERS if markers.get(k) and markers[k] == parent]", [U + "test_brain_source_markers.py", U + "test_brain_source.py"]),
 ("ga_src_fork_binding_off", B + "source.py", "counted = [k for k in FORK_MARKERS if markers.get(k) and (not parent or markers[k] == parent)]", "counted = [k for k in FORK_MARKERS if markers.get(k)]", [U + "test_brain_source_markers.py::test_a_fork_marker_counts_only_when_it_names_the_sessions_own_parent[inherited_branch]"]),
 ("ga_src_copied_fallback_off", B + "source.py", "    if unbound and not foreign:\n        return \"continuation\", parent  # copied", "    if False:\n        return \"continuation\", parent  # copied", [U + "test_brain_source_markers.py::test_a_fork_marker_counts_only_when_it_names_the_sessions_own_parent[copied_after_reopen]"]),
 ("gd1:release_unrelated_uncertain_off", B + "review.py", "    if await brain_groups.in_uncertain(session, sess.id):\n        # O-D1", "    if False:\n        # O-D1", [D1 + "::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits"]),
 ("gd1:holds_display_off", B + "review.py", '"uncertain": sess.source_session_id in uncertain,', '"uncertain": False,', [D1 + "::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits"]),
 ("gd1:confirm_uncertain_off", B + "identity.py", "CONFIRM_RELATIONS = (IDENTICAL_REL, RESTATED_REL, CONTAINS, PART_OF, UNCERTAIN_REL)", "CONFIRM_RELATIONS = (IDENTICAL_REL, RESTATED_REL, CONTAINS, PART_OF)", [D1 + "::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits"]),
 ("gd1:export_gate_off", B + "importer.py", "    if uncertain:\n        # option C (O-D1)", "    if False:\n        # option C (O-D1)", [D1 + "::test_uncertain_a_parent_that_does_not_resolve_is_held_never_one_click_and_the_export_waits"]),
 ("gd1:definite_off", B + "review.py", "sorted(sid for sid, m in pre.items() if not m.uncertain)", "sorted(pre)", [D1 + "::test_uncertain_members_never_move_the_decision_to_another_backup"]),
 ("gd1:hold_uncertain_off", B + "groups.py", "        if row.id in done or row.withheld:\n            continue", "        if True:\n            continue", [D1 + "::test_uncertain_conflicting_parents_one_unknown_is_held_one_in_the_group_joins"]),
 ("r1:orphan_delegate_anchors", B + "groups.py", 'return not n.parent or n.parent not in self.known or (n.orphan and n.lineage == "delegated")', "return not n.parent or n.parent not in self.known", [I + "test_brain_groups_inherited_062.py::test_skc3_n1_an_orphaned_rotation_with_an_inherited_delegate_marker_is_one_conversation_with_its_parent"]),
 ("r1:family_links_filter_off", B + "later.py", "    for a, b in (await s.execute(select(BrainSessionLink.a, BrainSessionLink.b).where(\n            BrainSessionLink.how.in_(FAMILY_LINKS)))).all():", "    for a, b in (await s.execute(select(BrainSessionLink.a, BrainSessionLink.b))).all():", [D1 + "::test_a_withhold_in_a_delegate_never_makes_its_parent_a_withheld_family"]),
 ("r1:withhold_records_group_off", B + "review.py", "        await brain_groups.refresh(session, anchors=[root])\n", "", [D1 + "::test_d1_a_markerless_child_joins_from_the_parent_down_only"]),
 ("r1:import_records_group_off", B + "importer.py", "        self.groups = await brain_groups.refresh(self.s, backup=self.src.backup, run_id=self.run_id, ids=self.ids,\n                                                 write=self.write)", "        self.groups = {}", [D1 + "::test_a_marker_naming_a_withheld_conversation_joins_in_the_import_that_first_sees_it"]),
 ("r1:file_groups_off", B + "importer.py", "        out[\"groups\"] += await brain_groups.record(", "        out[\"groups\"] += 0 and await brain_groups.record(", [D1 + "::test_d1_child_and_uncertain_members_survive_a_restore_and_the_reimport"]),
 ("r1:keep_trigger_uncertain_off", SQL, "        NEW.state := 'uncertain';  -- fail closed", "        NULL;  -- fail closed", [MC + "::test_a_recorded_group_is_never_forgotten_and_an_uncertain_member_never_becomes_a_member"]),
 ("r1:settle_groups_off", B + "importer.py", " or ss.id[:200] in grouped.get(g, {})", "", [D1 + "::test_settle_finishes_a_decision_over_the_whole_group"]),
 ("r1:legacy_groups_off", B + "importer.py", "        await brain_groups.refresh(s)\n        await brain_groups.hold_uncertain(s, actor=actor, cause=\"legacy-reset\")", "        pass", [D1 + "::test_the_legacy_reset_records_the_groups_of_the_withholds_it_binds"]),
 ("r1:repair_groups_off", B + "identity.py", "    await groups.refresh(s, run_id=None)\n", "", [MC + "::test_the_startup_repair_records_the_groups_again"]),
]
only = sys.argv[1:]
db = "r2m"
env = dict(os.environ, TEST_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s" % db, TEST_CREDIT_REPAIR_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s_cr" % db, HERMES_SRC="", REQUIRE_HERMES_SRC="")
out = open(os.path.join(S, OUT), "a")
for name, f, old, new, tests in M:
    if only and name not in only:
        continue
    if not tests:
        print(name, "NO TEST (listed)", flush=True)
        out.write("%s\t%s\tNO TEST\n" % (name, f)); out.flush()
        continue
    root = os.path.join(S, "copy")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    shutil.copytree(os.path.join(WT, "backend"), os.path.join(root, "backend"), ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    shutil.copytree(os.path.join(WT, "migration"), os.path.join(root, "migration"), ignore=shutil.ignore_patterns("__pycache__"))
    path = os.path.join(root, "backend", f)
    text = open(path).read()
    n = text.count(old)
    if n != 1:
        print(name, "INVALID (%d matches)" % n, flush=True)
        out.write("%s\t%s\tINVALID %d\n" % (name, f, n)); out.flush()
        continue
    open(path, "w").write(text.replace(old, new))
    r = subprocess.run([PY, "-m", "pytest", "-p", "no:randomly", "-q", "-x", *tests], cwd=os.path.join(root, "backend"), env=env, capture_output=True, text=True, timeout=1800)
    last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or " error" in l][-1:] or [r.stdout[-200:]]
    verdict = "CAUGHT" if r.returncode != 0 else "SURVIVED"
    print(name, verdict, last[0], flush=True)
    out.write("%s\t%s\t%s\t%s\t%s\n" % (name, f, verdict, last[0], " ".join(tests))); out.flush()
shutil.rmtree(os.path.join(S, "copy"), ignore_errors=True)
