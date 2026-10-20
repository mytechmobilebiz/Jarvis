# Research Gateway: the SearXNG files have moved

The proposed files that were reviewed here are now in their real places
(0.7.0-dev, unchanged from the reviewed copies):

- [`../../docker-compose.research.yml`](../../docker-compose.research.yml): the
  optional overlay, switched on by `scripts\research.ps1 -Enable`.
- [`../../research/searxng/settings.yml`](../../research/searxng/settings.yml)
  and [`limiter.toml`](../../research/searxng/limiter.toml): the read-only
  SearXNG configuration.

What the development build contains and how it is switched on:
[../RESEARCH_GATEWAY.md](../RESEARCH_GATEWAY.md). The design:
[../RESEARCH_GATEWAY_PLAN.md](../RESEARCH_GATEWAY_PLAN.md).
