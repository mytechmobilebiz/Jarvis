#!/usr/bin/env python3
"""Generates the software bill of materials: SBOM.md (readable) and sbom.cdx.json (CycloneDX 1.5).

    python scripts/generate-sbom.py [--image infinite-command-center/backend:local]

Python packages are read from the built backend image (what actually ships),
npm packages from dashboard/package-lock.json, container images from the
table below. Run it after changing any dependency and commit the result.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Third-party images the stack runs. None is redistributed by Infinite
# Marketing: Docker pulls each from its publisher at install time.
IMAGES = [
    ("postgres", "18.6-alpine", "PostgreSQL License (permissive)", "runtime", "database"),
    ("n8nio/n8n", "2.43.7", "Sustainable Use License (fair-code; NOT open source; commercial redistribution restricted)", "runtime", "workflow engine"),
    ("nginx", "1.27-alpine", "BSD-2-Clause", "runtime", "serves the dashboard"),
    ("python", "3.12-slim", "PSF-2.0 (Python) + Debian package licenses", "build base", "backend image base"),
    ("node", "22-alpine", "MIT (Node.js) + Alpine package licenses", "build only", "builds the dashboard; not in the shipped image"),
    ("wordpress", "6-apache", "GPL-2.0-or-later", "test only", "throwaway site in docker-compose.testing.yml"),
    ("mariadb", "11", "GPL-2.0", "test only", "database for the throwaway WordPress"),
    ("searxng/searxng", "2026.10.4-d48c4b555", "AGPL-3.0-or-later", "optional",
     "web research search service; pulled only when research is switched on (docker-compose.research.yml)"),
]

PY_PROBE = r"""
import json
from importlib.metadata import distributions
out = []
for d in distributions():
    m = d.metadata
    lic = m.get("License-Expression") or ""
    if not lic:
        raw = (m.get("License") or "").strip()
        lic = raw if raw and len(raw) < 80 and "\n" not in raw else ""
    if not lic:
        cls = [c.split(" :: ")[-1] for c in (m.get_all("Classifier") or []) if c.startswith("License ::")]
        lic = "; ".join(cls)
    out.append({"name": m["Name"], "version": d.version, "license": lic or "UNKNOWN", "url": m.get("Home-page") or ""})
print(json.dumps(sorted(out, key=lambda p: p["name"].lower())))
"""


def python_packages(image: str) -> list[dict]:
    result = subprocess.run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c", PY_PROBE],
                            capture_output=True, text=True, check=True)
    direct = {line.split("==")[0].split("[")[0].lower() for line in (ROOT / "backend/requirements.txt").read_text().splitlines()
              if line and not line.startswith("#")}
    packages = json.loads(result.stdout)
    for p in packages:
        p["direct"] = p["name"].lower() in direct
        p["scope"] = "build tool" if p["name"].lower() in {"pip", "setuptools", "wheel"} else "runtime"
    return packages


def npm_packages() -> list[dict]:
    lock = json.loads((ROOT / "dashboard/package-lock.json").read_text())
    root = lock["packages"][""]
    direct = set(root.get("dependencies", {})) | set(root.get("devDependencies", {}))
    out = []
    for path, meta in lock["packages"].items():
        if not path:
            continue
        name = path.split("node_modules/")[-1]
        license_ = meta.get("license")
        manifest = ROOT / "dashboard" / path / "package.json"
        if not license_ and manifest.exists():
            license_ = json.loads(manifest.read_text()).get("license")
        out.append({
            "name": name,
            "version": meta.get("version", ""),
            "license": license_ if isinstance(license_, str) else json.dumps(license_) if license_ else "UNKNOWN",
            "scope": "build only" if meta.get("dev") else "shipped in the dashboard bundle",
            "direct": name in direct,
        })
    return sorted(out, key=lambda p: p["name"])


def cyclonedx(py: list[dict], npm: list[dict]) -> dict:
    def component(kind, name, version, lic, purl, scope):
        return {
            "type": kind, "name": name, "version": version,
            "licenses": [{"license": {"name": lic}}],
            "purl": purl,
            "scope": "excluded" if scope in ("build only", "test only", "build tool") else "optional" if scope == "optional" else "required",
            "properties": [{"name": "cc:scope", "value": scope}],
        }

    components = [component("library", p["name"], p["version"], p["license"], f"pkg:pypi/{p['name'].lower()}@{p['version']}", p["scope"]) for p in py]
    components += [component("library", p["name"], p["version"], p["license"], f"pkg:npm/{p['name']}@{p['version']}", p["scope"]) for p in npm]
    components += [component("container", n, v, lic, f"pkg:docker/{n}@{v}", scope) for n, v, lic, scope, _ in IMAGES]
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "serialNumber": f"urn:uuid:{uuid.uuid4()}", "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "component": {"type": "application", "name": "Infinite AI Command Center",
                          "version": (ROOT / "VERSION").read_text().strip(), "licenses": [{"license": {"name": "Proprietary - Infinite Marketing"}}]},
        },
        "components": components,
    }


def markdown(py: list[dict], npm: list[dict]) -> str:
    version = (ROOT / "VERSION").read_text().strip()
    lines = [
        f"# Software bill of materials: Infinite AI Command Center {version}",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d} by `scripts/generate-sbom.py`; machine-readable copy: `sbom.cdx.json` (CycloneDX 1.5).",
        "",
        "Infinite Marketing's own code (everything under `command-center/` except the dependencies below) is proprietary.",
        "No third-party component is vendored into this repository: each is fetched from its publisher at build or install time.",
        "",
        "## Container images",
        "",
        "| Image | Version | License | Used | Role |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {n} | {v} | {lic} | {scope} | {role} |" for n, v, lic, scope, role in IMAGES]
    lines += ["", "## Python packages (installed in the backend image)", "", "| Package | Version | License | Direct | Scope |", "|---|---|---|---|---|"]
    lines += [f"| {p['name']} | {p['version']} | {p['license']} | {'yes' if p['direct'] else ''} | {p['scope']} |" for p in py]
    shipped = [p for p in npm if p["scope"] != "build only"]
    build = [p for p in npm if p["scope"] == "build only"]
    lines += ["", f"## npm packages shipped in the dashboard bundle ({len(shipped)})", "", "| Package | Version | License | Direct |", "|---|---|---|---|"]
    lines += [f"| {p['name']} | {p['version']} | {p['license']} | {'yes' if p['direct'] else ''} |" for p in shipped]
    lines += ["", f"## npm packages used only to build or test the dashboard ({len(build)})", "",
              "Not distributed: they run during `npm run build` / `npm test` and are absent from the shipped image.", "",
              "| Package | Version | License |", "|---|---|---|"]
    lines += [f"| {p['name']} | {p['version']} | {p['license']} |" for p in build]
    unknown = [p["name"] for p in py + npm if p["license"] == "UNKNOWN"]
    lines += ["", "## Review notes", ""]
    lines.append("- Licenses to review before distribution: " + (", ".join(unknown) if unknown else "none reported as unknown."))
    lines.append("- n8n is fair-code (Sustainable Use License): see COMMERCIALIZATION.md before any commercial distribution.")
    lines.append("- GPL components (WordPress, MariaDB) appear only in the test overlay and are never shipped.")
    lines.append("- SearXNG (AGPL-3.0-or-later) runs from its official image, unmodified, only when web research is switched on;"
                 " each installation pulls it from the registry, and it is never bundled or re-hosted.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="infinite-command-center/backend:local")
    args = parser.parse_args()
    py = python_packages(args.image)
    npm = npm_packages()
    (ROOT / "sbom.cdx.json").write_text(json.dumps(cyclonedx(py, npm), indent=2) + "\n")
    (ROOT / "SBOM.md").write_text(markdown(py, npm))
    print(f"{len(py)} Python packages, {len(npm)} npm packages, {len(IMAGES)} images -> SBOM.md, sbom.cdx.json")


if __name__ == "__main__":
    main()
