"""Builds a grid of session lineage shapes with the vendored Hermes's own SessionDB and records Hermes's own fork test.

Usage (an isolated subprocess, so Hermes's top-level modules never mix with the Command Center's):
    python -I hermes_lineage_driver.py <HERMES_SRC> <out.json>

Every shape is written with SessionDB.create_session / append_message / end_session into one scratch state.db; for
each child session the driver records SessionDB._is_explicit_fork_child_row(session, include_reset=True), Hermes's
reading of whether a fork marker (_branched_from, _delegate_from, _reset_from) makes the child a fork of its parent
("markers only count when they point at parent_session_id"; with no parent any marker counts). The test
(test_hermes_lineage.py) reads the same file with app.brain.source.Backup. Round 3 fix A (source.FORK_MARKERS,
source._markers); rebuild R7. Third-party modules Hermes imports at module level and this path never calls are inert
stubs, as in hermes_driver.py; nothing is installed. Synthetic data only.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import itertools
import json
import os
import sys
import tempfile
import types
import warnings
from pathlib import Path
from typing import Any

SRC, OUT = sys.argv[1], sys.argv[2]
sys.path.insert(0, SRC)
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes_home_")
warnings.filterwarnings("ignore")
STUB_ROOTS = {"ruamel", "rich"}


class _Stub(types.ModuleType):
    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        value = type(name, (), {"__init__": lambda self, *a, **k: None, "__call__": lambda self, *a, **k: None})
        setattr(self, name, value)
        return value


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):  # noqa: ANN001
        if name.split(".")[0] in STUB_ROOTS:
            return importlib.machinery.ModuleSpec(name, self, is_package=True)
        return None

    def create_module(self, spec):  # noqa: ANN001
        module = _Stub(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):  # noqa: ANN001
        pass


sys.meta_path.append(_StubFinder())

import hermes_state  # noqa: E402

T = 1_795_000_000.0
FORK = ("_branched_from", "_delegate_from", "_reset_from")
PARENT_ENDS = ("completed", "compression", "branched", "new_session", "")  # "" : still open (reopened)
PARENT_OWN = ({}, {"_delegate_from": "G"}, {"_branched_from": "G"}, {"_reset_from": "G"})
# what the child's fork marker names: its parent, the grandparent G (copied when the parent's own config names G,
# otherwise another conversation), or a third conversation O
TARGETS = ("parent", "G", "O")


def _turn(db: Any, sid: str, t: float) -> None:
    db.append_message(sid, "user", "Please check the travel plan for %s." % sid, timestamp=t)
    db.append_message(sid, "assistant", "The travel plan for %s is checked." % sid, timestamp=t + 1)


def main() -> dict[str, Any]:
    d = tempfile.mkdtemp(prefix="hermes_lineage_")
    path = os.path.join(d, "state.db")
    db = hermes_state.SessionDB(Path(path))
    t = T
    for sid in ("G", "O"):
        db.create_session(sid, "cli", model="m")
        _turn(db, sid, t)
        t += 10
    shapes: list[dict[str, Any]] = []
    n = 0
    for end, own, marker, target, compressed in itertools.product(PARENT_ENDS, PARENT_OWN, FORK, TARGETS,
                                                                 (False, True)):
        n += 1
        parent, child = "p%03d" % n, "c%03d" % n
        db.create_session(parent, "cli", model="m", parent_session_id="G" if own else None,
                          model_config=dict(own) or None)
        _turn(db, parent, t)
        if end:
            db.end_session(parent, end)
        config = {marker: parent if target == "parent" else target}
        if compressed:
            config["_compressed_from"] = parent
        db.create_session(child, "cli", model="m", parent_session_id=parent, model_config=config)
        _turn(db, child, t + 5)
        t += 10
        shapes.append({"child": child, "parent": parent, "parent_end": end, "parent_own": own, "marker": marker,
                       "target": target, "compressed": compressed})
    # no parent (Hermes deleted it, or a v16 orphan tag): any marker counts
    for marker, target in itertools.product(FORK, ("G", "__orphaned__")):
        n += 1
        child = "c%03d" % n
        db.create_session(child, "cli", model="m", model_config={marker: target})
        _turn(db, child, t)
        t += 10
        shapes.append({"child": child, "parent": None, "parent_end": None, "parent_own": None, "marker": marker,
                       "target": target, "compressed": False})
    for shape in shapes:
        shape["hermes_fork"] = bool(db._is_explicit_fork_child_row(db.get_session(shape["child"]),
                                                                    include_reset=True))
        shape["hermes_parent"] = db.get_session(shape["child"]).get("parent_session_id")
    db.close()
    return {"db": path, "shapes": shapes}


try:
    RESULT = main()
except Exception as exc:  # reported, never hidden: the test fails with it
    import traceback
    RESULT = {"error": "%s: %s" % (type(exc).__name__, exc), "trace": traceback.format_exc()[-2000:]}

with open(OUT, "w") as f:
    json.dump(RESULT, f, default=str)
