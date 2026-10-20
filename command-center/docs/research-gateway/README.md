# Research Gateway: proposed SearXNG files (not in use)

These are the copy-ready files of section 2 of
[../RESEARCH_GATEWAY_PLAN.md](../RESEARCH_GATEWAY_PLAN.md), kept here for review.
Nothing uses them: no script, compose file or installer refers to this folder, and
nothing is installed until the owner approves the plan and a tested 0.7.0 release.

- `docker-compose.research.yml`: the optional overlay (in the plan it lives next to
  `docker-compose.yml` and is switched on by `scripts\research.ps1 -Enable`).
- `searxng/settings.yml` and `searxng/limiter.toml`: the read-only SearXNG
  configuration (in the plan, `research/searxng/`).

Sandbox runs of exactly these files: [S3] in the plan's evidence table.
