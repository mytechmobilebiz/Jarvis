"""R5 mutation check of the oracle's guards and narrowings: each mutant (one guard loosened, or switched off) on its own
copy of the worktree's backend; its killing self-test must FAIL with an assertion (never an error or a collection
problem). Usage: python r5mut.py [names...]; results appended to results.tsv. Names follow the sources' matrix where it
names the guard (cr2:oracle_*, rel3:oracle_*, c3:*), r5:* for this rebuild's own."""
import os, shutil, subprocess, sys
WT = "/home/user/Jarvis/.worktrees/brain-062C/command-center"
S = os.path.dirname(os.path.abspath(__file__))
PY = "/home/user/rebuild/venv/bin/python"
OR = "tests/property/oracle.py"
P = "tests/property/"
CA, RE, DE, SE, SP, RC, GC = (P + f for f in ("test_oracle_carriers_r2.py", "test_oracle_releases_r2.py",
                                               "test_oracle_derived3.py", "test_oracle_seeds_r3.py",
                                               "test_oracle_spans_c3.py", "test_oracle_released_c3.py",
                                               "test_oracle_groups_c.py"))
F3 = P + "test_oracle_family_s3.py"
F4 = P + "test_oracle_family_part_of_s4.py"
F5 = P + "test_oracle_fold_piece_s4.py"
M = [
 ("cr2:oracle_fold_off", [(OR, '    return unicodedata.normalize("NFKC", t).translate(_IGN)\n', '    return text or ""\n')],
  [CA + "::test_oci_the_oracle_demands_a_carrier_in_another_case_or_form"]),
 ("cr2:oracle_generic_off", [(OR, "    f = fflat(piece)\n    if not f or (not whole and len(f) < MIN_REL):",
                              "    return False\n    f = fflat(piece)\n    if not f or (not whole and len(f) < MIN_REL):")],
  [CA + "::test_od4_the_oracle_neither_demands_nor_allows_a_generic_piece_and_demands_anything_more"]),
 ("cr2:oracle_prune_off", [(OR, "    for m in _PRUNE.finditer(text):  # the generic prune line", "    for m in []:  # the generic prune line")],
  [CA + "::test_p1_the_oracle_demands_ws_first_40_characters_in_the_generic_prune_line"]),
 ("r5:oracle_fqw_off", [(OR, "    if _QUOTE_LINE.search(text):  # fqw", "    if False:  # fqw")],
  [CA + "::test_fwords_a_short_withheld_text_is_identical_as_a_quoted_block_without_its_markers"]),
 ("c3:oracle_recursion_open", [(OR, "DECODE_CAP = True", "DECODE_CAP = False")],
  [CA + "::test_decode_cap_is_the_products_and_fails_closed_the_same_way"]),
 ("rel3:oracle_nb_off", [(OR, "NB_CHECK = True", "NB_CHECK = False")],
  [RE + "::test_nb_a_released_row_of_a_withheld_conversation_carrying_another_withheld_text_is_a_leak"]),
 ("rel3:oracle_na_own_only", [(OR, "NA_EVERY_TEXT = True", "NA_EVERY_TEXT = False")],
  [RE + "::test_na_none_must_hold_against_every_withheld_text_in_hand"]),
 ("rel3:oracle_later_unread", [(OR, "LATER_UNDECIDED = True", "LATER_UNDECIDED = False")],
  [RE + "::test_later_withhold_is_an_undecided_state_for_the_oracle"]),
 ("r5:o2_released_any", [(OR, '    texts = run.released.get(r["source_key"]) or run.released.get("id:%s" % r["id"])\n    if not texts:\n        return False',
                          '    return True')],
  [RE + "::test_o2_a_readable_group_row_never_released_or_released_with_other_text_is_reported"]),
 ("r5:o6_one_click_off", [(OR, '        if why:\n            run.bad("O6"', '        if False:\n            run.bad("O6"')],
  [RE + "::test_o6_one_click_only_for_exactly_none_user_or_assistant_plain_and_certain"]),
 ("c3:oracle_cc_redacted_any", [(OR, "    if not _CC_MARK.search(s):\n        return False\n",
                                 "    if _CC_MARK.search(s):\n        return True\n")],
  [RC + "::test_cc_redacted_of_refuses_other_text_beside_a_mark"]),
 ("c3:oracle_spans_admit_any", [(OR, "    return dem, alw | dem\n", "    return dem, alw | dem | set(TEXT_RELATIONS)\n")],
  [SP + "::test_a_relation_no_reading_gives_is_still_a_violation"]),
 ("c3:oracle_spans_admission_off", [(OR, "        if r.span and not allowed:", "        if r.span:")],
  [SP + "::test_a_span_relation_shown_is_honest"]),
 ("r5:kept_carrier_any_row", [(OR, '    return any(run.leaks([v]) for v in run.sim.versions_of(row["sid"], int(rid)))', "    return True")],
  [DE + "::test_kept_carrier_never_excuses_a_pending_hold_another_reason_no_hold_or_a_row_that_never_carried_w"]),
 ("r5:kept_carrier_pending_too", [(OR, '    if not (stone and stone.get("via") == CARRIER_VIA and stone.get("review") == "kept"):',
                                   '    if not (stone and stone.get("via") == CARRIER_VIA):')],
  [DE + "::test_kept_carrier_never_excuses_a_pending_hold_another_reason_no_hold_or_a_row_that_never_carried_w"]),
 ("r5:carrier_hold_any", [(OR, "    for v in versions_for(run.sim, row):\n        for w in run.withholds:\n            if relations_c(version_fields(v), w.text, group=False, carrier=True)[1]:",
                           "    return True\n    for v in versions_for(run.sim, row):\n        for w in run.withholds:\n            if relations_c(version_fields(v), w.text, group=False, carrier=True)[1]:")],
  [DE + "::test_carrier_hold_excuses_only_a_row_an_admitted_reading_relates"]),
 ("r5:o3_carriers_off", [(OR, "            if rel and in_hand:\n                hit.append((w, rel))", "            if False:\n                hit.append((w, rel))")],
  [DE + "::test_o3_a_readable_carrier_outside_every_group_is_reported"]),
 ("c3:oracle_rekeyed_any", [(OR, '        if v is not None and v.uid and rb["source_key"] == "jarvis:message:uid:" + v.uid:\n            return rb',
                             "        return rb")],
  [SE + "::test_rekeyed_another_uid_another_row_or_no_backup_is_still_disappeared"]),
 ("gd1:oracle_child_edge_off", [(OR, "            if parent in out:\n                out.add(child)", "            if False:\n                out.add(child)")],
  [GC + "::test_o1_down_only_kinds_join_from_the_parent_down_only", GC + "::test_o1_a_group_conversation_not_recorded_is_reported"]),
 ("gd1:oracle_child_both_ways", [(OR, "            elif child in out and kind not in DOWN_ONLY:", "            elif child in out:")],
  [GC + "::test_o1_down_only_kinds_join_from_the_parent_down_only"]),
 ("r5:o1_recorded_off", [(OR, '        if sid not in gs["members"]:\n            run.bad("O1"', '        if False:\n            run.bad("O1"')],
  [GC + "::test_o1_a_group_conversation_not_recorded_is_reported"]),
 ("r5:o11_group_title_off", [(OR, '        if sid in group:\n            run.bad("O11"', '        if False:\n            run.bad("O11"')],
  [GC + "::test_o11_a_group_conversations_title_is_hidden"]),
 # rebuild S3 (D-45): the carrier-only narrowing of B1 and B5
 ("s3:carrier_only_kept_too", [(OR, '    return stone.get("via") == CARRIER_VIA and stone.get("review") == "pending"',
                                '    return stone.get("via") == CARRIER_VIA')],
  [F3 + "::test_s3_b5_still_reports_a_withheld_conversations_new_row_a_linked_or_kept_carrier_and_a_leak"]),
 ("s3:carrier_only_group_too", [(OR, "    return set(fam) - i22 - set(group)", "    return set(fam) - i22")],
  [F3 + "::test_s3_a_conversation_only_a_pending_carrier_hold_binds_is_carrier_only"]),
 ("s3:carrier_only_links_off", [(OR, "    i22 = family(run, current, stored, [s for s in stones if not per_row_carrier(s)])",
                                 "    i22 = {w.sid for w in run.withholds}")],
  [F3 + "::test_s3_b5_still_reports_a_withheld_conversations_new_row_a_linked_or_kept_carrier_and_a_leak"]),
 ("s3:b1_carrier_withheld_too", [(OR, '        if (ra["sid"] in carrier_only and ra["sid"] not in group and not ra.get("withheld")',
                                  '        if (ra["sid"] in carrier_only and ra["sid"] not in group')],
  [F3 + "::test_s3_b1_still_reports_a_family_row_gone_a_held_carrier_readable_and_a_carrier_conversation_leak"]),
 ("s3:b1_carrier_leak_off", [(OR, "                    and run.leaks(versions_for(run.sim, rb)):", "                    and False:")],
  [F3 + "::test_s3_b1_still_reports_a_family_row_gone_a_held_carrier_readable_and_a_carrier_conversation_leak"]),
 ("s3:b5_carrier_leak_off", [(OR, '            if norm(r.get("content")) and run.leaks(versions_for(run.sim, r)):',
                              "            if False:")],
  [F3 + "::test_s3_b5_still_reports_a_withheld_conversations_new_row_a_linked_or_kept_carrier_and_a_leak"]),
 ("s4:family_part_of_carrier_too", [(OR, '    return row.get("sid") in comps.get(w.key, set()) and (stone.get("via") or "") != CARRIER_VIA',
                                    '    return row.get("sid") in comps.get(w.key, set())')],
  [F4 + "::test_s4_family_row_part_of_on_a_carrier_hold_is_still_a_violation"]),
 ("s4:family_part_of_any_row", [(OR, '    return row.get("sid") in comps.get(w.key, set()) and (stone.get("via") or "") != CARRIER_VIA',
                                '    return (stone.get("via") or "") != CARRIER_VIA')],
  [F4 + "::test_s4_part_of_outside_the_family_is_still_a_violation"]),
 ("s4:family_part_of_any_relation", [(OR, '                        allowed |= relations_c(version_fields(v), view, group=True)[1] & {"part_of"}',
                                     '                        allowed |= {"part_of"}')],
  [F4 + "::test_s4_part_of_no_value_gives_is_still_a_violation"]),
 ("s4:fold_piece_any_w", [(OR, "    if wt is None or not w.absorbed:\n        return False", "    if wt is None:\n        return False"),
                          (OR, "            return bool(set(w.absorbed) - set(v.absorbed) - {w.uid})", "            return True")],
  [F5 + "::test_s4_not_in_backup_for_a_whole_withheld_text_is_still_a_violation"]),
 ("s4:fold_piece_nib_always", [(OR, '                        and ("not_in_backup" not in rep or w_piece(w, snap, wt))', "")],
  [F5 + "::test_s4_not_in_backup_for_a_whole_withheld_text_is_still_a_violation"]),
]
only = sys.argv[1:]
db = "r5m"
env = dict(os.environ, TEST_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s" % db,
           TEST_CREDIT_REPAIR_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s_cr" % db)
out = open(os.path.join(S, "results.tsv"), "a")
root = "/tmp/claude-0/-home-user-Jarvis/1ab9c1cb-df4c-59f4-ae56-b5ac930a43fd/scratchpad/r5/mutcopy"
for name, edits, tests in M:
    if only and name not in only:
        continue
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
    r = subprocess.run([PY, "-m", "pytest", "-p", "no:randomly", "-p", "no:cacheprovider", "-q", "-rA", *tests],
                       cwd=os.path.join(root, "backend"), env=env, capture_output=True, text=True, timeout=1800)
    last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or " error" in l][-1:] or [r.stdout[-200:]]
    verdict = "CAUGHT" if (r.returncode == 1 and " failed" in last[0] and " error" not in last[0]
                           and "AssertionError" in r.stdout) else ("SURVIVED" if r.returncode == 0 else "ERROR")
    print(name, verdict, last[0], flush=True)
    out.write("%s\t%s\t%s\t%s\t%s\n" % (name, ",".join(sorted({e[0] for e in edits})), verdict, last[0], " ".join(tests))); out.flush()
shutil.rmtree(root, ignore_errors=True)
