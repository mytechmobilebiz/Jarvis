"""Faithfulness of the simulator (plan 5.1): every operation against the vendored Hermes function, on a scratch SQLite.

hermes_driver.py runs the real Hermes functions (SessionDB.archive_and_compact, _publish_compression_child_on_conn with
and without _carry_parent_timestamps, rewrite_pruned_rows, the micro-compaction merge, the CLI and gateway /branch row
builders, rewind_user_turn, replace_messages, delete_session, the sliced uid backfill, the reply pointer and Discord
note builders, the replay header) in an isolated subprocess and prints each scratch state.db before and after. Each
test starts the simulator from the same rows and compares the result: row ids, roles, contents, active/compacted
flags, which uid each row carries (kept, fresh or none), which timestamp (kept or new), absorbed uids, and the
sessions' lineage columns.

HERMES_SRC names the vendored Hermes checkout. Without it the tests are skipped; REQUIRE_HERMES_SRC=1 makes that a
failure (the release gate sets it). No package is installed: the two third-party modules Hermes imports at module
level and these paths never use are stubbed inside the subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

import pytest

from . import hermes_sim as hs

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("HERMES_SRC", "")


@pytest.fixture(scope="module")
def real(tmp_path_factory) -> dict[str, Any]:
    if not SRC or not os.path.isdir(SRC):
        if os.environ.get("REQUIRE_HERMES_SRC") == "1":
            pytest.fail("HERMES_SRC is not set to the vendored Hermes source (REQUIRE_HERMES_SRC=1)")
        pytest.skip("HERMES_SRC not set: the conformance tests need the vendored Hermes source")
    out = tmp_path_factory.mktemp("hermes") / "real.json"
    done = subprocess.run([sys.executable, "-I", os.path.join(HERE, "hermes_driver.py"), SRC, str(out)],
                          capture_output=True, text=True, timeout=600, cwd=str(out.parent))
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(out.read_text())


def scenario(real: dict[str, Any], name: str) -> dict[str, Any]:
    data = real[name]
    assert "error" not in data, "the real Hermes function failed: %s\n%s" % (data["error"], data.get("trace", ""))
    return data


def sim_from(tmp_path, state: dict[str, Any]) -> hs.Sim:
    sim = hs.Sim(str(tmp_path / "sim"), 1, uids="modern", donor=True, compressed_marker=True, uid_trigger=True)
    sim.load_state(state["sessions"], state["rows"], state["seq"])
    return sim


def dump(sim: hs.Sim) -> dict[str, Any]:
    c = sim.con()
    try:
        rows = [{k: r[k] for k in ("id", "session_id", "role", "content", "tool_call_id", "timestamp", "active",
                                   "compacted", "message_uid", "absorbed_message_uids")}
                for r in c.execute("SELECT * FROM messages ORDER BY id")]
        sessions = [dict(r) for r in c.execute("SELECT * FROM sessions ORDER BY id")]
    finally:
        c.close()
    return {"rows": rows, "sessions": sessions}


MARKERS = ("_branched_from", "_compressed_from", "_reset_from", "_delegate_from")


def project(before: dict[str, Any], after: dict[str, Any], *, mask_new_answers: bool = True,
            mask_all_content: bool = False) -> dict[str, Any]:
    """The comparable shape of a state.db: uids as 'kept uid k' / 'fresh n' / None, times as 'kept time k' / 'new'."""
    uid_of = {}
    for r in before["rows"]:
        if r["message_uid"] and r["message_uid"] not in uid_of:
            uid_of[r["message_uid"]] = "U%d" % len(uid_of)
    times = sorted({r["timestamp"] for r in before["rows"]})
    fresh: dict[str, str] = {}
    before_texts = {r["content"] for r in before["rows"]}

    def uid(u: str | None) -> str | None:
        if not u:
            return None
        if u in uid_of:
            return uid_of[u]
        return fresh.setdefault(u, "N%d" % len(fresh))

    rows = []
    for r in sorted(after["rows"], key=lambda x: x["id"]):
        ts = "T%d" % times.index(r["timestamp"]) if r["timestamp"] in times else "new"
        content = r["content"]
        if mask_all_content or (mask_new_answers and r["role"] == "assistant" and ts == "new"
                                and content not in before_texts and not (content or "").startswith("[CONTEXT")):
            content = "<%s text>" % r["role"]
        call = ("<call>" if r["tool_call_id"] else None) if mask_all_content else r["tool_call_id"]
        rows.append((r["id"], r["session_id"], r["role"], content, call, ts, r["active"],
                     r["compacted"], uid(r["message_uid"]),
                     tuple(uid(a) for a in json.loads(r["absorbed_message_uids"] or "[]"))))
    sessions = []
    for s in sorted(after["sessions"], key=lambda x: x["id"]):
        cfg = json.loads(s.get("model_config") or "{}") or {}
        sessions.append((s["id"], s.get("parent_session_id"), tuple(sorted((k, v) for k, v in cfg.items()
                                                                           if k in MARKERS)),
                         s.get("end_reason") if s.get("end_reason") in ("compression", "branched") else None))
    return {"rows": rows, "sessions": sessions}


def same(real_s: dict[str, Any], sim: hs.Sim, **kw: Any) -> None:
    want = project(real_s["before"], real_s["after"], **kw)
    got = project(real_s["before"], dump(sim), **kw)
    assert got["sessions"] == want["sessions"]
    for n, (g, w) in enumerate(zip(got["rows"], want["rows"])):
        assert g == w, "row %d differs:\n  simulator %r\n  Hermes    %r" % (n, g, w)
    assert len(got["rows"]) == len(want["rows"]), (got["rows"][len(want["rows"]):], want["rows"][len(got["rows"]):])


# --- one test per operation (plan 5.1) ---------------------------------------------------------------------------


def test_append(real, tmp_path):
    s = scenario(real, "append")
    sim = sim_from(tmp_path, s["before"])
    sim.append("s1", tool=True)
    same(s, sim, mask_all_content=True)


def test_independent_identical_message_in_the_same_second(real, tmp_path):
    s = scenario(real, "same_second_repeat")
    sim = sim_from(tmp_path, s["before"])
    first = s["before"]["rows"][0]
    lm = sim.tags[("s1", first["id"])].origin
    sim.append("s1", repeat_of=lm, same_second_as=first["timestamp"], answer=False)
    same(s, sim)
    a, b = s["after"]["rows"]
    assert a["message_uid"] != b["message_uid"] and a["timestamp"] == b["timestamp"]  # nothing ties them together


@pytest.mark.parametrize("name", ["compact_head_tail", "compact_no_head", "compact_restate_row",
                                  "compact_restate_carrier"])
def test_in_place_compaction_head_and_protected_tail(real, tmp_path, name):
    s = scenario(real, name)
    sim = sim_from(tmp_path, s["before"])
    p = s["params"]
    sim.compact("s1", p["head_n"], p["tail_n"], restate=p["restate"])
    same(s, sim)


@pytest.mark.parametrize("name", ["rotate_donor", "rotate_no_donor", "rotate_legacy_rows"])
def test_rotating_compaction_with_and_without_donor_timestamps(real, tmp_path, name):
    s = scenario(real, name)
    sim = sim_from(tmp_path, s["before"])
    p = s["params"]
    sim.rotate("r1", p["head_n"], p["tail_n"], donor=p["donor"], marker=True, child="r1c")
    same(s, sim)
    if name == "rotate_donor":  # first active match by content wins: the later 'Sounds good.' gets the earlier time
        copy = [r for r in s["after"]["rows"] if r["session_id"] == "r1c" and r["content"] == "Sounds good."][0]
        assert copy["timestamp"] == s["before"]["rows"][1]["timestamp"]


@pytest.mark.parametrize("name", ["prune_with_uid", "prune_null_uid"])
def test_rewrite_pruned_rows(real, tmp_path, name):
    s = scenario(real, name)
    sim = sim_from(tmp_path, s["before"])
    sim.prune("s1", s["params"]["row"])
    same(s, sim)


@pytest.mark.parametrize("name", ["fold_with_uid", "fold_no_uid"])
def test_micro_compaction_fold(real, tmp_path, name):
    s = scenario(real, name)
    sim = sim_from(tmp_path, s["before"])
    assert sim.fold("f1", pair=tuple(s["params"]["pair"])) is not None
    same(s, sim)


def test_cli_branch_keeps_uids(real, tmp_path):
    s = scenario(real, "cli_branch")
    sim = sim_from(tmp_path, s["before"])
    sim.cli_branch("s1", child="s1b")
    same(s, sim)


def test_gateway_branch_fresh_uid_copied_time(real, tmp_path):
    s = scenario(real, "gateway_branch")
    sim = sim_from(tmp_path, s["before"])
    sim.gateway_branch("s1", child="s1b")
    same(s, sim)


def test_cli_retry_rewinds_and_resends(real, tmp_path):
    s = scenario(real, "cli_retry")
    sim = sim_from(tmp_path, s["before"])
    sim.cli_retry("s1")
    same(s, sim)


def test_gateway_retry_rewrites_the_transcript(real, tmp_path):
    s = scenario(real, "gateway_retry")
    sim = sim_from(tmp_path, s["before"])
    sim.gateway_retry("g1")
    same(s, sim)


@pytest.mark.parametrize("name", ["replace_destructive", "replace_archive_dropped"])
def test_replace_messages(real, tmp_path, name):
    s = scenario(real, name)
    sim = sim_from(tmp_path, s["before"])
    sim.replace("s1", archive_dropped=s["params"]["archive_dropped"], drop_last_turn=True)
    same(s, sim)


def test_delete_session_orphans_children(real, tmp_path):
    s = scenario(real, "delete_session")
    sim = sim_from(tmp_path, s["before"])
    sim.delete_session("d1")
    same(s, sim)


def test_gateway_reset_copies_nothing(real, tmp_path):
    s = scenario(real, "reset")
    sim = sim_from(tmp_path, s["before"])
    sim.reset("e1", child="e1n")
    same(s, sim)


def test_sliced_uid_backfill_tiny_budget(real, tmp_path):
    s = scenario(real, "backfill_slice")
    sim = sim_from(tmp_path, s["before"])
    sim.backfill_done_through = 0
    sim.backfill(chunk=s["params"]["chunk"])
    same(s, sim)


def test_snapshot_restore_reuses_row_ids(real, tmp_path):
    s = scenario(real, "snapshot_restore")
    sim = sim_from(tmp_path, s["before"])
    cp = sim.checkpoint()
    sim.append("k1", answer=False)
    lost = dump(sim)["rows"][-1]["id"]
    sim.restore_checkpoint(cp)
    sim.append("k1", answer=False)
    same(s, sim, mask_all_content=True)
    assert lost == dump(sim)["rows"][-1]["id"] == s["lost"]["rows"][-1]["id"] == s["after"]["rows"][-1]["id"]


def test_text_wrappers_reply_pointer_discord_note_replay_header(real):
    w = scenario(real, "wrappers")
    assert w["reply"] == hs.reply_pointer("the vendor review before the weekly call", "Is it done?")
    assert w["reply_own"] == hs.reply_pointer("Done, the vendor review is ready.", "Thanks.", own=True)
    assert w["discord"] == hs.discord_note("1234567892") + "\n\nPlease archive it."
    assert w["discord_strip"] == "Please archive it."  # what the newest gateway persists (the note is peeled)
    assert w["replay_header"] == hs.REPLAY_HEADER
    assert w["restated"] == hs._restated(hs.REPLAY_HEADER + "\n  Please finish it.  ") == "Please finish it."
    assert w["fresh_copy_keeps_uid"]["message_uid"] == "u1"  # the restatement keeps the in-flight row's uid
    assert "timestamp" in w["branch_copy_keys"] and "message_uid" not in w["branch_copy_keys"]
    assert "message_uid" not in w["gateway_branch_row"] and w["gateway_branch_row"]["timestamp"] is not None
