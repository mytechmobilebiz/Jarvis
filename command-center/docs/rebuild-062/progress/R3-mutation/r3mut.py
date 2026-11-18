"""R3 mutation check: each guard (or a pair, for a compensated one) switched off on its own copy of the worktree's
backend; its killing test must FAIL (an assertion failure, never an error or a collection problem). Usage:
python r3mut.py [names...]; results appended to results.tsv. Names follow the sources' matrix (cr2:*, m18:dec_*, c3:*,
the round-3 skeptic's M1 mutants) and r3:* for this rebuild's own guards."""
import os, shutil, subprocess, sys
WT = "/home/user/Jarvis/.worktrees/brain-062C/command-center"
S = os.path.dirname(os.path.abspath(__file__))
PY = "/home/user/rebuild/venv/bin/python"
ID = "app/brain/identity.py"
RV = "app/brain/review.py"
I = "tests/integration/"
U = "tests/unit/"
UC = U + "test_brain_carriers_r2_062.py"
UD = U + "test_brain_derived3_062.py"
UM = U + "test_brain_mutation_closure_c_062.py"
U3 = U + "test_brain_mutation_closure_c3_062.py"
IC = I + "test_brain_carriers_r2_062.py"
ID3 = I + "test_brain_derived3_062.py"
LONGEST64 = ("    texts = {a: w for a, w in (texts or {}).items() if a and w and identity.wform(w) is not None}\n"
             "    out = {\"carrier_rows_held\": 0,")
M = [
 # --- O-CI
 ("cr2:ci_casefold_off", [(ID, '    t = t.casefold().replace("i\\u0307", "i")', '    t = t.replace("i\\u0307", "i")')],
  [UC + "::test_oci_every_relation_ignores_case_and_normalisation"]),
 ("cr2:ci_turkish_off", [(ID, '    t = unicodedata.normalize("NFKC", t).replace("\\u0130", "i").replace("\\u0131", "i")', '    t = unicodedata.normalize("NFKC", t)'),
                         (ID, '    t = t.casefold().replace("i\\u0307", "i")', '    t = t.casefold()')],
  [UC + "::test_oci_every_relation_ignores_case_and_normalisation"]),
 ("cr2:ci_nfkc_off", [(ID, '    t = unicodedata.normalize("NFKC", t).replace("\\u0130", "i").replace("\\u0131", "i")', '    t = t.replace("\\u0130", "i").replace("\\u0131", "i")'),
                      (ID, '    return unicodedata.normalize("NFKC", t).translate(_IGNORE_TABLE)', '    return t.translate(_IGNORE_TABLE)')],
  [UC + "::test_oci_every_relation_ignores_case_and_normalisation"]),
 ("cr2:ci_wordseq_off", [(ID, "    if family and wf.short_words and _word_seq(fwords(r.text), wf.words):", "    if False:")],
  [UC + "::test_oci_a_short_withheld_text_in_another_case_is_ambiguous_not_none"]),
 ("r3:ignorable_off", [(ID, "    t = text.translate(_IGNORE_TABLE)", "    t = text"),
                       (ID, '    return unicodedata.normalize("NFKC", t).translate(_IGNORE_TABLE)', '    return unicodedata.normalize("NFKC", t)')],
  [UD + "::test_n5_fold_removes_the_default_ignorable_code_points", UD + "::test_n5_invisible_formatting_never_hides_w"]),
 # --- O-D4
 ("cr2:generic_off", [(ID, "    return generic_phrase(f)\n", "    return False\n")],
  [UC + "::test_skc_a_common_url_value_in_an_unrelated_search_result_is_no_carrier"]),
 ("cr2:generic_overbroad", [(ID, "    return generic_phrase(f)\n", "    return True\n")],
  [UC + "::test_od4_greeting_and_url_cases_hold_nothing_and_anything_more_is_held"]),
 # --- P1 prune line
 ("cr2:prune_off", [(ID, "        for m in _PRUNE_LINE.finditer(text):", "        for m in ():")],
  [UC + "::test_p1_hermes_generic_prune_line_keeping_ws_first_40_characters_is_a_cut"]),
 # --- P2 full scan: the 64-longest-words prefilter put back (rows, titles, items)
 ("cr2:scan_longest64", [(RV, "            found = carries(identity.row_fields({\"content\": content, \"tool_calls\": calls}))",
   "            found = carries(identity.row_fields({\"content\": content, \"tool_calls\": calls})) if any(\n"
   "                x in (content or \"\") for t in texts.values() for x in sorted(t.split(), key=len)[-64:]) else []")],
  [IC + "::test_skc_a_native_partial_reply_quote_of_a_long_w_is_held_wherever_it_is_found"]),
 ("cr2:titles_longest64", [(RV, "            if identity.carrier_relations([title, sess.title or \"\"], w) or (",
   "            if not any(x in (sess.title or \"\") for x in sorted(w.split(), key=len)[-64:] if len(x) > 4):\n                continue\n            if identity.carrier_relations([title, sess.title or \"\"], w) or (")],
  [IC + "::test_p2_a_title_and_an_imported_memory_item_of_short_words_are_carriers_too"]),
 ("cr2:items_longest64", [(RV, "        if carries([x for x in (item.title or \"\", item.body or \"\") if x]):",
   "        if any(x in (item.body or \"\") for t in texts.values() for x in sorted(t.split(), key=len)[-64:] if len(x) > 4) and carries([x for x in (item.title or \"\", item.body or \"\") if x]):")],
  [IC + "::test_p2_a_title_and_an_imported_memory_item_of_short_words_are_carriers_too"]),
 # --- the quick filter
 ("cr2:quick_forms_one", [(ID, "    forms = {withheld, peel(withheld)[0]}", "    forms = {withheld}")],
  [UC + "::test_p2_the_quick_filter_compares_every_form_of_the_withheld_text"]),
 ("cr2:quick_keep_gt", [(ID, "    return fold(text).translate(_skel_table())", "    return \"\".join(c for c in fold(text).translate(_skel_table()) if c <= \"\\u02ff\") + \"\".join(c for c in fold(text) if c.isspace())")],
  [UC + "::test_p2_the_skeleton_drops_every_whitespace_and_combining_character"]),
 ("cr2:quick_keep_marks", [(ID, "    return fold(text).translate(_skel_table())", "    return \"\".join(c for c in fold(text) if not c.isspace())")],
  [UC + "::test_p2_the_skeleton_drops_every_whitespace_and_combining_character"]),
 ("cr2:quick_q8", [(ID, "QUICK_Q = 8\n", "QUICK_Q = 16\n")],
  [UC + "::test_p2_the_quick_filter_keeps_a_cut_of_sixteen_characters_with_fewer_than_sixteen_of_skeleton"]),
 ("cr2:quick_no_decode", [(ID, "        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or SNAPSHOT_LEAD.search(f) or _QUOTE_SHAPE.search(f):",
   "        if len(f) > CANON_MAX_TEXT or SNAPSHOT_LEAD.search(f) or _QUOTE_SHAPE.search(f):")],
  [UC + "::test_p2_a_row_only_its_decode_reads_is_never_skipped"]),
 ("cr2:quick_no_snapshot", [(ID, "        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or SNAPSHOT_LEAD.search(f) or _QUOTE_SHAPE.search(f):",
   "        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or _QUOTE_SHAPE.search(f):")],
  [UC + "::test_p2_a_snapshot_of_earlier_turns_is_never_skipped_by_the_quick_filter"]),
 ("cr2:quick_no_quote_bypass", [(ID, "        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or SNAPSHOT_LEAD.search(f) or _QUOTE_SHAPE.search(f):",
   "        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or SNAPSHOT_LEAD.search(f):")],
  [UC + "::test_p2_a_reply_quote_of_sixteen_characters_as_written_is_never_skipped_by_the_quick_filter"]),
 # --- the withhold preview in another case
 ("cr2:preview_narrow", [(ID, "        fields = row_fields(r)\n        if not quick_related(fields, quick):\n            continue\n        rel = text_relations(fields, message.content)",
   "        fields = row_fields(r)\n        if max(flat(message.content).split(), key=len) not in (r.content or \"\"):\n            continue\n        rel = text_relations(fields, message.content)")],
  [IC + "::test_oci_the_withhold_preview_reads_another_case"]),
 ("cr2:preview_items_case", [(ID, "        if (len(needle) >= MIN_CONTAINED and fflat(needle) in fflat(body)) or (\n                quick_related(fields, quick) and carrier_relations(fields, message.content)):",
   "        if len(needle) >= MIN_CONTAINED and needle in norm(body):")],
  [IC + "::test_oci_the_withhold_preview_reads_another_case"]),
 ("cr2:preview_title_case", [(ID, "        if carrier_relations([title_base(title)], message.content) or (\n                title_relations(title, message.content) - {NOT_IN_BACKUP}):",
   "        if flat(peel(message.content)[0])[:16] in (title or \"\"):")],
  [IC + "::test_oci_the_withhold_preview_reads_another_case"]),
 # --- the canonical decode
 ("m18:dec_json_off", [(ID, "    if _JSON_DOC.match(text) and (text.lstrip()[:1] != '\"' or \"\\\\\" in text):", "    if False:")],
  [UC + "::test_skc_a_json_value_only_json_loads_reads_back_is_a_carrier_and_evidence",
   UM + "::test_dec_json_a_short_w_as_a_whole_value_of_pretty_printed_ascii_json_is_identical"]),
 ("m18:dec_escape_off", [(ID, "        rest.append(lambda: [Reading(_unescape(text))])", "        pass")],
  [UM + "::test_dec_escape_a_cut_json_text_carries_w_through_its_escapes_only"]),
 ("m18:dec_repr_off", [(ID, "        rest.append(lambda: _literals(text))", "        pass")],
  [UM + "::test_dec_repr_a_repr_literal_only_unrepr_reads_back_carries_w"]),
 ("c3:recursion_open", [(ID, "            except RecursionError as exc:\n                raise _Capped from exc", "            except RecursionError:\n                return []")],
  [U3 + "::test_c3_json_nested_past_the_parser_is_capped_and_fails_closed"]),
 ("c3:spans_per_line_off", [(ID, "            for m in rx.finditer(text):", "            for m in [x for x in [rx.fullmatch(text)] if x]:")],
  [U3 + "::test_c3_a_piece_of_w_quoted_on_one_line_of_several_is_read_on_its_line"]),
 ("sk3:unrepr_cut_keep", [(ID, "                i += 2 + (part.end() if part else 0)\n                continue", "                out.append(e)\n                i += 2\n                continue")],
  [UD + "::test_m1_prev_a_preview_cut_inside_ws_escape_is_a_cut", UD + "::test_n4_a_repr_literal_cut_inside_an_escape_reads_what_came_before"]),
 ("sk3:open_literal_off", [(ID, "        if j >= n:  # open at the end: a cut literal\n            out.append(Reading(_unescape(body), unit=True, lead=True, cut_end=True))\n            break",
   "        if j >= n:  # open at the end: a cut literal\n            break")],
  [UD + "::test_n4_execute_code_summary_cutting_a_repr_escape_in_half_is_a_cut", UD + "::test_n4_a_repr_literal_cut_inside_an_escape_reads_what_came_before"]),
 ("sk3:cut_per_line_off", [(ID, "        points += [m.start() for m in re.finditer(r\"\\n\", text)]", "        pass")],
  [UD + "::test_m1_inline_truncation_fallback_cut_at_a_line_is_a_cut"]),
 # --- this rebuild's own guards
 ("r3:not_checked_off", [(ID, "    if c.capped:\n        out.add(NOT_CHECKED)", "    if False:\n        out.add(NOT_CHECKED)")],
  [UD + "::test_n2_json_nested_past_the_parser_is_capped", ID3 + "::test_derived3_a_row_the_decode_cannot_read_in_full_is_held_as_not_checked"]),
 ("r3:steps_cap_off", [(ID, "            if steps > CANON_STEPS:\n                capped = True\n                break", "            if False:\n                capped = True\n                break")],
  [UD + "::test_n2_more_decode_steps_than_the_cap_is_not_checked"]),
 ("r3:fields_off", [(ID, "    out = [get(\"content\") or \"\"]\n    calls = get(\"tool_calls\")\n    if calls:", "    out = [get(\"content\") or \"\"]\n    calls = get(\"tool_calls\")\n    if False:")],
  [ID3 + "::test_derived3_every_field_of_a_row_is_read_tool_call_arguments_too"]),
 ("r3:cut_off", [(ID, "    for p in _cut_points(r.text, r.cut_end):", "    for p in ():")],
  [UD + "::test_m1_prev_a_preview_cut_inside_ws_escape_is_a_cut"]),
 ("r3:lead_cut_off", [(ID, "    if (r.unit or r.lead) and len(f) < len(wf.w) and wf.w.startswith(f):", "    if False:")],
  [UC + "::test_p1_a_whole_value_that_is_a_beginning_of_w_is_a_cut"]),
 ("r3:spans_off", [(ID, "            for m in rx.finditer(text):", "            for m in ():")],
  [UD + "::test_n1_vision_analyze_summary_with_an_apostrophe_and_a_line_break_is_a_cut",
   U3 + "::test_c3_a_piece_of_w_quoted_on_one_line_of_several_is_read_on_its_line"]),
 ("r3:fqw_off", [(ID, "    if _QUOTE_LINE.search(text):", "    if False:")],
  [UD + "::test_13546_a_short_w_quoted_with_markers_in_a_session_search_result_is_identical", ID3 + "::test_derived3_13546_a_short_w_quoted_with_markers_stays_held_after_a_rollback"]),
 ("r3:carriers_off", [(RV, "    if texts:\n        # rebuild R3: carriers", "    if False:\n        # rebuild R3: carriers")],
  [IC + "::test_skc_a_native_partial_reply_quote_of_a_long_w_is_held_wherever_it_is_found"]),
 ("r3:carrier_exempt_off", [(RV, "                if await identity.exempt_covers(session, anchor, sid, row):\n                    continue  # released", "                if False:\n                    continue  # released")],
  [ID3 + "::test_derived3_a_nested_session_search_read_is_held_outside_every_group_until_released"]),
 ("r3:titles_off", [(RV, "            if wf is None or wf.generic or not title:", "            if True:")],
  [IC + "::test_p2_a_title_and_an_imported_memory_item_of_short_words_are_carriers_too"]),
 ("r3:items_off", [(RV, "        if carries([x for x in (item.title or \"\", item.body or \"\") if x]):", "        if False:")],
  [IC + "::test_p2_a_title_and_an_imported_memory_item_of_short_words_are_carriers_too"]),
 ("r3:copies_fields_off", [(RV, "            rel = identity.family_relations(identity.row_fields(row), w) & set(identity.TEXT_RELATIONS)", "            rel = identity.family_relations(row.get(\"content\") or \"\", w) & {\"identical\", \"contains\", \"part_of\", \"restated\"} if False else set()")],
  [ID3 + "::test_derived3_a_rollback_copy_whose_older_text_carries_w_in_a_derived_form_loses_it"]),
 ("r3:group_fields_off", [(ID, "        out |= family_relations(held, own)\n    for w in others:", "        out |= family_relations(held[:1] if isinstance(held, list) else held, own) - {CUT, NOT_CHECKED}\n    for w in others:")],
  [ID3 + "::test_derived3_a_group_row_cut_in_a_prune_line_is_never_none"]),
 ("r3:release_on_evidence_carrier_off", [("app/brain/groups.py", "RELEASE_ON_EVIDENCE = frozenset({identity.GROUP_HOLD, identity.UNCERTAIN_HOLD, identity.COPY_HOLD,\n                                 identity.CARRIER_HOLD})", "RELEASE_ON_EVIDENCE = frozenset({identity.GROUP_HOLD, identity.UNCERTAIN_HOLD, identity.COPY_HOLD})")],
  [ID3 + "::test_derived3_a_nested_session_search_read_is_held_outside_every_group_until_released"]),
 ("r3:preview_item_title_shown", [(ID, "            shown = \"\" if title and carrier_relations([title], message.content) else title", "            shown = title")],
  [IC + "::test_oci_the_withhold_preview_reads_another_case"]),
]
# cr2:quick_no_quote_bypass alone: every reply pointer also opens a quoted value (': "'), so the decode bypass compares
# it as well; shown COMPENSATED with the double
M.append(("cr2:quick_no_quote_bypass+cr2:quick_no_decode", [(ID, "        if len(f) > CANON_MAX_TEXT or _NEEDS_DECODE.search(f) or SNAPSHOT_LEAD.search(f) or _QUOTE_SHAPE.search(f):",
   "        if len(f) > CANON_MAX_TEXT or SNAPSHOT_LEAD.search(f):")],
  [UC + "::test_p2_a_reply_quote_of_sixteen_characters_as_written_is_never_skipped_by_the_quick_filter"]))
# sk3:cut_per_line_off alone: Hermes's inline fallback ends with a '[Truncated' mark, itself a cut point; shown
# COMPENSATED with the double (that mark not read as one)
M.append(("sk3:cut_per_line_off+r3:truncated_mark_off", [
    (ID, "        points += [m.start() for m in re.finditer(r\"\\n\", text)]", "        pass"),
    (ID, '_CUT_MARK = re.compile(r"…|\\.\\.\\.|\\[(?:output |content |message )?truncated|\\((?:output )?truncated|<truncated", re.I)',
     '_CUT_MARK = re.compile(r"…|\\.\\.\\.|\\((?:output )?truncated|<truncated", re.I)')],
  [UD + "::test_m1_inline_truncation_fallback_cut_at_a_line_is_a_cut"]))
only = sys.argv[1:]
db = "r3m"
env = dict(os.environ, TEST_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s" % db,
           TEST_CREDIT_REPAIR_DATABASE_URL="postgresql+asyncpg://cc:cc@127.0.0.1:55432/%s_cr" % db,
           HERMES_SRC="", REQUIRE_HERMES_SRC="")
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
    r = subprocess.run([PY, "-m", "pytest", "-p", "no:randomly", "-q", "-rA", *tests], cwd=os.path.join(root, "backend"),
                       env=env, capture_output=True, text=True, timeout=1800)
    last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or " error" in l][-1:] or [r.stdout[-200:]]
    verdict = "CAUGHT" if (r.returncode == 1 and " failed" in last[0] and " error" not in last[0]) else (
        "SURVIVED" if r.returncode == 0 else "ERROR")
    print(name, verdict, last[0], flush=True)
    out.write("%s\t%s\t%s\t%s\t%s\n" % (name, ",".join(sorted({e[0] for e in edits})), verdict, last[0], " ".join(tests))); out.flush()
if os.environ.get("KEEP") is None:
    shutil.rmtree(os.path.join(S, "copy"), ignore_errors=True)
