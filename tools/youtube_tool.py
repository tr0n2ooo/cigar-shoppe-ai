"""
tools/youtube_tool.py
---------------------
YouTube Data API v3 search tool for finding cigar review videos.

Requires (optional — degrades gracefully if absent):
  YOUTUBE_API_KEY — enable "YouTube Data API v3" in Google Cloud Console,
                    create an API key (free tier: 10,000 units/day).

Uses the requests library (already available via pandas) — no extra SDK needed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass
class YouTubeVideo:
    title: str
    channel: str
    video_id: str
    url: str
    published_at: str          # YYYY-MM-DD
    description_preview: str = ""


# ── availability check ────────────────────────────────────────────────────────

def is_available() -> bool:
    """True if a YouTube API key is present in the environment."""
    return bool(os.environ.get("YOUTUBE_API_KEY"))


def availability_note() -> str:
    """Human-readable note if key is missing, or empty string if OK."""
    if os.environ.get("YOUTUBE_API_KEY"):
        return ""
    return (
        "YouTube API not configured (missing: YOUTUBE_API_KEY). "
        "Enable YouTube Data API v3 in Google Cloud Console and set the key to get video data."
    )


# ── main search function ──────────────────────────────────────────────────────

def search_videos(
    query: str,
    max_results: int = 10,
    query_suffix: str = "cigar review",
) -> tuple[list[YouTubeVideo], str]:
    """
    Search YouTube for videos matching *query* (with *query_suffix* appended).

    Returns
    -------
    videos  : list[YouTubeVideo]   Empty list on any failure.
    warning : str                  Empty string on success; descriptive message on failure.
    """
    note = availability_note()
    if note:
        return [], note

    try:
        import requests
    except ImportError:
        return [], "requests package not installed."

    try:
        resp = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": f"{query} {query_suffix}".strip(),
                "type": "video",
                "order": "relevance",
                "maxResults": max_results,
                "key": os.environ["YOUTUBE_API_KEY"],
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        videos = [
            YouTubeVideo(
                title=item["snippet"]["title"],
                channel=item["snippet"]["channelTitle"],
                video_id=item["id"]["videoId"],
                url=f"https://www.youtube.com/watch?v={item['id']['videoId']}",
                published_at=item["snippet"]["publishedAt"][:10],
                description_preview=item["snippet"].get("description", "")[:200],
            )
            for item in data.get("items", [])
            if item.get("id", {}).get("videoId")
        ]
        return videos, ""

    except Exception as exc:
        return [], f"YouTube API error: {exc}"


def format_for_prompt(videos: list[YouTubeVideo], warning: str) -> str:
    """
    Format YouTube results as a readable block for inclusion in an LLM prompt.
    """
    if warning:
        return f"[YouTube data unavailable: {warning}]"
    if not videos:
        return "[No YouTube videos found for this query]"

    lines = [f"YouTube — {len(videos)} videos found:"]
    for i, v in enumerate(videos, 1):
        lines.append(f"  {i}. \"{v.title}\" by {v.channel} (published: {v.published_at})")
        lines.append(f"     URL: {v.url}")
    return "\n".join(lines)
