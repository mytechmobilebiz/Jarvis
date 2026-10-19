#!/usr/bin/env python3
"""Writes templates.json: the JARVIS template files as shipped in 1.7.4 and as the repository holds them after the
owner's changes, so discovery can tell which files on the PC are untouched templates, changed templates or new.

Development only; reads the git history of this repository and nothing else.

    python make_templates.py [--out templates.json]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jarvis_kit import TEXT_DIFF_LIMIT, TEXT_SCAN_LIMIT, normalize, yaml_keys  # noqa: E402

REVISIONS = {
    # "Import Jarvis v1.7.4 source"
    "shipped-1.7.4": "288cb6cf3e204f3cba8161161df7105bb2c1a44a",
    # "Add a zero-token website watchdog for the owner's sites", the last owner change to the templates
    "owner-edited": "3d83ecc4ce85080d0fcfed7740d0871a2bf5b351",
}
FOLDERS = ("agents", "playbooks", "demo", "examples")


def git(*args):
    return subprocess.run(("git",) + args, check=True, capture_output=True).stdout


def file_hash(data):
    """As discovery computes it: text normalized (no BOM, LF endings), other files as they are."""
    if len(data) <= TEXT_SCAN_LIMIT and b"\0" not in data[:8192]:
        text = normalize(data).decode("utf-8", errors="replace")
        return hashlib.sha256(text.encode("utf-8")).hexdigest(), text
    return hashlib.sha256(data).hexdigest(), None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json"))
    args = parser.parse_args()
    files, cron_jobs, skills, env_parts, config_words, mcp_servers = {}, {}, set(), set(), set(), set()
    for label, revision in REVISIONS.items():
        listing = git("ls-tree", "-r", "--full-tree", "--name-only", "-z", revision, "--", *FOLDERS)
        paths = listing.decode("utf-8").split("\0")
        for path in filter(None, paths):
            if "__pycache__" in path.split("/"):
                continue
            data = git("show", "%s:%s" % (revision, path))
            digest, text = file_hash(data)
            if text is not None:
                # The words of the setting names the templates and playbooks use (EMAIL_SMTP_HOST and so on).
                for name in re.findall(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", text):
                    env_parts.update(part for part in name.split("_") if part)
            entry = {"sha256": digest, "size": len(data)}
            # The text (for counting changed lines) only of the role templates small enough to compare.
            if text is not None and path.startswith("agents/") and len(text) <= TEXT_DIFF_LIMIT:
                entry["text"] = text
            files.setdefault(path, {})[label] = entry
            parts = path.split("/")
            if text is not None and parts[0] == "agents" and parts[-1] in ("config.yaml", "distribution.yaml"):
                found = yaml_keys(text)
                config_words.update(seg for key in found["keys"] for seg in key.split("."))
                mcp_servers.update(found["mcp_servers"])
            if len(parts) == 5 and parts[0] == "agents" and parts[2] == "skills" and parts[4] == "SKILL.md":
                skills.add(parts[3])
            if len(parts) == 4 and parts[0] == "agents" and parts[2:] == ["cron", "jobs.json"] and text:
                for job in json.loads(text).get("jobs", []):
                    cron_jobs[job["name"]] = {"role": parts[1], "prompt": job.get("prompt", ""),
                                              "schedule": job.get("schedule", {}).get("expr", ""),
                                              "revision": label}
    # Keep one copy of the text when both revisions hold the same file.
    for revisions in files.values():
        shipped, edited = revisions.get("shipped-1.7.4"), revisions.get("owner-edited")
        if shipped and edited and shipped["sha256"] == edited["sha256"]:
            edited.pop("text", None)
            edited["same_as"] = "shipped-1.7.4"
    templates = {"generated_from": {"repository": "mytechmobilebiz/Jarvis", "revisions": REVISIONS},
                 "files": dict(sorted(files.items())), "cron_jobs": dict(sorted(cron_jobs.items())),
                 "skill_names": sorted(skills), "env_name_parts": sorted(env_parts),
                 "config_key_words": sorted(config_words), "mcp_servers": sorted(mcp_servers)}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(templates, f, indent=1, sort_keys=True)
        f.write("\n")
    print("%s: %d files, %d scheduled jobs, %d skills" % (args.out, len(files), len(cron_jobs), len(skills)))


if __name__ == "__main__":
    main()
