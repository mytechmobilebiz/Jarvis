"""Generates tool-result summary lines with the vendored Hermes's own code (rebuild S5: a stand-in for the round-3
hermes_lean_gen.py, lost with the container).

Usage (an isolated subprocess, so Hermes's top-level modules never mix with the Command Center's):
    python -I hermes_lean_gen.py <HERMES_SRC> <out.json>

For every withheld-looking text W of a small synthetic set (apostrophes, double quotes, line breaks, non-ASCII,
backslashes, a long text) and for every tool shape that puts a call's argument into its one-line summary, the driver
calls Hermes's agent.context_compressor._summarize_tool_result(tool_name, tool_args, tool_content), the line Hermes's
compression writes in place of an old tool result: the quoted f-string summaries (vision_analyze, web_search,
delegate_task, search_files: N1), the code and command previews (execute_code, terminal), and the generic summary
of any other tool, which cuts str(value) at 40 characters (a repr of a dict or list cut inside an escape: N4), and
the <persisted-output> preview tools/tool_result_storage.maybe_persist_tool_result writes for an oversized result
(cut inside an escape of a JSON or repr text, or at a line break inside W).
The same shapes with unrelated arguments are the controls. Third-party modules Hermes imports at module level and
this path never calls are inert stubs, as in hermes_driver.py; nothing is installed. Synthetic data only."""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import json
import os
import sys
import tempfile
import traceback
import types
import warnings
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

WS = {
    "apostrophes": "Anna's code for the archive door is Möwe-5512;\nshe said don't tell Ben about it.",
    "quotes": 'Er sagte: "Der Tresor öffnet mit Kranich-7741"\nund dann ging er nach Hause, ohne Licht.',
    "long": ("Der Notfallschlüssel für das Archiv liegt hinter der dritten Kachel links, Kombination Möwe-5512, "
             "das bleibt bitte unter uns."),
    "backslashes": "Path C:\\Users\\anna\\vault\\key.txt holds the backup phrase lantern-orbit-71 for the safe.",
}
CONTROLS = {
    "control_plain": "Please book the train to Hamburg for Tuesday morning and send me the ticket.",
    "control_quoted": "What's the weather like in Lisbon this weekend?\nI'd like to go hiking.",
}


def shapes(w: str) -> list[tuple[str, str, dict[str, Any], str]]:
    """(shape, tool, args, tool result) for one text: every summary shape that quotes or previews an argument."""
    return [
        ("vision_question", "vision_analyze", {"image_url": "https://example.invalid/a.png", "question": w}, "A photo."),
        ("web_search_query", "web_search", {"query": w}, json.dumps({"success": True, "data": {"web": []}})),
        ("delegate_goal", "delegate_task", {"goal": w, "context": "none"}, "The sub-agent finished."),
        ("search_pattern", "search_files", {"pattern": w, "path": "notes", "target": "content"},
         json.dumps({"total_count": 0, "matches": []})),
        ("terminal_echo", "terminal", {"command": "echo %s >> notes.txt" % json.dumps(w)},
         json.dumps({"output": "", "exit_code": 0})),
        ("code_repr", "execute_code", {"code": "msg = %r\nsend(msg)\n" % w}, "sent"),
        ("code_ascii", "execute_code", {"code": "msg = %s\nprint(msg)\n" % ascii(w)}, "printed"),
        ("generic_text", "send_message", {"message": w, "target": "telegram"}, json.dumps({"success": True})),
        ("generic_dict", "send_message", {"payload": {"text": w}, "target": "telegram"}, json.dumps({"success": True})),
        ("generic_list", "send_message", {"lines": [w], "target": "telegram"}, json.dumps({"success": True})),
        ("generic_failed", "send_message", {"message": w}, json.dumps({"success": False, "error": "timeout"})),
    ]


FILLER = "Step %03d of the export finished without a warning.\n"


def persisted(trs: Any, w: str, size: int) -> list[tuple[str, str]]:
    """(shape, tool result) whose preview Hermes's tool_result_storage cuts inside W: a one-line ensure_ascii JSON
    result cut inside one of W's escapes, a one-line ascii() repr cut inside one, and (W with a line break) a
    multi-line result whose preview ends at the line break inside W, its first line of 16 characters or more kept.
    The derived skeptic's survivor shapes (unrepr_cut_keep, open_literal_off, cut_per_line_off)."""
    out = []
    for shape, body, esc, lead, tail in (
            ("persist_json_escape", json.dumps(w, ensure_ascii=True)[1:-1], "\\u", '[{"status": "ok", "output": "',
             ' and more."}]'),
            ("persist_repr_escape", ascii(w)[1:-1], "\\x", "{'status': 'ok', 'output': '", " and more.'}")):
        at = [i for i in range(len(body)) if body.startswith(esc, i) and i >= 16]
        cut = (at[0] + 3) if at else 30  # inside the escape, or 30 characters into W
        out.append((shape, lead + "x" * (size - len(lead) - cut) + body + tail))
    if "\n" in w:
        first = w.split("\n", 1)[0]
        tail = "".join(FILLER % n for n in range(400, 440))
        head = "".join(FILLER % n for n in range(size // len(FILLER % 0) - 3))
        for pad in range(0, size):
            text = head + "y" * pad + "\n" + w + "\n" + tail
            content = json.dumps({"output": text, "exit_code": 0}, ensure_ascii=False)
            page = trs._pageable_text(content)
            end = page.index(w) + len(first)  # the line break inside W
            if end >= size - 4:
                out.append(("persist_line_cut", content))
                break
    return out


def main() -> dict[str, Any]:
    from agent import context_compressor as cc
    from tools import tool_result_storage as trs
    from tools.budget_config import DEFAULT_PREVIEW_SIZE_CHARS

    lines = []
    for label, texts in (("w", WS), ("control", CONTROLS)):
        for name, w in texts.items():
            for shape, tool, args, content in shapes(w):
                line = cc._summarize_tool_result(tool, json.dumps(args, ensure_ascii=False), content)
                lines.append({"kind": label, "w": name, "text": w, "shape": shape, "tool": tool, "args": args,
                              "line": line})
            for shape, content in persisted(trs, w, DEFAULT_PREVIEW_SIZE_CHARS):
                line = trs.maybe_persist_tool_result(content, "terminal", "call_%s_%s" % (name, shape), threshold=10)
                assert trs.PERSISTED_OUTPUT_TAG in line, line[:200]
                lines.append({"kind": label, "w": name, "text": w, "shape": shape, "tool": "terminal", "args": {},
                              "line": line})
    return {"lines": lines, "ws": WS, "controls": CONTROLS, "preview_size": DEFAULT_PREVIEW_SIZE_CHARS}


if __name__ == "__main__":
    try:
        data = main()
    except Exception as exc:  # the real Hermes function failed: the test reports it
        data = {"error": repr(exc), "trace": traceback.format_exc()}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
