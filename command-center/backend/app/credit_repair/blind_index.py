"""The Credit Repair match index: lets the Research Gateway refuse a web search that names a client, without the
gateway ever seeing a client's data.

For each client the index (cr_research_index, in the Credit Repair database) holds HMAC-SHA256 values of: the pairs
of words of the full name that name the person (canon.name_pairs, as for a lead) and the full name itself, also written
as one word ("SachaCoe"), each also with an apostrophe inside a word dropped ("O'Brien" as "brien" and "obrien"), the
e-mail address and the phone number's digits (also its last seven, the local number). The words are read as the
research screen reads a lead's name (the same common words left out), a query's pairs are any two of its words however
far apart, and its numbers are read without CVE ids, so a Credit Repair name and a lead's are refused alike. The CR-0001
reference is not indexed: the screen refuses every reference shape, whether or not the client exists. The key is
derived from CREDIT_REPAIR_DATA_KEY with HKDF (label "research-index"), so it is neither the field-encryption key nor
stored anywhere. The gateway hands over the same HMAC values of the parts of its query, and matches() answers True or
False: nothing is decrypted for it and nothing else leaves this module.

The index is filled when a client is created or changed (app/api/credit_repair.py), and rebuilt as a whole for every
client at every start (rebuild(), run by store.migrate()): rows left by a restore of an older backup or by an older
version never outlive a restart. cr_research_index_meta records the version of the rows and a check value of the key
they were made with. matches() raises IndexIncomplete, and the gateway refuses the search, when a client has no rows,
when the version differs, or when CREDIT_REPAIR_DATA_KEY is not the key the rows were made with (a regenerated .env, a
restore paired with another .env): the old rows would quietly match nothing, and they cannot be remade without the
old key. It also raises IndexIncomplete when the rebuild at this start failed for any other reason (REBUILD_FAILED):
rows left from before may be stale (a client renamed under 0.6.x keeps its old rows), so nothing is checked against
them until a rebuild succeeds.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re

from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, exists, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..research.canon import (
    STOP_WORDS,
    fold,
    join_spelled,
    name_pairs,
    phone_numbers,
    query_runs,
    spelled_out,
    tokens,
    unapostrophe,
)
from . import store

log = logging.getLogger(__name__)

HKDF_LABEL = b"research-index"
KINDS = ("name_pair", "name_joined", "full_name", "email", "phone", "reference")  # "reference": no rows since version 2
# The rows' version: a change in what they hold (a new kind, another normal form) makes matches() refuse until the
# next start has rebuilt them. 2: apostrophe forms, the screen's common words left out, no reference rows. 3: no pair of
# a name particle with another word when the name has two other words (canon.name_pairs).
INDEX_VERSION = 3
LOCK_ID = 7_314_002_213  # pg_advisory_xact_lock: one rebuild at a time (the API and the worker both start one)
MAX_NAME_WORDS = 6
MIN_JOINED = 6  # a name written as one word is compared from this length ("sachacoe"), as in the research screen
EMAIL = re.compile(r"[a-z0-9._%+\-]{1,64}@[a-z0-9.\-]{1,253}\.[a-z]{2,24}")
# Set by store.migrate() when the rebuild at this start failed; a successful rebuild clears it.
REBUILD_FAILED = False


class IndexIncomplete(RuntimeError):
    """A client has no index rows: a search cannot be checked against them."""


def index_key(settings: Settings) -> bytes:
    raw = settings.credit_repair_data_key.get_secret_value() if settings.credit_repair_data_key else ""
    if not raw:
        raise store.NotConfigured("CREDIT_REPAIR_DATA_KEY is not set")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_LABEL).derive(raw.encode("utf-8"))


def mac(key: bytes, kind: str, value: str) -> str:
    return hmac.new(key, f"{kind}\x1f{value}".encode(), hashlib.sha256).hexdigest()


def key_check(key: bytes) -> str:
    """A value that tells whether the rows were made with this key, and nothing about the key."""
    return mac(key, "check", "research-index")


def name_forms(text: str) -> list[list[str]]:
    """A name's words as the research screen reads a query: look-alikes folded, lower case, initials and common words
    (titles, "and", "LLC") left out; once as written and once with apostrophes inside words dropped ("O'Brien")."""
    folded = fold(text)
    forms = dict.fromkeys((folded, unapostrophe(folded)))
    return [[w for w in tokens(form) if len(w) > 1 and w not in STOP_WORDS] for form in forms]


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text or "")


def client_values(full_name: str, email: str, phone: str) -> set[tuple[str, str]]:
    """(kind, normalized value) for one client."""
    values: set[tuple[str, str]] = set()
    for form in name_forms(full_name):
        words = list(dict.fromkeys(form))[:MAX_NAME_WORDS]
        if len(words) >= 2:
            values.add(("full_name", " ".join(words)))
            pairs = name_pairs(words)  # "van der", or "de" with "cruz" in "Maria de la Cruz", is no name
            values.update(("name_pair", " ".join(sorted(pair))) for pair in pairs)
            joined = {"".join(words), *("".join(pair) for pair in pairs)}
            values.update(("name_joined", j) for j in joined if len(j) >= MIN_JOINED)
    if email.strip():
        values.add(("email", fold(email).strip()))
    values.update(("phone", n) for n in phone_numbers(_digits(phone)))  # also the local number
    return values


def query_candidates(text: str) -> set[tuple[str, str]]:
    """(kind, normalized value) for the parts of a query that could name a client: its words as typed and spelled
    together ("d a n a"), with and without inner apostrophes, and its numbers without CVE ids."""
    skel = fold(text)
    values: set[tuple[str, str]] = set()
    for words in name_forms(skel) + name_forms(join_spelled(skel)):
        for i in range(len(words)):
            for j in range(i + 1, len(words)):  # any two words, however far apart ("Dana Marie Ann Whitfield")
                if words[i] != words[j]:
                    values.add(("name_pair", " ".join(sorted((words[i], words[j])))))
            for n in range(2, MAX_NAME_WORDS + 1):
                if i + n <= len(words):
                    values.add(("full_name", " ".join(words[i:i + n])))
        values.update(("name_joined", w) for w in words if len(w) >= MIN_JOINED)  # "SachaCoe"
    for view in (skel, spelled_out(skel)):
        values.update(("email", m.group(0)) for m in EMAIL.finditer(view))
    for run in query_runs(text):  # CVE ids aside, as the screen reads a lead's number
        values.update(("phone", n) for n in phone_numbers(run))
    return values


async def index_client(s: AsyncSession, settings: Settings, cipher: store.Cipher, client: store.CRClient) -> int:
    """Replaces one client's index rows from its current (decrypted) fields; in the caller's transaction."""
    key = index_key(settings)
    values = client_values(cipher.dec(client.full_name_enc), cipher.dec(client.email_enc), cipher.dec(client.phone_enc))
    await s.execute(delete(store.CRResearchIndex).where(store.CRResearchIndex.client_id == client.id))
    rows = [{"client_id": client.id, "kind": kind, "mac": mac(key, kind, value)} for kind, value in sorted(values)]
    if rows:
        await s.execute(pg_insert(store.CRResearchIndex).values(rows).on_conflict_do_nothing())
    return len(rows)


def _unindexed():
    return ~exists().where(store.CRResearchIndex.client_id == store.CRClient.id)


async def rebuild(settings: Settings) -> int:
    """Replaces every row of the index from every client's current fields, in one transaction, and records the version
    and the key check. Returns how many clients were indexed, or -1 when the index was left as it was: the clients'
    data cannot be decrypted with this key (it changed, or a restore came with another .env). matches() then keeps
    refusing; only the code is logged. Any other error is raised: store.migrate() logs its class, sets REBUILD_FAILED
    (every search is refused until a rebuild succeeds) and lets the Command Center start."""
    global REBUILD_FAILED
    key = index_key(settings)
    check = key_check(key)
    cipher = store.Cipher(settings)
    try:
        async with store.session(settings) as s:
            await s.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
            meta = await s.get(store.CRResearchIndexMeta, 1, with_for_update=True)
            clients = (await s.scalars(select(store.CRClient).order_by(store.CRClient.id))).all()
            if clients and meta is not None and not hmac.compare_digest(meta.key_check, check):
                log.warning("research match index not rebuilt: key_changed")
                return -1
            await s.execute(delete(store.CRResearchIndex))  # also the rows of clients that are gone
            for client in clients:
                await index_client(s, settings, cipher, client)
            stmt = pg_insert(store.CRResearchIndexMeta).values(id=1, key_check=check, version=INDEX_VERSION)
            await s.execute(stmt.on_conflict_do_update(
                index_elements=[store.CRResearchIndexMeta.id],
                set_={"key_check": check, "version": INDEX_VERSION, "built_at": func.now()}))
            if clients:  # every client's name, e-mail and phone were decrypted for it
                await store.log_access(s, "system:research-index", "research_index_rebuild", count=len(clients))
    except InvalidToken:
        log.warning("research match index not rebuilt: key_mismatch")
        return -1
    REBUILD_FAILED = False
    return len(clients)


async def matches(settings: Settings, candidates: set[tuple[str, str]]) -> bool:
    """True when any candidate is a client's name, e-mail or phone. False without a Credit Repair store.

    Raises IndexIncomplete when the rows were not made with this key and version, a client has no rows or the rebuild
    at this start failed, and any database error as it is: the gateway refuses the search in every one of these cases.
    """
    if not store.configured(settings):
        return False
    if REBUILD_FAILED:
        raise IndexIncomplete("the research match index was not rebuilt at this start")
    key = index_key(settings)
    macs = sorted({mac(key, kind, value) for kind, value in candidates})
    async with store.session(settings) as s:
        meta = await s.get(store.CRResearchIndexMeta, 1)
        if meta is None or meta.version != INDEX_VERSION or not hmac.compare_digest(meta.key_check, key_check(key)):
            raise IndexIncomplete("the research match index was not built with this key and version")
        if await s.scalar(select(store.CRClient.id).where(_unindexed()).limit(1)) is not None:
            raise IndexIncomplete("a Credit Repair client is not in the research match index")
        if not macs:
            return False
        return bool(await s.scalar(select(exists().where(store.CRResearchIndex.mac.in_(macs)))))
