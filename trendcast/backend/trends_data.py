"""Stage 3: fetch trending data from Apify actors, with a small in-memory cache.

Each source maps to one Apify actor. We run the actor synchronously, normalize its
output into one shape, and keep results for a few minutes so repeat questions are instant.
"""
import asyncio
import os
import time
from typing import Any, Callable

import httpx

APIFY_BASE = "https://api.apify.com/v2"
CACHE_TTL_SECONDS = int(os.getenv("TRENDS_CACHE_TTL", "900"))  # 15 minutes
ITEMS_PER_SOURCE = 8
ACTOR_RUN_TIMEOUT = 75  # seconds Apify may spend on a single run

GENERIC_TOPICS = {"", "general tech", "tech", "technology", "tech news"}

REDDIT_TIME = {"today": "day", "week": "week", "month": "month"}
YOUTUBE_DATE = {"today": "today", "week": "week", "month": "month"}
TIKTOK_DATE = {"today": "PAST_24_HOURS",
               "week": "PAST_WEEK", "month": "PAST_MONTH"}

DESC_CHAR_LIMIT = 250


class MissingApiKey(RuntimeError):
    pass


# ---------------------------------------------------------------- helpers

def _is_generic(topic: str) -> bool:
    return topic.strip().lower() in GENERIC_TOPICS


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = value.replace(",", "").strip()
        return int(digits) if digits.isdigit() else None
    return None


def _clip(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())  # collapse newlines and runs of spaces
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _make_structured_item(
    source: str,
    title: Any,
    url: Any = None,
    score: int | None = None,
    score_label: str = "",
    comments: int | None = None,
    description: str = "",
) -> dict[str, Any] | None:
    title = str(title or "").strip()
    if not title:
        return None
    return {
        "source": source,
        "title": title,
        "url": str(url) if url else None,
        "score": score,
        "score_label": score_label if score is not None else "",
        "comments": comments,
        "description": (description or "").strip()[:DESC_CHAR_LIMIT],
    }


# ------------------------------------------------- per-source input + output

def _reddit_input(topic: str, timeframe: str, count: int) -> dict[str, Any]:
    window = REDDIT_TIME[timeframe]

    return {
        "subredditName": "technology",
        "subredditTimeframe": window,
        "scrapeComments": True,
        "maxPosts": count,
        "maxComments": 5,
    } if _is_generic(topic) else {
        "queries": [topic],
        "timeframe": window,
        "scrapeComments": True,
        "maxPosts": count,
        "maxComments": 5,
    }


def _norm_reddit(item: dict[str, Any]) -> dict[str, Any] | None:
    subreddit = str(item.get("subreddit") or "")
    body = str(item.get("body") or "")
    return _make_structured_item(
        "reddit",
        item.get("title"),
        item.get("url"),
        _to_int(item.get("score")),
        "upvotes",
        _to_int(item.get("num_comments")),
        f"r/{subreddit}: {body}" if subreddit else body,
    )


def _clean_srt_subtitles(subtitle_data: list[dict]) -> str:
    # Extract the raw srt string from the dictionary inside the list
    raw_srt = subtitle_data[0].get('srt', '')

    # Remove lines containing only numbers (index) or timestamps (00:00:00 --> 00:00:00)
    cleaned_lines = []
    for line in raw_srt.split('\n'):
        line = line.strip()

        # Skip empty lines, numeric line counts, and timestamp arrows
        if not line or line.isdigit() or '-->' in line:
            continue

        # Clean up dialogue hyphens at the start of a line if present
        if line.startswith('- '):
            line = line[2:]
        elif line.startswith('-'):
            line = line[1:]

        cleaned_lines.append(line)

    # Join everything into a single block of text
    return " ".join(cleaned_lines)


def _youtube_input(topic: str, timeframe: str, count: int) -> dict[str, Any]:
    return {
        "searchQueries": ["latest tech news" if _is_generic(topic) else topic],
        "maxResults": 1,
        "maxResultsShorts": count - 1,
        "sortingOrder": "relevance",
        "dateFilter": YOUTUBE_DATE[timeframe],
        "transcriptionAndSubtitle": "ALWAYS_SUBTITLES",
    }


def _norm_youtube(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("error"):
        return None
    channel = item.get("channelName", "")
    description = item.get("text", "")
    subtitles = _clean_srt_subtitles(item.get("subtitles") or [{}])

    return _make_structured_item(
        "youtube",
        item.get("title"),
        item.get("url"),
        _to_int(item.get("viewCount")),
        "views",
        _to_int(item.get("commentsCount")),
        f"{channel}: {description} | {subtitles}",
    )


def _tiktok_input(topic: str, timeframe: str, count: int) -> dict[str, Any]:
    return {
        "searchQueries": ["tech" if _is_generic(topic) else topic],
        "searchSection": "/video",
        "videoSearchSorting": "MOST_LIKED",
        "videoSearchDateFilter": TIKTOK_DATE[timeframe],
        "resultsPerPage": count,
    }


def _norm_tiktok(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("error"):
        return None  # the actor reports unscrapable inputs as error items
    author_meta = item.get("authorMeta")
    # Nested in the JSON API, but the Apify console shows it flattened as "authorMeta.name".
    author = (author_meta.get("name") if isinstance(
        author_meta, dict) else None) or item.get("authorMeta.name")
    likes = _to_int(item.get("diggCount"))
    parts = [f"@{author}" if author else "",
             f"{likes:,} likes" if likes is not None else ""]
    return _make_structured_item(
        "tiktok",
        _clip(str(item.get("text") or "")),
        item.get("webVideoUrl"),
        _to_int(item.get("playCount")),
        "plays",
        _to_int(item.get("commentCount")),
        " · ".join(p for p in parts if p),
    )


def _hashtag(topic: str) -> str:
    if _is_generic(topic):
        return "tech"
    tag = topic.lower().replace("c++", "cpp").replace("c#", "csharp")
    return "".join(ch for ch in tag if ch.isalnum()) or "tech"


def _instagram_input(topic: str, timeframe: str, count: int) -> dict[str, Any]:
    # Hashtag pages have no date filter, so `timeframe` is ignored for this source.
    return {"hashtags": [_hashtag(topic)], "resultsType": "posts", "resultsLimit": count}


def _norm_instagram(it: dict[str, Any]) -> dict[str, Any] | None:
    owner = str(it.get("ownerUsername") or "")
    caption = _clip(str(it.get("caption") or ""))
    plays = _to_int(it.get("videoPlayCount"))
    likes = _to_int(it.get("likesCount"))
    comments = it.get("latestComments")
    if likes is not None and likes < 0:
        likes = None  # hidden like counts come back as -1
    score, label = (plays, "plays") if plays else (likes, "likes")
    return _make_structured_item(
        "instagram",
        caption or (f"Post by @{owner}" if owner else ""),
        it.get("url"),
        score,
        label,
        _to_int(it.get("commentsCount")),
        f"@{owner} | " if owner else "",
    )


# `fetch` = how many items to pull from the actor (this is what you pay for)
SOURCES: dict[str, dict[str, Any]] = {
    "reddit": {
        "actor": "fatihtahta~reddit-scraper-search-fast",
        "fetch": 10,
        "build": _reddit_input,
        "norm": _norm_reddit,
    },
    "youtube": {
        "actor": "streamers~youtube-scraper",
        "fetch": 10,
        "build": _youtube_input,
        "norm": _norm_youtube,
    },
    "tiktok": {
        "actor": "clockworks~tiktok-scraper",
        "fetch": 5,
        "build": _tiktok_input,
        "norm": _norm_tiktok,
    },
    "instagram": {
        "actor": "apify~instagram-hashtag-scraper",
        "fetch": 10,
        "build": _instagram_input,
        "norm": _norm_instagram,
    },
}


# --------------------------------------------------------------- Apify call

_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}


async def _run_actor(
    client: httpx.AsyncClient, actor: str, payload: dict[str, Any], limit: int
) -> list[Any]:
    r = await client.post(
        f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items",
        headers={"Authorization": f"Bearer {os.getenv('APIFY_TOKEN')}"},
        params={"timeout": ACTOR_RUN_TIMEOUT, "limit": limit, "clean": "true"},
        json=payload,
        # the shared client's 30 s default is too short here
        timeout=ACTOR_RUN_TIMEOUT + 20,
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


async def fetch_source(
    client: httpx.AsyncClient, source: str, topic: str, timeframe: str
) -> dict[str, Any]:
    key = (source, topic.strip().lower(), timeframe)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return {"source": source, "cached": True, "items": hit[1]}

    source_details = SOURCES[source]
    actor = os.getenv(f"APIFY_ACTOR_{source.upper()}", source_details["actor"])
    payload = source_details["build"](
        topic, timeframe, source_details["fetch"])
    raw = await _run_actor(client, actor, payload, source_details["fetch"])

    if os.getenv("APIFY_DEBUG"):
        print(f"[apify:{source}] {len(raw)} raw items, first: {raw[:1]}")

    norm: Callable[[dict[str, Any]], dict[str, Any]
                   | None] = source_details["norm"]
    items = [e for e in (norm(item) for item in raw if isinstance(
        item, dict)) if e][:ITEMS_PER_SOURCE]

    if items:  # never cache an empty result; it may be a failed scrape
        _cache[key] = (time.monotonic(), items)
    return {"source": source, "cached": False, "items": items}


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        try:
            message = str(exc.response.json()["error"]["message"])
        except Exception:
            message = exc.response.text[:200]
        hint = {
            401: " (check APIFY_TOKEN)",
            402: " (out of Apify credits)",
            403: " (token lacks permission)",
            404: " (actor not found)",
            408: " (run took too long)",
        }.get(status, "")
        return f"Apify returned {status}{hint}: {message}".strip()
    if isinstance(exc, httpx.TimeoutException):
        return "Timed out waiting for Apify."
    if isinstance(exc, httpx.HTTPError):
        return "Could not reach Apify."
    return str(exc) or exc.__class__.__name__


async def fetch_trends(
    client: httpx.AsyncClient, sources: list[str], topic: str, timeframe: str
) -> dict[str, Any]:
    """Fetch all requested sources in parallel. One source failing doesn't sink the rest."""
    if not os.getenv("APIFY_TOKEN"):
        raise MissingApiKey("APIFY_TOKEN is not set on the server.")

    results = await asyncio.gather(
        *(fetch_source(client, s, topic, timeframe) for s in sources),
        return_exceptions=True,
    )
    ok: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for source, res in zip(sources, results):
        if isinstance(res, BaseException):
            print(f"[apify:{source}] failed: {describe_error(res)}")
            errors[source] = describe_error(res)
        else:
            ok.append(res)
    return {"sources": ok, "errors": errors}
