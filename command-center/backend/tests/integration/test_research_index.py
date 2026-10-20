"""The Credit Repair match index (app/credit_repair/blind_index.py): filled on create and change, rebuilt for every
client at every start, refusing every search while its rows were made with another key or version or after a failed
rebuild, answering only "match" or "no match", and the gateway refusing a search that names a client without saying it
is one: a client's name and a lead's read alike to everyone without the sensitive-data permission, and a person's
refusals for such names are limited and audited."""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app import bootstrap as startup
from app.credit_repair import blind_index, store
from app.db import session_scope
from app.models import AuditLog, Lead, ResearchQuery, Site, User
from app.research import intake
from app.research.gateway import Gateway

from ..research_fakes import (
    OPERATOR,
    RecordingMockProvider,
    add_people,
    as_,
    forbid_real_http,
    research_on,
    running_task,
)

pytestmark = pytest.mark.usefixtures("clean_db")


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    forbid_real_http(monkeypatch)

CLIENT = {"full_name": "Pat Q. Example", "email": "Pat.Example@Example.org", "phone": "(404) 555-0123",
          "address": "1 Test Way", "dob": "1990-01-01", "ssn_last4": "1234"}


async def new_client(admin, client: dict | None = None) -> dict:
    async with session_scope() as s:
        (await s.scalar(select(User).where(User.email == "admin@test.local"))).can_access_sensitive = True
    assert (await admin.post("/api/credit-repair/enable", json={"acknowledge": True})).status_code == 200
    r = await admin.post("/api/credit-repair/clients", json=client or CLIENT)
    assert r.status_code == 201, r.text
    return r.json()["client"]


async def index_rows(settings) -> list[tuple[int, str, str]]:
    async with store.session(settings) as s:
        return [tuple(r) for r in (await s.execute(text("SELECT client_id, kind, mac FROM cr_research_index"))).all()]


async def test_a_new_client_is_indexed_and_nothing_readable_is_stored(admin, settings):
    client = await new_client(admin)
    found = await index_rows(settings)
    kinds = sorted({kind for _, kind, _ in found})
    assert kinds == ["email", "full_name", "name_joined", "name_pair", "phone"]  # no reference: the screen refuses all
    assert {cid for cid, _, _ in found} == {client["id"]}
    dump = str(found).lower()
    for plain in ("pat", "example", "4045550123", "5550123", "555", "cr-0001", "0001"):
        assert plain not in dump
    key = blind_index.index_key(settings)
    assert key != settings.credit_repair_data_key.get_secret_value().encode()
    assert blind_index.mac(key, "phone", "5550123") in {m for _, _, m in found}


@pytest.mark.parametrize("query, hit", [
    ("Pat Example reviews", True),
    ("Example, Pat", True),
    ("pat q example", True),
    ("pat.example@example.org", True),
    ("pat example at example dot org", True),
    ("call 404 555 0123", True),
    ("CR-0001 status", False),  # a reference shape is refused by the screen (credit_topic), never by the index
    ("PatExample instagram", True),  # the name written as one word
    ("Pat Marie Ann Example", True),  # two words apart and more, as the screen reads a lead's name
    ("Example reviews in Atlanta Pat", True),
    ("p a t example", True),  # spelled letter by letter
    ("examplepat", True),
    ("404, 555, 0123", True),  # the number split by commas
    ("call 555-0123", True),  # the local number
    ("Pat Smith reviews", False),
    ("example pricing ideas", False),
    ("CR-0002", False),
    ("call 555-0124", False),
])
async def test_matches_answers_only_match_or_no_match(admin, settings, query, hit):
    await new_client(admin)
    assert await blind_index.matches(settings, blind_index.query_candidates(query)) is hit


async def test_clients_created_before_the_index_are_indexed_by_the_rebuild_at_every_start(settings):
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        s.add(store.CRClient(created_by="old", reference="CR-0001", full_name_enc=cipher.enc("Lee Older Client")))
    with pytest.raises(blind_index.IndexIncomplete):
        await blind_index.matches(settings, blind_index.query_candidates("anything at all"))
    await store.migrate(settings)
    rows = await index_rows(settings)
    await store.migrate(settings)  # twice: the same rows again
    assert sorted(await index_rows(settings)) == sorted(rows) and rows
    assert await blind_index.matches(settings, blind_index.query_candidates("Lee Older Client"))
    async with store.session(settings) as s:
        log = (await s.execute(select(store.CRAccessLog.action, store.CRAccessLog.detail))).all()
    assert [tuple(r) for r in log] == [("research_index_rebuild", {"count": 1})] * 2  # each decrypted the client


async def test_rows_a_restore_left_behind_are_replaced_at_the_next_start(admin, settings):
    client = await new_client(admin)  # Pat Q. Example, indexed
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:  # an older backup restored, then a 0.6.x client with the same id
        await s.execute(text("DELETE FROM cr_clients WHERE id = :id"), {"id": client["id"]})
        s.add(store.CRClient(id=client["id"], created_by="old", reference="CR-0009",
                             full_name_enc=cipher.enc("Bob Newcomer"), phone_enc=cipher.enc("(212) 555-0177")))
    assert await blind_index.matches(settings, blind_index.query_candidates("Pat Example"))  # stale until a start
    await store.migrate(settings)
    assert await blind_index.matches(settings, blind_index.query_candidates("Bob Newcomer reviews"))
    assert await blind_index.matches(settings, blind_index.query_candidates("call 555-0177"))
    assert not await blind_index.matches(settings, blind_index.query_candidates("Pat Example"))
    assert {cid for cid, _, _ in await index_rows(settings)} == {client["id"]}


async def test_rows_made_with_another_key_or_version_refuse_every_search(admin, settings):
    await new_client(admin)
    other = settings.model_copy(update={"credit_repair_data_key": SecretStr("another-credit-repair-key-0123456789")})
    with pytest.raises(blind_index.IndexIncomplete):  # a regenerated .env: the rows would quietly match nothing
        await blind_index.matches(other, blind_index.query_candidates("Pat Example"))
    await research_on("mock")
    mock = RecordingMockProvider()
    res = await Gateway(other, providers={"mock": mock}).search(agent="seo_content", task_id=await running_task(),
                                                                query="small business pricing ideas")
    assert (res.status, res.reason, mock.calls) == ("blocked", "credit_repair_index_incomplete", [])
    # A start with that key cannot decrypt the clients: it leaves the rows, and the refusals, as they are.
    before = await index_rows(settings)
    assert await blind_index.rebuild(other) == -1
    assert await index_rows(settings) == before
    with pytest.raises(blind_index.IndexIncomplete):
        await blind_index.matches(other, blind_index.query_candidates("Pat Example"))
    assert await blind_index.matches(settings, blind_index.query_candidates("Pat Example"))  # the right key still works

    async with store.session(settings) as s:  # rows of another version: refused until the next start rebuilds them
        await s.execute(text("UPDATE cr_research_index_meta SET version = version + 1"))
    with pytest.raises(blind_index.IndexIncomplete):
        await blind_index.matches(settings, blind_index.query_candidates("Pat Example"))
    await store.migrate(settings)
    assert await blind_index.matches(settings, blind_index.query_candidates("Pat Example"))


async def test_changing_a_client_rebuilds_its_rows(admin, settings):
    client = await new_client(admin)
    before = await index_rows(settings)
    assert (await admin.patch(f"/api/credit-repair/clients/{client['id']}", json={"status": "active"})).status_code == 200
    assert sorted(await index_rows(settings)) == sorted(before)


async def test_the_gateway_refuses_a_search_that_names_a_client(admin, settings):
    await new_client(admin)
    await research_on("mock")
    mock = RecordingMockProvider()
    gw = Gateway(settings, providers={"mock": mock})
    res = await gw.search(agent="seo_content", task_id=await running_task(), query="Pat Example reviews")
    # The same code as a lead's or a ticket's name: a refusal never says that someone is a Credit Repair client.
    assert (res.status, res.reason, mock.calls) == ("blocked", "personal_record", [])
    async with session_scope() as s:
        row = await s.scalar(select(ResearchQuery))
    assert row.blocked_by_ref is None and row.query_text is None  # no client id or text leaves the store
    res = await gw.search(agent="seo_content", task_id=await running_task(), query="small business pricing ideas")
    assert res.status == "ok"


async def test_an_incomplete_index_refuses_every_search(settings):
    cipher = store.Cipher(settings)
    async with store.session(settings) as s:
        s.add(store.CRClient(created_by="old", reference="CR-0001", full_name_enc=cipher.enc("Lee Older Client")))
    await research_on("mock")
    mock = RecordingMockProvider()
    res = await Gateway(settings, providers={"mock": mock}).search(agent="seo_content", task_id=await running_task(),
                                                                   query="small business pricing ideas")
    assert (res.status, res.reason, mock.calls) == ("blocked", "credit_repair_index_incomplete", [])
    async with store.session(settings) as s:
        assert await s.scalar(select(func.count()).select_from(store.CRResearchIndex)) == 0


async def test_an_operator_sees_a_credit_repair_match_as_any_personal_record_and_its_refusals_are_limited(admin,
                                                                                                         settings):
    await new_client(admin)
    await add_people()
    await research_on("mock", settings_values={"person_daily_cap": 3})
    client = await as_(admin, "operator")  # an operator: no sensitive-data permission
    run = {"capability": "research_topic", "params": {"query": "Pat Example reviews"}}
    for query in ("Pat Example reviews", "PatExample", "Example, Pat"):
        r = await client.post("/api/agents/seo_content/tasks", json={**run, "params": {"query": query}})
        assert r.status_code == 400 and "(personal_record)" in r.json()["detail"]
        assert "credit" not in r.json()["detail"].lower()
    # Refusals are limited like searches: after the daily cap, nothing more of this person's is screened today.
    r = await client.post("/api/agents/seo_content/tasks", json={**run, "params": {"query": "small business ideas"}})
    assert r.status_code == 400 and "(too_many_blocked)" in r.json()["detail"]
    blocked = (await client.get("/api/research/blocked")).json()["blocked"]
    assert [b["reason_code"] for b in blocked] == ["personal_record"] * 3
    assert all(b["blocked_by_ref"] is None for b in blocked)
    async with session_scope() as s:
        last = await s.scalar(select(ResearchQuery).order_by(ResearchQuery.id.desc()).limit(1))
    assert (last.status, last.reason_code, last.query_text) == ("refused_budget", "too_many_blocked", None)
    # Someone else's searches are screened as before.
    client = await as_(admin, "admin")
    r = await client.post("/api/agents/seo_content/tasks", json={**run, "params": {"query": "small business ideas"}})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("query, hit", [
    ("Dana OBrien", True),  # the apostrophe dropped, kept or typographic
    ("Dana Obrien", True),
    ("OBrien Dana", True),
    ("Dana O'Brien", True),
    ("Dana O\u2019Brien", True),
    ("Dana Marie Ann OBrien", True),
    ("Brien Smith", False),
    ("Dana Scully", False),
])
async def test_a_clients_name_with_an_apostrophe_matches_however_it_is_typed(admin, settings, query, hit):
    await new_client(admin, {**CLIENT, "full_name": "Dana O'Brien"})
    assert await blind_index.matches(settings, blind_index.query_candidates(query)) is hit


async def test_a_lead_and_a_credit_repair_client_read_alike_to_everyone_without_the_sensitive_permission(
        admin, settings, monkeypatch):
    await new_client(admin)  # Pat Q. Example, (404) 555-0123; the admin got the sensitive-data permission for it
    async with session_scope() as s:
        lead = Lead(name="Sacha Coe", phone="(678) 555-0199")
        s.add_all([lead, Site(name="Bright Smiles Dental", url="https://brightsmiles.com", client="BS LLC")])
        await s.flush()
        lead_id = lead.id
    await add_people()
    await research_on("mock")
    monkeypatch.setattr(intake, "PERSONAL_RECORD_DAILY", 1000)  # this test refuses many names on purpose
    looked_up: list[bool] = []
    real_matches = blind_index.matches

    async def counted(settings, candidates):
        looked_up.append(await real_matches(settings, candidates))
        return looked_up[-1]

    monkeypatch.setattr(blind_index, "matches", counted)
    # (agent, a lead's query, a client's query): whatever else the query holds, the same answer.
    pairs = [("supervisor", "Sacha Coe", "Pat Example"),
             ("supervisor", "Sacha Coe localhost", "Pat Example localhost"),  # internal_host comes later
             ("seo_content", "Sacha Coe brightsmiles.com", "Pat Example brightsmiles.com"),  # client_name too
             ("seo_content", "Sacha Coe WordPress 6.9", "Pat Example WordPress 6.9"),  # software_version too
             ("supervisor", "Sacha Marie Coe", "Pat Marie Example"),
             ("supervisor", "555 0199", "555 0123"),  # a bare local number
             ("supervisor", "CR-0001 status", "CR-0002 status")]  # a reference, existing or not
    capability = {"supervisor": "web_search", "seo_content": "research_topic"}

    async def run(client, agent: str, query: str):
        r = await client.post(f"/api/agents/{agent}/tasks", json={"capability": capability[agent],
                                                                   "params": {"query": query}})
        return r.status_code, r.json().get("detail")

    async with session_scope() as s:  # the admin, without the permission for now
        (await s.scalar(select(User).where(User.email == "admin@test.local"))).can_access_sensitive = False
    for who in ("operator", "admin"):
        client = await as_(admin, who)
        for agent, lead_query, client_query in pairs:
            looked_up.clear()
            lead_answer, client_answer = await run(client, agent, lead_query), await run(client, agent, client_query)
            assert lead_answer == client_answer and lead_answer[0] == 400, (who, lead_query, client_query)
            assert len(looked_up) == 2  # the index is asked for a lead's name too, at the same point
        assert "(personal_record)" in (await run(client, "supervisor", "Sacha Coe"))[1]
        assert "(credit_topic)" in (await run(client, "supervisor", "CR-0002 status"))[1]
        assert (await run(client, "supervisor", "CR2032 battery life"))[0] == 201  # a battery is no reference
        blocked = (await client.get("/api/research/blocked", params={"limit": 500})).json()["blocked"]
        listed = (await client.get("/api/research/queries", params={"status": "blocked", "limit": 200})).json()
        assert {b["blocked_by_ref"] for b in blocked} == {None}
        assert {q["blocked_by_ref"] for q in listed["queries"]} == {None}
    async with session_scope() as s:
        refs = set((await s.scalars(select(ResearchQuery.blocked_by_ref))).all())
    assert refs == {None, f"lead:{lead_id}"}  # recorded, for those who may see it
    async with session_scope() as s:
        (await s.scalar(select(User).where(User.email == "admin@test.local"))).can_access_sensitive = True
    blocked = (await admin.get("/api/research/blocked", params={"limit": 500})).json()["blocked"]
    assert {b["blocked_by_ref"] for b in blocked} == {None, f"lead:{lead_id}"}


async def test_a_persons_personal_record_refusals_are_limited_whatever_the_search_cap_and_audited(app_client,
                                                                                                    settings):
    await add_people()
    async with session_scope() as s:
        s.add(Lead(name="Sacha Coe"))
    await research_on("mock", settings_values={"person_daily_cap": 1000})  # a raised search cap changes nothing
    client = await as_(app_client, "operator")

    async def run(query: str):
        return await client.post("/api/agents/seo_content/tasks",
                                 json={"capability": "research_topic", "params": {"query": query}})

    for _ in range(intake.PERSONAL_RECORD_DAILY):
        r = await run("Sacha Coe reviews")
        assert r.status_code == 400 and "(personal_record)" in r.json()["detail"]
    for query in ("small business ideas", "Sacha Coe reviews", "content marketing ideas"):
        r = await run(query)
        assert r.status_code == 400 and "(too_many_blocked)" in r.json()["detail"]
    # The tasks that person queued earlier are refused when they run, too.
    mock = RecordingMockProvider()
    res = await Gateway(settings, providers={"mock": mock}).search(
        agent="seo_content", task_id=await running_task(created_by=OPERATOR), query="small business ideas")
    assert (res.status, res.reason, mock.calls) == ("refused_budget", "too_many_blocked", [])
    async with session_scope() as s:
        entries = (await s.scalars(select(AuditLog).where(AuditLog.action == intake.LIMIT_ACTION))).all()
    assert [(e.actor, e.target_id, e.detail) for e in entries] == [
        ("system:research", OPERATOR, {"person": OPERATOR, "count": intake.PERSONAL_RECORD_DAILY})]  # once a day
    # Anyone else is screened as before.
    client = await as_(app_client, "admin")
    assert (await run("small business ideas")).status_code == 201


async def test_a_failing_rebuild_never_stops_the_start_and_refuses_every_search(admin, settings, monkeypatch,
                                                                                caplog):
    await new_client(admin)
    real_index_client = blind_index.index_client

    async def broken(*args, **kwargs):
        raise IntegrityError("INSERT INTO cr_research_index", {}, Exception("Failing row contains (Pat Example)"))

    async def no_migrations():
        return []

    monkeypatch.setattr(blind_index, "index_client", broken)
    monkeypatch.setattr(startup, "run_migrations", no_migrations)
    caplog.set_level(logging.INFO)
    await research_on("mock")
    mock = RecordingMockProvider()
    gw = Gateway(settings, providers={"mock": mock})
    try:
        await startup.bootstrap(settings)  # completes: the API and the worker start
        assert "research match index not rebuilt: IntegrityError" in caplog.text and "Pat" not in caplog.text
        # The old rows may be stale (a client renamed under 0.6.x): nothing is checked against them.
        res = await gw.search(agent="seo_content", task_id=await running_task(), query="small business pricing ideas")
        assert (res.status, res.reason, mock.calls) == ("blocked", "credit_repair_index_incomplete", [])
        monkeypatch.setattr(blind_index, "index_client", real_index_client)
        await store.migrate(settings)  # the next start rebuilds it
        res = await gw.search(agent="seo_content", task_id=await running_task(), query="small business pricing ideas")
        assert res.status == "ok" and len(mock.calls) == 1
    finally:
        blind_index.REBUILD_FAILED = False
