"""Tests of the JARVIS migration kit against a synthetic installation (tests/make_fixture.py).

The kit must read only, report counts and structure only, and back up without secrets."""

import ast
import builtins
import hashlib
import io
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jarvis_kit as kit  # noqa: E402
from make_fixture import DETAIL_ONLY, LUHN_UID, NOT_IN_BACKUP, PLANTED, build  # noqa: E402

PASSPHRASE = "correct horse battery staple 42"
# Planted values that may appear in no report and no restored file at all.
NEVER_REPORTED = [k for k in PLANTED if k not in DETAIL_ONLY]


def tree_state(root):
    state = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            digest = None
            if os.path.isfile(full) and not os.path.islink(full):
                with open(full, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
            state[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns, digest)
    return state


@pytest.fixture(scope="module")
def install(tmp_path_factory):
    return build(str(tmp_path_factory.mktemp("jarvis")))


@pytest.fixture(scope="module")
def discovered(install, tmp_path_factory):
    """One full discovery run, recording every file the kit opens and how."""
    out = str(tmp_path_factory.mktemp("discovery"))
    before = tree_state(install["root"])
    opened, connected = [], []
    real_open, real_connect = builtins.open, sqlite3.connect

    def watching_open(file, mode="r", *args, **kwargs):
        opened.append((os.path.realpath(str(file)), mode))
        return real_open(file, mode, *args, **kwargs)

    def watching_connect(target, *args, **kwargs):
        connected.append((target, kwargs))
        return real_connect(target, *args, **kwargs)
    builtins.open, sqlite3.connect = watching_open, watching_connect
    try:
        code = kit.main(["discover", "--profile", install["profile"], "--content", install["content"],
                         "--db", install["db"], "--out", out, "--full", "--check"])
    finally:
        builtins.open, sqlite3.connect = real_open, real_connect
    files = {name: open(os.path.join(out, name), encoding="utf-8").read() for name in os.listdir(out)}
    return {"code": code, "out": out, "files": files, "opened": opened, "connected": connected,
            "before": before, "after": tree_state(install["root"]),
            "summary": json.loads(files["discovery-summary.json"]),
            "detail": json.loads(files["discovery-detail.json"])}


def profile_files(discovered):
    return {f["path"]: f for f in discovered["detail"]["files"] if f["area"] == "profile"}


def test_discovery_writes_three_reports(discovered):
    assert discovered["code"] == 0
    assert sorted(discovered["files"]) == ["discovery-detail.json", "discovery-summary.json", "discovery-summary.md"]


def test_the_installation_is_not_changed(install, discovered):
    assert discovered["after"] == discovered["before"]
    assert not any(os.path.exists(install["db"] + s) for s in ("-wal", "-shm", "-journal"))


def test_source_files_are_only_opened_for_reading(install, discovered):
    root = os.path.realpath(install["root"])
    source = [(p, m) for p, m in discovered["opened"] if p.startswith(root)]
    assert source, "the watcher saw no reads"
    assert all(m in ("rb", "r") for _, m in source), source
    opened = {p for p, _ in source}
    for never in ("state.db", "state.db-wal", "bin/hermes.exe", "__pycache__/x.cpython-312.pyc"):
        assert os.path.realpath(os.path.join(install["profile"], never)) not in opened
    assert not any("/outside/" in p for p in opened), "a link was followed out of the profile"


def test_the_backup_is_opened_read_only_and_immutable(install, discovered):
    targets = [t for t, _ in discovered["connected"]]
    assert targets and all(t.startswith("file:") and t.endswith("?mode=ro&immutable=1") for t in targets)


@pytest.mark.parametrize("report", ["discovery-summary.json", "discovery-summary.md", "discovery-detail.json"])
def test_no_secret_personal_data_content_or_reasoning_in_any_report(discovered, report):
    text = discovered["files"][report]
    if report == "discovery-detail.json":
        # The owner's own report may name the keys of a credential file (structure, like a file name), nothing else.
        detail = json.loads(text)
        for f in detail["files"]:
            f.pop("keys", None)
        text = json.dumps(detail)
    for key in NEVER_REPORTED:
        assert PLANTED[key] not in text, (report, key)
    assert "planted-mail" not in text and "planted-client.example" not in text and "GOCSPX" not in text
    assert "PlantedPemTail" not in text, "a line of a multi-line key was taken for a setting name"


@pytest.mark.parametrize("report", ["discovery-summary.json", "discovery-summary.md"])
def test_the_summary_names_no_file_skill_job_or_client_chosen_by_the_owner(discovered, report):
    text = discovered["files"][report]
    for key in DETAIL_ONLY:
        assert PLANTED[key] not in text, key
    assert "Plantedcustomer" not in text and "JANE_" not in text and "acmedental" not in text.lower()


def test_the_detail_report_has_the_paths_for_the_owner(discovered):
    paths = {f["path"] for f in discovered["detail"]["files"]}
    assert "knowledge/clients/%s.md" % PLANTED["custom_file"] in paths
    skipped = {s["path"] for s in discovered["detail"]["skipped"]}
    assert {"knowledge/linked.md", "linked-folder"} <= skipped


def test_files_are_categorized(discovered):
    cats = {(f["area"], f["path"]): f["category"] for f in discovered["detail"]["files"]}
    p = lambda path: cats[("profile", path)]  # noqa: E731
    assert p(".env") == p("auth.json") == p("local-api.json") == p("credentials.json") == "secret"
    assert p(".envrc") == p("postiz.env") == p(".env.%s" % PLANTED["customer_slug"]) == "secret"
    assert p(".ssh/id_ed25519_github") == "secret"
    assert p("platforms/whatsapp/session/creds.json") == p("whatsapp/session/pre-key-1.json") == "secret"
    assert p("workspace/clients/%s/.env" % PLANTED["customer_slug"]) == "secret"
    assert p("config.yaml.malformed-backup") == "temporary"
    assert p("state.db") == p("state.db-wal") == "database"
    assert p("__pycache__/x.cpython-312.pyc") == p("node_modules/pkg/index.js") == "cache"
    assert p("bin/hermes.exe") == "program"
    assert p("sandbox/scratch.txt") == p("draft.tmp") == "temporary"
    assert p("logs/errors.log") == "log"
    assert p("sessions/session_20261001.json") == "session_transcript"
    assert p("memories/MEMORY.md") == "memory"
    assert p("SOUL.md") == p("operations.md") == "instructions"
    assert p("config.yaml") == "configuration"
    assert p("cron/jobs.json") == "schedule"
    assert p("workspace/proposals/sent-log.jsonl") == "workspace"
    for store in ("npmrc", "browser-profile/Default/Login Data", "browser-profile/Default/Preferences",
                  "mcp-tokens/postiz.json", "vault/vault.json.enc", "backups/hermes-backup-20261001.zip"):
        assert p(store) == "secret", store


def test_secret_files_give_setting_names_only(discovered):
    secrets = {s["file"]: s for s in discovered["summary"]["secret_files"]}
    env = secrets[".env"]
    assert env["variables"] == ["ANTHROPIC_API_KEY", "EMAIL_PASSWORD", "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"]
    assert env["other_variables"] == 4 and env["empty_values"] == 2
    detail = profile_files(discovered)[".env"]
    assert detail["variables"] == sorted(["AFTER_KEY", "ANTHROPIC_API_KEY", "EMAIL_PASSWORD", "ENCRYPTION_KEY",
                                          "GOOGLE_SA_PRIVATE_KEY", "JANE_PLANTEDCUSTOMER_SMTP_PASSWORD",
                                          "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"])
    assert detail["value_like_lines"] == 1, "a value pasted on its own line is not a setting name"
    assert "providers.claude-cli.access_token" in secrets["auth.json"]["keys"]
    assert "accounts.(key)" in secrets["auth.json"]["keys"], "an e-mail address used as a key must not be shown"
    assert set(secrets["credentials.json"]["keys"]) == {"(key)", "(key).(key)", "(key).(key).(key)", "client_id",
                                                        "client_secret"}
    others = [f for f in secrets if f.startswith("secret file")]
    assert len(others) >= 8 and any(f.endswith("(.env file)") for f in others)
    assert any(f.endswith("in skills/") for f in others) and any(f.endswith("in workspace/") for f in others)
    assert discovered["summary"]["linked_device_sessions"]["whatsapp"].startswith("present")


def test_templates_are_recognized_only_at_their_own_place(discovered):
    files = profile_files(discovered)
    assert files["SOUL.md"]["template"] == "identical"
    assert files["knowledge/product.md"]["template"] == "identical", "CRLF line endings must not count as a change"
    assert files["skills/morning-brief/SKILL.md"]["template"] == "identical"
    ops = files["operations.md"]
    assert ops["template"] == "modified" and ops["lines_added"] >= 3 and ops["lines_removed"] == 0
    assert files["knowledge/sarah/faq.md"]["template"] == "modified"
    assert files["knowledge/clients/%s.md" % PLANTED["custom_file"]]["template"] == "not from a template"
    stub = files["knowledge/clients/%s-notes.md" % PLANTED["customer_slug"]]
    assert stub["template"] == "same content as a template"
    copy = files["skills/%s-onboarding/SKILL.md" % PLANTED["customer_slug"]]
    assert copy["template"] == "same content as a template"
    skills = discovered["summary"]["skills"]
    assert skills["total"] == 14 and skills["other_skills"] == 2
    assert "triage-inbox" in skills["identical_to_template"]
    review = {r["file"] for r in discovered["summary"]["files_to_review"]}
    assert "SOUL.md" in review and "skills/triage-inbox/SKILL.md" in review


def test_findings_are_counted_not_copied(discovered):
    files = profile_files(discovered)
    assert files["config.yaml"]["secret_like"] == {"url_path_secret": 1, "url_secret_parameter": 1}
    notes = files["knowledge/sarah/smtp-notes.md"]["secret_like"]
    assert notes == {"credential_assignment": 5, "cookie": 1, "basic_auth": 1, "cookie_argument": 1,
                     "wordpress_app_password": 1, "gmail_app_password": 1, "webhook_url": 1,
                     "bare_authorization": 1}
    assert files["workspace/oauth-state.json"]["secret_like"] == {"credential_assignment": 1}
    assert files["scripts/publish.cmd"]["secret_like"] == {"credential_assignment": 1}
    assert files["scripts/deploy.sh"]["secret_like"] == {"credential_assignment": 1}
    assert files["scripts/headers.ps1"]["secret_like"] == {"basic_auth": 1, "env_command": 1}
    assert files["scripts/owner_tool.py"]["secret_like"] == {"credential_assignment": 1}
    assert not files["scripts/owner_tool.js"]["secret_like"]
    assert files["memories/MEMORY.md"]["secret_like"].get("anthropic_key") == 1
    assert files["memories/MEMORY.md"]["instruction_like"].get("override_attempt") == 1
    assert files["memories/MEMORY.md"]["entries"] == 4
    assert files["operations.md"]["instruction_like"].get("approval_bypass", 0) >= 1
    assert files["knowledge/clients/%s.md" % PLANTED["custom_file"]]["personal_data"] == {"us_ssn": 1}
    faq = files["knowledge/sarah/faq.md"]["personal_data"]
    assert faq.get("email_address") == 1 and faq.get("phone_number") == 1
    # JARVIS's own scripts read their settings in code: that is not a secret.
    assert not files["scripts/send_proposal.py"]["secret_like"]
    assert not files["scripts/check_websites.py"]["secret_like"]
    assert files["workspace/notes.txt"]["secret_like"] == {"anthropic_key": 1}, "UTF-16 text must be scanned"
    ledger = files["workspace/ledger.jsonl"]
    assert ledger["scanned"] is False and ledger["not_scanned_because"].startswith("larger than")


def test_config_and_jobs_without_secrets(discovered):
    summary = discovered["summary"]
    config = summary["config_yaml"]
    assert config["values"]["model.provider"] == "claude-cli" and config["values"]["approvals.cron_mode"] == "deny"
    assert config["mcp_servers"] == ["hud", "postiz", "mcp server 1", "mcp server 2"]
    assert "quick_commands.(key)" in config["keys"] and "whatsapp.chats.(key)" in config["keys"]
    jobs = {j["name"]: j for j in summary["scheduled_jobs"]}
    assert jobs["morning-brief"]["enabled"] is True and jobs["morning-brief"]["schedule"] == "0 8 * * *"
    custom = jobs["custom job 2"]
    assert custom["deliver"] == "whatsapp" and custom["skills"] == ["custom skill"]


def test_database_inventory(discovered):
    db = discovered["summary"]["database_backup"]
    assert db["quick_check"] == "ok" and not db["files_next_to_it"]
    assert db["tables"]["sessions"]["rows"] == 30 and db["tables"]["messages"]["rows"] == 127
    assert db["tables"]["messages_fts"]["kind"] == "full-text index" and db["tables"]["messages_fts"]["rows"] == 127
    sessions = db["sessions"]
    assert sessions["scheduled_job_sessions"] == 10
    assert sessions["scheduled_job_sessions_by_job_id"] == {"a1b2c3": 6, "d4e5f6": 4}
    assert sessions["archived"] == 2 and sessions["continuations_after_compression"] == 1
    assert sessions["branches"] == 1 and sessions["delegated"] == 1
    messages = db["messages"]
    assert messages["without_a_session"] == 1 and messages["content_with_think_tags"] == 1
    assert messages["with_tool_calls"] == 30 and messages["tool_results"] == 31
    assert messages["live"] == 122 and messages["archived_by_compaction"] == 4
    assert messages["rewound_or_superseded"] == 1
    assert db["reasoning_columns"] == {"messages": ["reasoning", "reasoning_details"]}
    roles = db["tables"]["messages"]["values"]["role"]
    assert roles == {"user": 34, "assistant": 62, "tool": 31}
    tools = db["tables"]["messages"]["values"]["tool_name"]
    assert tools.get("(tool of another mcp server)") == 1 and tools.get("terminal") == 29
    assert db["tables"]["sessions"]["time"]["started_at"]["first"] == "2026-07-01"


def test_database_content_is_scanned_for_counts(discovered):
    scan = discovered["summary"]["database_backup"]["content_scan"]["messages"]
    assert scan["mode"] == "all rows" and scan["rows_scanned"] == 127
    assert "reasoning" not in scan["columns_scanned"] and "reasoning_details" not in scan["columns_scanned"]
    assert scan["secret_like"].get("anthropic_key") == 1
    assert scan["personal_data"]["us_ssn"] == 1 and scan["personal_data"]["payment_card"] == 1, \
        "identifiers (message_uid) must not be scanned as text"
    assert "message_uid" not in scan["columns_scanned"] and "tool_call_uids" not in scan["columns_scanned"]
    assert scan["duplicate_rows_in_scan"] >= 100, "repeated content counts as duplicate whatever its identifiers"
    assert scan["instruction_like_by_role"]["tool"]["override_attempt"] == 1
    sessions_scan = discovered["summary"]["database_backup"]["content_scan"]["sessions"]
    assert "system_prompt" in sessions_scan["columns_scanned"] and "title" in sessions_scan["columns_scanned"]


def test_a_sample_reads_only_part_of_the_database(install, tmp_path):
    assert kit.main(["discover", "--profile", install["profile"], "--content", install["content"],
                     "--db", install["db"], "--out", str(tmp_path), "--sample", "20"]) == 0
    summary = json.loads((tmp_path / "discovery-summary.json").read_text())
    scan = summary["database_backup"]["content_scan"]["messages"]
    assert scan["mode"] == "sample" and scan["rows_scanned"] <= 20


def test_damaged_text_is_neither_fatal_nor_printed(install, tmp_path, capfd):
    assert kit.main(["discover", "--profile", install["profile"], "--content", install["content"],
                     "--db", install["db"], "--out", str(tmp_path)]) == 0
    out, err = capfd.readouterr()
    assert PLANTED["bad_utf8"] not in out + err


def test_an_unexpected_error_never_prints_its_text(install, tmp_path, capfd, monkeypatch):
    def failing(*_args):
        raise RuntimeError("could not read %s" % PLANTED["message"])
    monkeypatch.setattr(kit, "analyze_database", failing)
    assert kit.main(["discover", "--profile", install["profile"], "--content", install["content"],
                     "--db", install["db"], "--out", str(tmp_path)]) == 1
    out, err = capfd.readouterr()
    assert "RuntimeError" in err and PLANTED["message"] not in out + err


def test_the_database_cannot_be_written(install):
    con = kit.open_backup(install["db"])
    try:
        for sql in ("CREATE TABLE x (a)", "INSERT INTO gateway_routing VALUES ('a', 'b', 'c', 1)",
                    "DELETE FROM messages", "PRAGMA query_only = OFF", "PRAGMA writable_schema = ON",
                    "ATTACH DATABASE ':memory:' AS other"):
            with pytest.raises(sqlite3.DatabaseError):
                con.execute(sql)
    finally:
        con.close()


@pytest.mark.parametrize("case", ["out inside profile", "db inside profile", "out not empty", "not a database"])
def test_refusals(install, tmp_path, case):
    out = str(tmp_path)
    db = install["db"]
    if case == "out inside profile":
        out = os.path.join(install["profile"], "workspace")
    elif case == "db inside profile":
        db = os.path.join(install["profile"], "state.db")
    elif case == "out not empty":
        (tmp_path / "x").write_text("x")
    else:
        db = os.path.join(install["profile"], "SOUL.md")
    before = tree_state(install["root"])
    assert kit.main(["discover", "--profile", install["profile"], "--content", install["content"],
                     "--db", db, "--out", out]) == 2
    assert tree_state(install["root"]) == before


def run_with_passphrase(monkeypatch, argv, passphrase=PASSPHRASE):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO((passphrase + "\r\n").encode("ascii"))))
    return kit.main(argv)


@pytest.fixture(scope="module")
def backup(install, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("backup"))
    before = tree_state(install["root"])
    mp = pytest.MonkeyPatch()
    try:
        code = run_with_passphrase(mp, ["backup", "--profile", install["profile"], "--content", install["content"],
                                        "--out", out])
    finally:
        mp.undo()
    archive = [n for n in os.listdir(out) if n.endswith(".ccjb")][0]
    return {"code": code, "out": out, "archive": os.path.join(out, archive), "before": before,
            "after": tree_state(install["root"])}


@pytest.fixture(scope="module")
def restored(backup, tmp_path_factory):
    target = tmp_path_factory.mktemp("restored")
    mp = pytest.MonkeyPatch()
    try:
        code = run_with_passphrase(mp, ["verify", "--archive", backup["archive"], "--extract", str(target)])
    finally:
        mp.undo()
    assert code == 0
    return target


def test_backup_is_written_without_changing_the_installation(backup):
    assert backup["code"] == 0
    assert backup["after"] == backup["before"]
    names = sorted(os.listdir(backup["out"]))
    assert len(names) == 4 and {"backup-report.json", "backup-files.json"} <= set(names)


def test_backup_is_encrypted(backup):
    raw = open(backup["archive"], "rb").read()
    assert raw.startswith(kit.MAGIC)
    for plain in (b"Owner prefers short replies", b"Closed 3 tickets", b"MANIFEST", b"Compose the brief"):
        assert plain not in raw


def test_backup_report_names_no_file(backup):
    report = open(os.path.join(backup["out"], "backup-report.json"), encoding="utf-8").read()
    for key in PLANTED:
        assert PLANTED[key] not in report
    data = json.loads(report)
    assert data["files_included"] > 50 and data["redactions"] >= 10
    assert data["included_not_scanned"] == {"document or media file": 1}


def test_backup_restores_without_secrets(restored, install):
    prof = restored / "profile"
    for left_out in (".env", ".envrc", "postiz.env", ".env.%s" % PLANTED["customer_slug"], "auth.json",
                     "local-api.json", "credentials.json", ".ssh/id_ed25519_github", "config.yaml.malformed-backup",
                     "platforms/whatsapp/session/creds.json", "whatsapp/session/pre-key-1.json",
                     "skills/gmail-%s/token.json" % PLANTED["customer_slug"],
                     "workspace/clients/%s/.env" % PLANTED["customer_slug"], "state.db", "state.db-wal",
                     "logs/errors.log", "sessions/session_20261001.json", "__pycache__/x.cpython-312.pyc",
                     "bin/hermes.exe", "sandbox/scratch.txt", "draft.tmp", "demo/brief.md", "knowledge/linked.md",
                     "node_modules/pkg/index.js"):
        assert not (prof / left_out).exists(), left_out
    config = (prof / "config.yaml").read_text()
    assert "/mcp/[REDACTED:url_path_secret]" in config and "apiKey=[REDACTED:url_secret_parameter]" in config
    memory = (prof / "memories" / "MEMORY.md").read_text()
    assert PLANTED["memory_key"] not in memory and PLANTED["memory_text"] in memory
    notes = (prof / "workspace" / "notes.txt").read_bytes()
    assert notes.startswith(b"\xff\xfe") and "[REDACTED:anthropic_key]" in notes[2:].decode("utf-16-le")
    assert (prof / "workspace" / "ledger.jsonl").read_text().endswith('{"key": "[REDACTED:anthropic_key]"}\n')
    everything = b"".join(p.read_bytes() for p in restored.rglob("*") if p.is_file())
    for key in NOT_IN_BACKUP:
        assert PLANTED[key].encode() not in everything, key
        assert PLANTED[key].encode("utf-16-le") not in everything, key


def test_backup_keeps_files_byte_for_byte_where_nothing_was_redacted(restored, install):
    prof = restored / "profile"
    for path in ("knowledge/product.md", "scripts/send_proposal.py", "scripts/check_websites.py",
                 "workspace/proposals/%s-proposal.pdf" % PLANTED["custom_file"]):
        assert (prof / path).read_bytes() == open(os.path.join(install["profile"], path), "rb").read(), path
    for script in ("send_proposal.py", "check_websites.py"):
        ast.parse((prof / "scripts" / script).read_text())


def test_restore_lists_what_was_redacted(restored):
    listing = json.loads((restored / "backup-files.json").read_text())
    entries = {e["path"]: e for e in listing["files"]}
    assert entries["profile/config.yaml"]["redactions"] == 2
    assert entries["profile/workspace/proposals/%s-proposal.pdf" % PLANTED["custom_file"]]["scanned"] is False


def test_a_file_that_vanishes_during_the_backup_is_counted(monkeypatch, install, tmp_path):
    real_plan = kit.backup_plan

    def plan_with_a_vanished_file(area, root):
        plan, left_out = real_plan(area, root)
        rel, full, st, category = plan[0]
        return plan + [("gone.md", full + ".gone", st, category)], left_out
    monkeypatch.setattr(kit, "backup_plan", plan_with_a_vanished_file)
    assert run_with_passphrase(monkeypatch, ["backup", "--profile", install["profile"], "--content",
                                             install["content"], "--out", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "backup-report.json").read_text())
    assert report["left_out"]["profile: changing, removed or unreadable during the backup"] == 1


def test_restore_works_where_file_times_cannot_be_set(monkeypatch, backup, tmp_path):
    def refuse(*_args, **_kwargs):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "utime", refuse)
    assert run_with_passphrase(monkeypatch, ["verify", "--archive", backup["archive"], "--extract",
                                             str(tmp_path)]) == 0
    assert (tmp_path / "profile" / "SOUL.md").exists()


def test_wrong_passphrase_fails(monkeypatch, backup):
    assert run_with_passphrase(monkeypatch, ["verify", "--archive", backup["archive"]], "a different passphrase") == 1


@pytest.mark.parametrize("damage", ["flip a byte", "cut off the end", "add bytes"])
def test_a_damaged_archive_fails(monkeypatch, backup, tmp_path, damage):
    raw = bytearray(open(backup["archive"], "rb").read())
    if damage == "flip a byte":
        raw[len(raw) // 2] ^= 1
    elif damage == "cut off the end":
        raw = raw[:-100]
    else:
        raw += b"\x00" * 10
    copy = tmp_path / "damaged.ccjb"
    copy.write_bytes(bytes(raw))
    assert run_with_passphrase(monkeypatch, ["verify", "--archive", str(copy)]) == 1


def test_a_short_passphrase_is_refused(monkeypatch, install, tmp_path):
    assert run_with_passphrase(monkeypatch, ["backup", "--profile", install["profile"], "--content", install["content"],
                                             "--out", str(tmp_path)], "short") == 2
    assert os.listdir(tmp_path) == []


@pytest.mark.parametrize("text", [
    "api_key: ${ANTHROPIC_API_KEY}", "password = your-password-here", "token: <your token>", "password: changeme",
    "secret: {{SECRET}}", "token: [REDACTED:slack_token]"])
def test_placeholders_are_kept(text):
    assert kit.redact(text) == (text, 0)


@pytest.mark.parametrize("text, value", [
    ("smtp_password: Trueblue#2024", "Trueblue#2024"), ("EMAIL_PASSWORD=Yourfirm2024!", "Yourfirm2024!"),
    ("password: $unny-Day-77", "$unny-Day-77"), ('password: "correct horse battery"', "horse battery"),
    ("token: abcdef123456", "abcdef123456"), ("Set-Cookie: a=1; sessionid=abcdefgh123", "sessionid=abcdefgh123"),
    ("app password abcd EFGH ij1l MNOP qrst UV2X", "UV2X"),
    ("url: https://api.postiz.com/mcp/0123456789abcdef0123", "0123456789abcdef0123")])
def test_real_values_are_redacted(text, value):
    redacted, n = kit.redact(text)
    assert n >= 1 and value not in redacted


@pytest.mark.parametrize("line", [
    'password = os.environ.get("EMAIL_PASSWORD")', "token = raw.strip().lstrip('-')",
    'token = base64.b64encode(f"{user}:{password}".encode())', "password = password_field"])
def test_code_reading_a_setting_is_not_redacted_in_a_program(line):
    assert kit.redact(line, "code") == (line, 0)


def test_quoted_secrets_in_programs_are_redacted():
    redacted, n = kit.redact('PASSWORD = "Hunter2-Really"\n', "code")
    assert n == 1 and "Hunter2" not in redacted


def test_the_uid_with_digits_would_count_as_a_card_if_it_were_scanned():
    assert kit.count(kit.PERSONAL_PATTERNS, LUHN_UID) == {"payment_card": 1}


def test_owner_scripts_still_work_after_the_backup(restored, install):
    prof = restored / "profile"
    tool = (prof / "scripts" / "owner_tool.py").read_text()
    ast.parse(tool)
    original = open(os.path.join(install["profile"], "scripts", "owner_tool.py")).read()
    assert tool == original.replace(PLANTED["py_quoted"], "[REDACTED:credential_assignment]")
    assert (prof / "scripts" / "owner_tool.js").read_bytes() == open(
        os.path.join(install["profile"], "scripts", "owner_tool.js"), "rb").read()
    for script in ("publish.cmd", "deploy.sh", "headers.ps1"):
        text = (prof / "scripts" / script).read_text()
        assert "[REDACTED:" in text, script


def test_hermes_credential_stores_are_left_out(restored):
    prof = restored / "profile"
    for store in ("npmrc", "browser-profile", "mcp-tokens", "vault", "backups"):
        assert not (prof / store).exists(), store


def test_discovery_survives_a_file_that_vanishes(monkeypatch, install, tmp_path):
    real_walk = kit.walk

    def walk_with_a_vanished_file(root):
        files, skipped = real_walk(root)
        rel, full, st = files[0]
        return files + [("knowledge/vanished.md", full + ".vanished", st)], skipped
    monkeypatch.setattr(kit, "walk", walk_with_a_vanished_file)
    assert kit.main(["discover", "--profile", install["profile"], "--content", install["content"],
                     "--out", str(tmp_path)]) == 0
    detail = json.loads((tmp_path / "discovery-detail.json").read_text())
    vanished = [f for f in detail["files"] if f["path"] == "knowledge/vanished.md"]
    assert vanished and vanished[0]["not_scanned_because"].startswith("changed, removed")


def test_a_file_being_written_during_the_backup_is_left_out(monkeypatch, install, tmp_path):
    target = os.path.realpath(os.path.join(install["profile"], "memories", "USER.md"))
    real_fstat, calls = os.fstat, {"n": 0}

    def fstat_of_a_growing_file(fd):
        st = real_fstat(fd)
        if os.path.realpath("/proc/self/fd/%d" % fd) == target:
            calls["n"] += 1
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                   st.st_size + calls["n"], st.st_atime, st.st_mtime, st.st_ctime))
        return st
    monkeypatch.setattr(kit.os, "fstat", fstat_of_a_growing_file)
    assert run_with_passphrase(monkeypatch, ["backup", "--profile", install["profile"], "--content",
                                             install["content"], "--out", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "backup-report.json").read_text())
    assert report["left_out"]["profile: changing, removed or unreadable during the backup"] == 1
    listing = json.loads((tmp_path / "backup-files.json").read_text())
    assert "profile/memories/USER.md" not in {e["path"] for e in listing["files"]}


def test_an_unverified_backup_leaves_no_archive(monkeypatch, install, tmp_path):
    monkeypatch.setattr(kit, "verify_stream", lambda *_a: {"files": 0, "problems": ["simulated"], "created_at": None})
    assert run_with_passphrase(monkeypatch, ["backup", "--profile", install["profile"], "--content",
                                             install["content"], "--out", str(tmp_path)]) == 1
    assert os.listdir(tmp_path) == []


def test_a_failed_restore_leaves_the_folder_empty(monkeypatch, backup, tmp_path):
    raw = bytearray(open(backup["archive"], "rb").read())
    raw[len(raw) * 3 // 4] ^= 1
    damaged = tmp_path / "damaged.ccjb"
    damaged.write_bytes(bytes(raw))
    target = tmp_path / "restore"
    target.mkdir()
    assert run_with_passphrase(monkeypatch, ["verify", "--archive", str(damaged), "--extract", str(target)]) == 1
    assert os.listdir(target) == []
    assert run_with_passphrase(monkeypatch, ["verify", "--archive", backup["archive"], "--extract", str(target)]) == 0
    assert (target / "profile" / "SOUL.md").exists()
    assert not any(n.startswith(".incomplete") for n in os.listdir(target))


KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7plantedkeymaterial"


@pytest.mark.parametrize("text", [
    "ssh:\n  private_key: |\n    -----BEGIN OPENSSH PRIVATE KEY-----\n    %s\n    -----END OPENSSH PRIVATE KEY-----\n",
    "- deploy key:\n  ```\n  -----BEGIN RSA PRIVATE KEY-----\n  %s\n  -----END RSA PRIVATE KEY-----\n  ```\n",
    'def key():\n    return """-----BEGIN PRIVATE KEY-----\n    %s\n    -----END PRIVATE KEY-----"""\n',
    '{"private_key": "-----BEGIN PRIVATE KEY-----\\r\\n%s\\r\\n-----END PRIVATE KEY-----\\r\\n"}',
    "> -----BEGIN PRIVATE KEY-----\n> %s\n> -----END PRIVATE KEY-----\n"])
def test_private_keys_are_redacted_however_they_are_indented_or_escaped(text):
    redacted, n = kit.redact(text % KEY_BODY)
    assert n >= 1 and KEY_BODY not in redacted


@pytest.mark.parametrize("line", [
    'headers = {"Authorization": settings.POSTIZ_API_KEY_VALUE, "Accept": "application/json"}',
    "const res = await fetch(url, { headers: { Authorization: config.postiz.apiKeyValue } });",
    '[Environment]::SetEnvironmentVariable("TELEGRAM_BOT_TOKEN", $Token, "User")',
    '[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", $env:OPENAI_API_KEY)'])
def test_code_expressions_in_programs_are_left_alone(line):
    assert kit.redact(line, "code") == (line, 0)


def test_a_literal_key_in_a_program_is_still_redacted():
    redacted, n = kit.redact('headers = {"Authorization": "pk_live_abcdefghijklmnopqrstuv123"}', "code")
    assert n == 1 and "pk_live_" not in redacted


def test_json_yaml_and_shell_stay_valid_after_redaction(tmp_path):
    import subprocess
    for line in ('{"date": "2026-10-01", "api_key": null}', '{"step": "summary", "has_token": true}',
                 '{"user": "a", "password": "Hunter2pass!", "port": 587}'):
        json.loads(kit.redact(line, "text")[0])
    flow = "- {name: smtp, password: Hunter2pass, port: 587}\n"
    redacted, n = kit.redact(flow, "config")
    assert n == 1 and redacted.endswith(", port: 587}\n")
    script = ('#!/bin/sh\n: "${POSTIZ_API_KEY:?not set}"\nTOKEN=$(curl -s https://x | jq -r .token)\n'
              'if [ -n "$1" ]; then token=${1}; fi\nTOKEN=${TELEGRAM_BOT_TOKEN:-}\nexport WP_PASSWORD=Hunter2Hunter2\n')
    redacted, n = kit.redact(script, "shell")
    assert n == 1 and "Hunter2Hunter2" not in redacted
    path = tmp_path / "s.sh"
    path.write_text(redacted)
    assert subprocess.run(["bash", "-n", str(path)]).returncode == 0


@pytest.mark.parametrize("line", [
    "Password: never send it by email; Sarah uses the reset link.", "**Token:** budget each run at 50k tokens",
    "Lint pass: clean", "Our secret: consistency beats intensity.", "## Password: reset flow",
    "- token: refresh daily via cron"])
def test_ordinary_prose_is_left_alone(line):
    assert kit.redact(line + "\n") == (line + "\n", 0)


def test_a_secret_in_a_markdown_code_span_keeps_the_markup():
    redacted, n = kit.redact("Set `password: hunter22` in the config.\n")
    assert n == 1 and redacted == "Set `password: [REDACTED:credential_assignment]` in the config.\n"


def test_long_lines_are_redacted_quickly():
    import time
    record = json.dumps({"log": "\n".join("test_%05d pass: ok" % i for i in range(8000))})
    start = time.monotonic()
    kit.redact(record)
    kit.count(kit.SECRET_PATTERNS, record)
    assert time.monotonic() - start < 3


def test_base64_data_is_not_taken_for_keys():
    import base64
    blob = base64.b64encode(os.urandom(3000) + b"EAA" + bytes(range(48, 58)) * 8 + os.urandom(3000)).decode()
    blob = blob[:4000] + "EAA" + "Abc123" * 12 + "+/" + blob[4000:]
    text = '<style>@font-face { src: url(data:font/ttf;base64,%s) }</style>' % blob
    assert kit.redact(text) == (text, 0)


def test_no_template_of_jarvis_is_taken_for_a_secret():
    templates = kit.load_templates()
    for path, revisions in templates["files"].items():
        for info in revisions.values():
            if isinstance(info, dict) and "text" in info:
                kind = kit.file_kind(path)
                assert kit.redact(info["text"], kind)[1] == 0 or path.endswith(("playbooks/postiz.md",)), path


@pytest.mark.parametrize("line", ["SECRET_KEY=django-insecure-abc123xyz", "API_SERVER_KEY=Abc123Def456",
                                  "ENCRYPTION_KEY=q3T9vL2aBcDeFgHiJkLmNo", "auth_key: Xy7-really-secret"])
def test_snake_case_key_names_are_redacted(line):
    redacted, n = kit.redact(line + "\n", "config")
    assert n == 1 and line.split("=")[-1].split(": ")[-1] not in redacted


def test_a_restore_that_cannot_move_its_files_leaves_nothing(monkeypatch, backup, tmp_path):
    real_replace, calls = os.replace, {"n": 0}

    def failing_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated")
        return real_replace(src, dst)
    monkeypatch.setattr(kit.os, "replace", failing_replace)
    assert run_with_passphrase(monkeypatch, ["verify", "--archive", backup["archive"], "--extract", str(tmp_path)]) == 1
    assert os.listdir(tmp_path) == []


def test_a_stopped_restore_can_be_run_again(monkeypatch, backup, tmp_path):
    (tmp_path / ".incomplete-deadbeef" / "profile").mkdir(parents=True)
    assert run_with_passphrase(monkeypatch, ["verify", "--archive", backup["archive"], "--extract", str(tmp_path)]) == 0
    assert sorted(os.listdir(tmp_path)) == ["backup-files.json", "content", "profile"]


def test_a_table_that_cannot_be_opened_is_reported(tmp_path):
    db = str(tmp_path / "odd.db")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp)")
    con.executemany("INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
                    [("user", "hi", 1782864000.5), ("assistant", "yes", "2026-08-02T10:00:00"), ("user", "x", None)])
    con.execute("PRAGMA writable_schema = ON")
    con.execute("INSERT INTO sqlite_master VALUES ('table', 'weird', 'weird', 0, "
                "'CREATE VIRTUAL TABLE weird USING nosuchmodule()')")
    con.commit()
    con.close()
    report = kit.analyze_database(db, 10, False, False)
    assert "could not be opened" in report["tables"]["weird"]["error"]
    times = report["tables"]["messages"]["time"]["timestamp"]
    assert times["first"] == "2026-07-01" and times["last"] == "2026-08-02", "numbers and text in one time column"
