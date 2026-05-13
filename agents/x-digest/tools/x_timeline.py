"""
X (Twitter) timeline fetcher.

Fetches tweets from people the authenticated user follows, since a given
timestamp. Uses X API v2's `users/:id/timelines/reverse_chronological` endpoint.

Required env vars:
    X_BEARER_TOKEN — OAuth2 bearer token (Basic tier or higher)

Optional env vars:
    X_DIGEST_MAX_TWEETS — cap per call (default 200)

Notes:
- The free X API tier does NOT support this endpoint. You need Basic ($100/mo)
  or higher.
- If you don't want to pay, swap this tool's implementation to use an
  unofficial scraping library like `twikit`. Same interface, different backend.
- The tool returns at most `limit` tweets, newest first. If you want
  pagination across days, increase `limit` and filter by `since` client-side.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from eonlet.tools import tool, ToolContext, ToolResult, ToolAnnotations


X_API_BASE = "https://api.x.com/2"


class XTimelineArgs(BaseModel):
    since: datetime = Field(
        description="Fetch tweets created after this UTC timestamp."
    )
    limit: int = Field(
        default=100,
        ge=10,
        le=200,
        description="Max tweets to return; X API caps at 200 per call.",
    )


class XTweet(BaseModel):
    id: str
    author: str
    author_id: str
    text: str
    created_at: datetime
    url: str
    is_retweet: bool
    is_reply: bool
    referenced_tweet_id: str | None = None
    metrics: dict[str, int] = Field(default_factory=dict)


class XTimelineResult(BaseModel):
    tweets: list[XTweet]
    fetched_at: datetime
    api_remaining: int | None = None
    note: str | None = None


@tool
class XTimelineFetch:
    """Fetch tweets from people the authenticated user follows."""

    name = "x_timeline_fetch"
    description = (
        "Fetch tweets from people the authenticated X user follows, since a "
        "given UTC timestamp. Returns up to `limit` tweets, newest first."
    )
    input_schema = XTimelineArgs
    output_schema = XTimelineResult
    annotations = ToolAnnotations(
        read_only=True,
        network=True,
        estimated_duration_s=3.0,
    )

    async def __call__(
        self, args: XTimelineArgs, ctx: ToolContext
    ) -> ToolResult:
        token = ctx.env.get("X_BEARER_TOKEN")
        if not token:
            return ToolResult(
                content="X_BEARER_TOKEN env var is not set. Cannot fetch.",
                is_error=True,
            )

        # Cap limit by env override if set
        env_cap = ctx.env.get("X_DIGEST_MAX_TWEETS")
        if env_cap is not None:
            args.limit = min(args.limit, int(env_cap))

        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: get the authenticated user's id
            me_resp = await client.get(
                f"{X_API_BASE}/users/me", headers=headers
            )
            if me_resp.status_code != 200:
                return ToolResult(
                    content=(
                        f"Failed to identify authenticated user. "
                        f"Status: {me_resp.status_code}. "
                        f"Body: {me_resp.text[:500]}"
                    ),
                    is_error=True,
                )
            user_id = me_resp.json()["data"]["id"]

            # Step 2: fetch reverse-chronological timeline
            params: dict[str, Any] = {
                "max_results": args.limit,
                "start_time": args.since.astimezone(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "tweet.fields": (
                    "author_id,created_at,public_metrics,"
                    "referenced_tweets,in_reply_to_user_id"
                ),
                "expansions": "author_id",
                "user.fields": "username",
            }
            tl_resp = await client.get(
                f"{X_API_BASE}/users/{user_id}/timelines/reverse_chronological",
                headers=headers,
                params=params,
            )

            if tl_resp.status_code == 429:
                return ToolResult(
                    content=(
                        "X API rate-limited (HTTP 429). "
                        "Reset header: "
                        f"{tl_resp.headers.get('x-rate-limit-reset', 'unknown')}. "
                        "Suggest sleeping and retrying."
                    ),
                    is_error=True,
                )
            if tl_resp.status_code != 200:
                return ToolResult(
                    content=(
                        f"X API returned {tl_resp.status_code}. "
                        f"Body: {tl_resp.text[:500]}"
                    ),
                    is_error=True,
                )

            payload = tl_resp.json()
            tweets_raw = payload.get("data", [])
            users_by_id = {
                u["id"]: u["username"]
                for u in payload.get("includes", {}).get("users", [])
            }

            tweets = []
            for t in tweets_raw:
                author_id = t["author_id"]
                author = users_by_id.get(author_id, "unknown")
                refs = t.get("referenced_tweets") or []
                is_retweet = any(r["type"] == "retweeted" for r in refs)
                is_reply = bool(t.get("in_reply_to_user_id"))
                ref_id = refs[0]["id"] if refs else None

                tweets.append(
                    XTweet(
                        id=t["id"],
                        author=author,
                        author_id=author_id,
                        text=t["text"],
                        created_at=datetime.fromisoformat(
                            t["created_at"].replace("Z", "+00:00")
                        ),
                        url=f"https://x.com/{author}/status/{t['id']}",
                        is_retweet=is_retweet,
                        is_reply=is_reply,
                        referenced_tweet_id=ref_id,
                        metrics=t.get("public_metrics", {}),
                    )
                )

            remaining_hdr = tl_resp.headers.get("x-rate-limit-remaining")
            api_remaining = int(remaining_hdr) if remaining_hdr else None

            result = XTimelineResult(
                tweets=tweets,
                fetched_at=datetime.now(timezone.utc),
                api_remaining=api_remaining,
                note=(
                    None
                    if len(tweets) == args.limit
                    else f"Returned {len(tweets)} of {args.limit} requested."
                ),
            )

            # Provide a rendered summary for the LLM that won't blow context.
            # Full structured output is in `structured_output` for downstream use.
            lines = [
                f"Fetched {len(tweets)} tweets since "
                f"{args.since.isoformat()} (API remaining: {api_remaining})."
            ]
            for t in tweets:
                kind = "RT" if t.is_retweet else ("reply" if t.is_reply else "post")
                # Truncate tweet text to keep tool output manageable
                snippet = t.text.replace("\n", " ")[:280]
                lines.append(
                    f"- [{kind}] @{t.author} ({t.created_at.isoformat()}): "
                    f"{snippet}  ({t.url})"
                )
            content = "\n".join(lines)

            return ToolResult(
                content=content,
                structured_output=result,
            )
