"""
tools/reddit_tool.py
--------------------
Reddit search tool for r/cigars community data (via PRAW).

⚠️  Reddit API access now requires pre-approval (as of late 2025).
    The self-serve "create an app" flow is no longer sufficient on its own.
    You must apply at https://www.reddit.com/wiki/api and wait 2-4 weeks
    for approval before your credentials will work.

    Free non-commercial tier: 100 QPM, read-only.
    Commercial tier: ~$0.24/1,000 calls, requires a formal contract.

    ALTERNATIVE (recommended while waiting for approval, or instead of it):
    When REDDIT_CLIENT_ID/SECRET are not set, social_intel_agent.py
    automatically falls back to Claude's native web search, which searches
    Reddit content directly. This covers ~80% of the use case without
    any Reddit API credentials.

Requires (both optional — degrades gracefully if absent):
  REDDIT_CLIENT_ID     — from your approved Reddit app
  REDDIT_CLIENT_SECRET — from the same app

The user-agent is generated automatically from the app constants.
No login / user credentials are needed; read-only access is sufficient.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class RedditPost:
    title: str
    score: int                # net upvotes
    upvote_ratio: float       # 0.0–1.0
    num_comments: int
    url: str
    created_utc: float        # Unix timestamp
    body_preview: str = ""    # first 400 chars of selftext (empty for link posts)


# ── availability check ────────────────────────────────────────────────────────

def is_available() -> bool:
    """True if both Reddit credentials are present in the environment."""
    return bool(
        os.environ.get("REDDIT_CLIENT_ID")
        and os.environ.get("REDDIT_CLIENT_SECRET")
    )


def availability_note() -> str:
    """Human-readable note about which credentials are missing, or empty string if OK."""
    missing = [
        v for v in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET")
        if not os.environ.get(v)
    ]
    if not missing:
        return ""
    return (
        f"Reddit API not configured (missing: {', '.join(missing)}). "
        "Reddit API now requires pre-approval — apply at reddit.com/wiki/api "
        "(free non-commercial tier, ~2-4 week review). "
        "Claude web search is used as fallback in the meantime."
    )


# ── main search function ──────────────────────────────────────────────────────

def search_cigars(
    query: str,
    limit: int = 20,
    time_filter: str = "year",   # "hour","day","week","month","year","all"
    subreddit: str = "cigars",
) -> tuple[list[RedditPost], str]:
    """
    Search a subreddit (default: r/cigars) for posts matching *query*.

    Returns
    -------
    posts   : list[RedditPost]   Empty list on any failure.
    warning : str                Empty string on success; descriptive message on failure.
    """
    note = availability_note()
    if note:
        return [], note

    try:
        import praw  # optional dependency; graceful ImportError below
    except ImportError:
        return [], (
            "praw package not installed. "
            "Run: pip install praw  (or add it to requirements.txt)"
        )

    try:
        reddit = praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            user_agent="SmokeShoppe:SocialIntel/1.0 (by /u/smokeshoppe_bot)",
        )
        sub = reddit.subreddit(subreddit)
        submissions = list(
            sub.search(query, sort="relevance", time_filter=time_filter, limit=limit)
        )
        posts = [
            RedditPost(
                title=s.title,
                score=int(s.score),
                upvote_ratio=float(getattr(s, "upvote_ratio", 0.0)),
                num_comments=int(s.num_comments),
                url=f"https://www.reddit.com{s.permalink}",
                created_utc=float(s.created_utc),
                body_preview=(s.selftext or "")[:400],
            )
            for s in submissions
        ]
        return posts, ""
    except Exception as exc:
        return [], f"Reddit API error: {exc}"


def format_for_prompt(posts: list[RedditPost], warning: str) -> str:
    """
    Format Reddit results as a readable block for inclusion in an LLM prompt.
    Returns a short warning block if no posts are available.
    """
    if warning:
        return f"[Reddit data unavailable: {warning}]"
    if not posts:
        return "[No Reddit posts found for this query]"

    lines = [f"Reddit r/cigars — {len(posts)} posts found:"]
    for i, p in enumerate(posts[:15], 1):
        ratio_pct = f"{p.upvote_ratio * 100:.0f}% upvoted"
        lines.append(
            f"  {i}. \"{p.title}\"  "
            f"(score: {p.score}, {p.num_comments} comments, {ratio_pct})"
        )
        if p.body_preview:
            lines.append(f"     Preview: {p.body_preview[:200].strip()}")
        lines.append(f"     URL: {p.url}")
    return "\n".join(lines)
