"""Content & Social: blog and social drafts; publishing only after approval (mock)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import Field

from ..models import Artifact, Site
from .base import AgentContext, AgentResult, ArtifactSpec, BaseAgent, Intent, Params, Risk, capability
from .drafting import write

PLATFORMS = ("facebook", "instagram", "linkedin", "x", "google_business")


class BlogParams(Params):
    topic: str = Field(min_length=3, max_length=200)
    audience: str = Field("small business owners", max_length=200)
    words: int = Field(800, ge=200, le=2500)


class SocialParams(Params):
    topic: str = Field(min_length=3, max_length=200)
    platforms: list[str] = Field(default_factory=lambda: ["facebook", "instagram", "linkedin"])
    posts_per_platform: int = Field(1, ge=1, le=5)


class PublishParams(Params):
    artifact_id: int = Field(description="The approved draft to publish")
    destination: str = Field(description="'wordpress' (needs site_id) or a social platform name")
    site_id: int | None = None


def _topic_from(text: str) -> str:
    m = re.search(r"(?i)\b(about|on|for)\s+(.+)$", text)
    return (m.group(2) if m else text).strip(" .?!")[:200]


class ContentAgent(BaseAgent):
    name = "content"
    title = "Content & Social"
    description = "Drafts blog posts and social posts in Infinite Marketing's voice. Publishing waits for approval (mock in this build)."
    integrations = ("wordpress_publish", "social")

    def intents(self) -> list[Intent]:
        return [
            Intent("draft_blog_post", (r"\bblog\b", r"\barticle\b", r"\bwrite\b.*\bpost\b"), lambda text, urls: {"topic": _topic_from(text)}, weight=2),
            Intent("draft_social_posts", (r"\bsocial\b", r"\b(facebook|instagram|linkedin|tweet|posts?)\b"),
                   lambda text, urls: {"topic": _topic_from(text)}),
        ]

    @capability(risk=Risk.DRAFT, description="Draft a blog post (Markdown) for review.", params=BlogParams,
                examples=("Write a blog post about why small businesses need fast websites",))
    async def draft_blog_post(self, ctx: AgentContext, params: BlogParams) -> AgentResult:
        def template() -> str:
            t = params.topic
            return "\n".join([
                f"# {t[:1].upper() + t[1:]}", "", "_Template outline: no AI model is connected. Fill in each section before publishing._", "",
                "## Introduction", f"Why {t} matters to {params.audience}, in two or three sentences.", "",
                "## The problem", "What goes wrong without it; a short, concrete example.", "",
                "## What good looks like", "- Point one\n- Point two\n- Point three", "",
                "## How Infinite Marketing helps", "One paragraph on the relevant service; no prices or guarantees.", "",
                "## Next step", "A clear call to action: book a free consultation.",
            ])

        text, writer = await write(
            ctx, purpose="blog_draft",
            system="You write for Infinite Marketing, a web design and digital marketing agency serving small businesses. Warm, clear, "
                   "professional, short sentences. Markdown with an H1 title, H2 sections and a call to action. Never invent statistics, "
                   "client names, prices, guarantees or results.",
            prompt=f"Topic: {params.topic}\nAudience: {params.audience}\nLength: about {params.words} words",
            fallback=template,
        )
        title = text.splitlines()[0].lstrip("# ").strip() or params.topic
        return AgentResult(summary=f"Blog draft '{title}' ready for review ({writer})", data={"writer": writer},
                           artifacts=[ArtifactSpec(kind="blog_draft", title=title, content=text, data={"writer": writer, "topic": params.topic})])

    @capability(risk=Risk.DRAFT, description="Draft social posts for one or more platforms.", params=SocialParams,
                examples=("Draft social posts about our new AI chatbot service",))
    async def draft_social_posts(self, ctx: AgentContext, params: SocialParams) -> AgentResult:
        platforms = [p for p in (x.lower() for x in params.platforms) if p in PLATFORMS] or ["facebook"]

        def template() -> str:
            out = [f"# Social posts: {params.topic}", "", "_Template drafts: no AI model is connected._", ""]
            for p in platforms:
                for i in range(params.posts_per_platform):
                    out += [f"## {p} #{i + 1}", f"Thinking about {params.topic}? Here is one thing most small businesses miss... "
                            "[your tip]. Want help? Book a free consultation with Infinite Marketing.", ""]
            return "\n".join(out)

        text, writer = await write(
            ctx, purpose="social_drafts",
            system="You write social posts for Infinite Marketing. Match each platform's length and style. One clear call to action each. "
                   "No invented statistics, prices, guarantees or client names. Markdown: one H2 per post named '<platform> #<n>'.",
            prompt=f"Topic: {params.topic}\nPlatforms: {', '.join(platforms)}\nPosts per platform: {params.posts_per_platform}",
            fallback=template,
        )
        return AgentResult(summary=f"{len(platforms) * params.posts_per_platform} social post draft(s) on '{params.topic}' ({writer})",
                           data={"writer": writer, "platforms": platforms},
                           artifacts=[ArtifactSpec(kind="social_drafts", title=f"Social posts - {params.topic}", content=text,
                                                   data={"writer": writer, "platforms": platforms})])

    @capability(risk=Risk.EXTERNAL, description="Publish an approved draft to a WordPress site or a social platform (mock in this build).",
                params=PublishParams)
    async def publish_draft(self, ctx: AgentContext, params: PublishParams) -> AgentResult:
        artifact = await ctx.session.get(Artifact, params.artifact_id)
        if artifact is None or artifact.kind not in ("blog_draft", "social_drafts"):
            raise ValueError(f"artifact {params.artifact_id} is not a content draft")
        if params.destination == "wordpress":
            site = await ctx.session.get(Site, params.site_id) if params.site_id else None
            if site is None:
                raise ValueError("publishing to WordPress needs the site_id of a monitored site")
            entry = await ctx.integrations.get("wordpress_publish").publish(  # type: ignore[attr-defined]
                ctx, self.name, site_url=site.url, title=artifact.title, content=artifact.content, status="draft")
        elif params.destination in PLATFORMS:
            entry = await ctx.integrations.get("social").publish(ctx, self.name, platform=params.destination, text=artifact.content)  # type: ignore[attr-defined]
        else:
            raise ValueError(f"unknown destination '{params.destination}'")
        artifact.data = {**artifact.data, "published_mock": {"outbox_id": entry.id, "at": datetime.now(UTC).isoformat()}}
        return AgentResult(summary=f"Recorded publishing of '{artifact.title}' to {params.destination} (mock: outbox #{entry.id}, nothing was published)",
                           data={"outbox_id": entry.id})
