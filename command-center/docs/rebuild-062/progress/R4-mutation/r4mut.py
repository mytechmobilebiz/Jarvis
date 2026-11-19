"""R4 mutation check: each guard (or a pair, for a compensated one) switched off on its own copy of the worktree's
backend; its killing test must FAIL (an assertion failure, never an error or a collection problem). Usage:
python r4mut.py [names...]; results appended to results.tsv. Names follow the sources' matrix where it names the guard
(rel3:*, m18:oc_*, gd1:one_click_uncertain_off, m18:carried_via) and r4:* for this rebuild's own guards."""
import os, shutil, subprocess, sys
WT = "/home/user/Jarvis/.worktrees/brain-062C/command-center"
S = os.path.dirname(os.path.abspath(__file__))
PY = "/home/user/rebuild/venv/bin/python"
ID, RV, GR, IM = "app/brain/identity.py", "app/brain/review.py", "app/brain/groups.py", "app/brain/importer.py"
IR = "tests/integration/test_brain_releases_r2_062.py"
IL = "tests/integration/test_brain_later_062.py"
UR = "tests/unit/test_brain_releases_r2_062.py"
UM = "tests/unit/test_brain_mutation_closure_c_062.py"
CARRIERS_OFF = (RV, "            if fields and identity.quick_related(fields, quicks[a]):", "            if False:")
FOREIGN_OFF = (GR, "                if rel and not await identity.exempt_covers(s, other, sid, row):", "                if False:")
M = [
 ("rel3:mark_off", [(RV, "        for sha in await identity.mark_later_withhold(session, root):", "        for sha in []:")],
  [IR + "::test_rel3_a_later_withhold_makes_none_stale_at_once_and_the_export_waits"]),
 ("rel3:reopen_off", [(RV, "        for sha in await identity.mark_later_withhold(session, root):\n            await identity.reopen_decision_backup(session, sha)",
                       "        for sha in await identity.mark_later_withhold(session, root):\n            pass")],
  [IR + "::test_rel3_a_later_withhold_makes_none_stale_at_once_and_the_export_waits"]),
 ("rel3:keep_later_off", [(ID, "        out.add(LATER_WITHHOLD)  # keep_later", "        pass")],
  [UR + "::test_keep_later_keeps_the_mark_until_every_text_was_in_hand",
   IR + "::test_rel3_a_settle_without_the_later_text_keeps_the_evidence_stale"]),
 ("rel3:none_every_off", [(ID, "    if NO_RELATION in out and not all_in_hand:", "    if False:")],
  [UR + "::test_none_every_none_needs_every_withheld_text_in_hand"]),
 ("rel3:imp_none_every_off", [(IM, "                relations = identity.settled_relations(\n",
                               "                relations = (lambda new, old, all_in_hand: tuple(identity.relation_order(new or [identity.NO_RELATION]))) (\n")],
  [IR + "::test_rel3_13465_a_later_row_identical_to_a_withheld_text_not_in_hand_is_never_none"]),
 ("rel3:all_in_hand_true", [(IM, "        self.all_in_hand = {st.key for st in self.ids.by_key.values() if st.anchor == \"\" and st.certain} <= set(",
                             "        self.all_in_hand = True or {st.key for st in self.ids.by_key.values() if st.anchor == \"\" and st.certain} <= set(")],
  [IR + "::test_rel3_a_settle_without_the_later_text_keeps_the_evidence_stale"]),
 ("rel3:all_in_hand_false", [(IM, "        self.all_in_hand = {st.key for st in self.ids.by_key.values() if st.anchor == \"\" and st.certain} <= set(",
                              "        self.all_in_hand = False and {st.key for st in self.ids.by_key.values() if st.anchor == \"\" and st.certain} <= set(")],
  [IR + "::test_rel3_settle_with_every_text_in_hand_computes_it_again"]),
 ("rel3:eligible_own_only", [(RV, "    return set(anchors) <= set(compared)", "    return True")],
  [UR + "::test_eligible_now_compares_every_withheld_text_in_hand"]),
 ("m18:oc_role", [(RV, "    if role not in ONE_CLICK_ROLES:\n        return False\n", "")],
  [UM + "::test_oc_role_only_user_and_assistant_rows_are_one_click_eligible"]),
 ("m18:oc_none_in", [(RV, "    return found == [identity.NO_RELATION]", "    return identity.NO_RELATION in found")],
  [UM + "::test_oc_none_in_only_exactly_none_is_one_click_eligible"]),
 ("m18:oc_plain", [(RV, "    if form != identity.PLAIN_FORM:\n        return False", "    if False:\n        return False")],
  [UM + "::test_oc_plain_and_tool_row_only_a_plain_read_form_is_one_click_eligible",
   IL + "::test_the_one_click_release_skips_a_row_whose_form_was_never_read"]),
 ("m18:oc_tool_row", [(ID, "        return SUMMARY_FORM\n", "        return PLAIN_FORM\n"),
                      (ID, "        return WRAPPED_FORM\n", "        return PLAIN_FORM\n"),
                      (ID, "        return ENCODED_FORM\n", "        return PLAIN_FORM\n")],
  [IL + "::test_the_one_click_release_skips_a_hermes_summary_or_wrapped_text_and_releases_the_plain_row"]),
 ("gd1:one_click_uncertain_off", [(RV, "    if uncertain or identity.UNCERTAIN_REL in found:\n        return False  # O-D1", "    if False:\n        return False  # O-D1")],
  [UR + "::test_uncertain_is_never_one_click_eligible_and_never_eligible_now"]),
 ("r4:b6_typed_off", [(RV, "    if not (confirmed or typed or checked):", "    if False:")],
  [IR + "::test_b6_every_untyped_body_gets_400_or_422_and_the_row_stays_held"]),
 ("r4:b6_client_trust", [(RV, "                                 trusted=(typed or checked) if trusted is None else bool(trusted))",
                          "                                 trusted=True)")],
  [IR + "::test_rel3_6725_a_check_of_one_backups_content_marks_only_that_backups_release"]),
 ("rel3:mark_any_backup", [(ID, "            BrainWithholdExemption.decided_on == decided_on[:64],", "            BrainWithholdExemption.decided_on != \"\",")],
  [IR + "::test_rel3_6725_a_check_of_one_backups_content_marks_only_that_backups_release"]),
 ("r4:f1_exempt_trust_off", [(ID, "    return any(b.trusted for b in found)\n\n\nasync def unchecked_covers", "    return bool(found)\n\n\nasync def unchecked_covers")],
  [IR + "::test_skc2_exempt_covers_never_takes_trust_from_another_backups_release",
   IR + "::test_skc2_an_unchecked_release_never_stands_on_another_backups_typed_release"]),
 ("r4:f1_covering_any", [(ID, "    return undecided + [b for b in found if b.decided_on and sha and b.decided_on == sha]", "    return found")],
  [IR + "::test_skc2_exempt_covers_never_takes_trust_from_another_backups_release"]),
 ("r4:f1_released_trust_off", [(ID, "        return any(b.covers(row) and b.trusted and (backup is None", "        return any(b.covers(row) and (backup is None")],
  [UR + "::test_identities_released_takes_trust_from_the_covering_release_only"]),
 ("r4:file_trust_off", [(IM, "                    if not _release_trusted(r):\n                        continue  # rebuild R4", "                    if False:\n                        continue  # rebuild R4")],
  [IR + "::test_rel3_an_unchecked_file_release_frees_no_hold_and_a_trusted_one_does"]),
 ("r4:file_mark_off", [(IM, "                    for sha in await identity.mark_later_withhold(s, st.key):", "                    for sha in []:")],
  [IR + "::test_rel3_a_decisions_file_bringing_a_withhold_this_database_lacks_makes_none_stale"]),
 ("r4:w_text_same_uid", [(IM, '        same_text = ("key", "same JARVIS row", "duplicate",', '        same_text = ("key", "same JARVIS row", "same message id", "duplicate",'),
                         (IM, '                                and x.via != "same message id"\n', "")],
  [IR + "::test_seeds_r3_a_same_message_id_member_is_never_taken_as_the_withheld_text"]),
 ("m18:carried_via", [(IM, "    elif review == identity.KEPT and text(\"via\") in identity.HOLD_VIA:", "    elif False:")],
  [IR + "::test_a_kept_hold_keeps_the_rule_that_held_it_through_a_restore"]),
 ("r4:form_sweep_off", [(ID, "                 form=row_form(row))", "                 form=PLAIN_FORM)")],
  [IL + "::test_the_one_click_release_skips_a_hermes_summary_or_wrapped_text_and_releases_the_plain_row"]),
 ("r4:file_trust_off+f1_released_trust_off", [
     (IM, "                    if not _release_trusted(r):\n                        continue  # rebuild R4", "                    if False:\n                        continue  # rebuild R4"),
     (ID, "        return any(b.covers(row) and b.trusted and (backup is None", "        return any(b.covers(row) and (backup is None")],
  [IR + "::test_rel3_an_unchecked_file_release_frees_no_hold_and_a_trusted_one_does"]),
 ("rel3:hgr_foreign_off", [FOREIGN_OFF], [IR + "::test_skC_a_row_released_for_w1_that_carries_w2_is_held_when_w2_is_withheld"]),
 ("rel3:hgr_foreign_off+hc_carriers_off", [FOREIGN_OFF, CARRIERS_OFF],
  [IR + "::test_skC_a_row_released_for_w1_that_carries_w2_is_held_when_w2_is_withheld"]),
]
only = sys.argv[1:]
db = "r4m"
env = dict(os.environ, TEST_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s" % db,
           TEST_CREDIT_REPAIR_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s_cr" % db,
           HERMES_SRC="", REQUIRE_HERMES_SRC="")
out = open(os.path.join(S, "results.tsv"), "a")
for name, edits, tests in M:
    if only and name not in only:
        continue
    root = "/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/r4/mutcopy"
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
    r = subprocess.run([PY, "-m", "pytest", "-p", "no:randomly", "-q", "-rA", *tests], cwd=os.path.join(root, "backend"),
                       env=env, capture_output=True, text=True, timeout=1800)
    last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or " error" in l][-1:] or [r.stdout[-200:]]
    verdict = "CAUGHT" if (r.returncode == 1 and " failed" in last[0] and " error" not in last[0]) else (
        "SURVIVED" if r.returncode == 0 else "ERROR")
    print(name, verdict, last[0], flush=True)
    out.write("%s\t%s\t%s\t%s\t%s\n" % (name, ",".join(sorted({e[0] for e in edits})), verdict, last[0], " ".join(tests))); out.flush()
if os.environ.get("KEEP") is None:
    shutil.rmtree("/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/r4/mutcopy", ignore_errors=True)
