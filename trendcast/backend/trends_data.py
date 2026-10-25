"""Stage 3: fetch trending data from Apify actors, with a small in-memory cache.

Each source maps to one Apify actor. We run it synchronously and turn its output into
plain-text documents (post + top comments, description + transcript, ...) that the next
stage feeds to the LLM. Results are cached for a few minutes so repeat questions are instant.
"""
import asyncio
import os
import re
import time
from typing import Any, Awaitable, Callable

import httpx

APIFY_BASE = "https://api.apify.com/v2"
CACHE_TTL_SECONDS = int(os.getenv("TRENDS_CACHE_TTL", "900"))  # 15 minutes
ITEMS_PER_SOURCE = 5
# seconds Apify may spend on a single run (a timeout returns nothing, so don't go too low)
ACTOR_RUN_TIMEOUT = 30

GENERIC_TOPICS = {"", "general tech", "tech", "technology", "tech news"}

# Reddit only: if a narrow window finds nothing, try the next one once.
WIDER_TIMEFRAME = {"today": "week", "week": "month"}
REDDIT_TIME = {"today": "day", "week": "week", "month": "month"}
YOUTUBE_DATE = {"today": "today", "week": "week", "month": "month"}

# How much text each source contributes to the LLM context (characters).
REDDIT_COMMENTS_PER_POST = 3
REDDIT_BODY_CHARS = 600
REDDIT_COMMENT_CHARS = 200
YOUTUBE_DESC_CHARS = 300
YOUTUBE_TRANSCRIPT_CHARS = 1200
INSTAGRAM_TRANSCRIPT_CHARS = 800
# Instagram's own caption limit, so effectively the whole caption
INSTAGRAM_CAPTION_CHARS = 2200
INSTAGRAM_COMMENT_CHARS = 200
INSTAGRAM_COMMENTS_SHOWN = 3


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
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def _trim(text: str, limit: int) -> str:
    """Cut long text but keep its line breaks."""
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def _fmt(value: int | None, label: str) -> str:
    return f"{value:,} {label}" if value is not None else ""


def _stats_line(*parts: str) -> str:
    return " · ".join(p for p in parts if p)


def _make_structured_item(
    source: str, title: Any, url: Any, text: str
) -> dict[str, Any] | None:
    """One result. `text` is exactly what the LLM will read for this post/video."""
    title = str(title or "").strip()
    if not title:
        return None
    return {
        "source": source,
        "title": title,
        "url": str(url) if url else None,
        "text": text.strip(),
    }


def _per_item(
    fn: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Turn a one-record normalizer into one that takes the whole list."""
    def run(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [e for e in (fn(it) for it in raw) if e]
    return run


# Subtitle files (SRT from YouTube, WebVTT from TikTok) share the same cue layout:
#   [number]\n00:00:01,000 --> 00:00:03,000\ncaption text\n\n
_CUE_TIMESTAMP = re.compile(
    r"(?:\d+:)?\d+:\d+[,.]\d+\s*-->\s*(?:\d+:)?\d+:\d+[,.]\d+")
_MARKUP_TAGS = re.compile(r"<[^>]+>")


def _cues_to_text(raw: str) -> str:
    """Keep only the caption text: no cue numbers, timestamps, markup or repeated lines."""
    parts: list[str] = []
    in_cue = False  # True between a cue's timestamp line and the blank line that ends it
    for raw_line in raw.splitlines():
        line = _MARKUP_TAGS.sub("", raw_line).strip()
        if _CUE_TIMESTAMP.search(line):
            in_cue = True
        elif not line:
            in_cue = False  # blank line (or a " " filler cue) ends the cue
        elif in_cue:
            if line.startswith("- "):  # dialogue dash
                line = line[2:]
            # auto captions repeat lines
            if line and (not parts or parts[-1] != line):
                parts.append(line)
    return " ".join(parts)


def _clean_srt_subtitles(subtitle_data: Any) -> str:
    """YouTube: flatten the actor's `subtitles` list into plain transcript text."""
    if not isinstance(subtitle_data, list):
        return ""
    tracks = [t for t in subtitle_data if isinstance(
        t, dict) and isinstance(t.get("srt"), str)]
    if not tracks:
        return ""
    track = next((t for t in tracks if t.get("language") == "en"), tracks[0])
    return _cues_to_text(track["srt"])


# ------------------------------------------------- per-source input + output

# ---- Reddit: post text + top comments

def _reddit_input(topic: str, timeframe: str, count: int) -> dict[str, Any]:
    window = REDDIT_TIME[timeframe]
    query = "technology" if _is_generic(topic) else topic
    return {
        "queries": [query],
        "sort": "top",
        "timeframe": window,
        "scrapeComments": True,
        "maxPosts": count,
        "maxComments": REDDIT_COMMENTS_PER_POST,
    }


def _norm_reddit(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Posts and comments arrive as separate records; attach each comment to its post."""
    comments_by_post: dict[str, list[dict[str, Any]]] = {}
    for rec in raw:
        if rec.get("kind") == "comment" and rec.get("postId"):
            comments_by_post.setdefault(str(rec["postId"]), []).append(rec)

    items: list[dict[str, Any]] = []
    for rec in raw:
        # anything else counts as a post, whatever the actor calls it
        if rec.get("kind") == "comment":
            continue
        comments = [
            c for c in comments_by_post.get(str(rec.get("id")), [])
            if str(c.get("body") or "").strip() not in ("", "[deleted]", "[removed]")
        ]
        # The actor doesn't document its comment order, so sort by score ourselves.
        comments.sort(key=lambda c: _to_int(c.get("score")) or 0, reverse=True)

        subreddit = str(rec.get("subreddit") or "")
        lines = [
            _stats_line(
                f"r/{subreddit}" if subreddit else "",
                _fmt(_to_int(rec.get("score")), "upvotes"),
                _fmt(_to_int(rec.get("num_comments")), "comments"),
            ),
            "",
            _trim(str(rec.get("body") or ""),
                  REDDIT_BODY_CHARS) or "(link post, no text)",
        ]
        if comments:
            lines += ["", "Top comments:"]
            for c in comments[:REDDIT_COMMENTS_PER_POST]:
                votes = _fmt(_to_int(c.get("score")), "upvotes")
                prefix = f"- ({votes}) " if votes else "- "
                lines.append(
                    prefix + _trim(_one_line(c.get("body")), REDDIT_COMMENT_CHARS))
        item = _make_structured_item("reddit", rec.get(
            "title"), rec.get("url"), "\n".join(lines))
        if item:
            items.append(item)
    return items


# ---- YouTube: description + transcript

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
    # `or ""` matters: the key can exist with a null value, which .get(key, "") would return as None.
    description = _trim(str(item.get("text") or ""), YOUTUBE_DESC_CHARS)
    transcript = _trim(_clean_srt_subtitles(
        item.get("subtitles")), YOUTUBE_TRANSCRIPT_CHARS)
    header = _stats_line(
        str(item.get("channelName") or ""),
        _fmt(_to_int(item.get("viewCount")), "views"),
        _fmt(_to_int(item.get("commentsCount")), "comments"),
    )
    text = "\n".join([
        header, "",
        "Description:", description or "(none)", "",
        "Transcript:", transcript or "(none available)",
    ])
    return _make_structured_item("youtube", item.get("title"), item.get("url"), text)

# ---- Instagram: caption + transcript (reels) + comments


def _hashtag(topic: str) -> str:
    if _is_generic(topic):
        return "tech"
    tag = topic.lower().replace("c++", "cpp").replace("c#", "csharp")
    return "".join(ch for ch in tag if ch.isalnum()) or "tech"


def _instagram_input(topic: str, timeframe: str, count: int) -> dict[str, Any]:
    # Hashtag pages have no date filter, so `timeframe` is ignored for this source.
    return {"hashtags": [_hashtag(topic)], "resultsType": "posts", "resultsLimit": count}


_TRAILING_HASHTAGS = re.compile(r"(?:\s*#\w+)+\s*$")

def _strip_trailing_hashtags(caption: str) -> str:
    """Drop the hashtag wall at the end of a caption (hashtags inside sentences stay)."""
    return _TRAILING_HASHTAGS.sub("", caption).strip()


def _comment_texts(item: dict[str, Any]) -> list[str]:
    found: list[str] = []
    latest = item.get("latestComments")
    if isinstance(latest, list):
        for c in latest:
            body = c.get("text") if isinstance(c, dict) else c
            if isinstance(body, str) and body.strip():
                found.append(_trim(_one_line(body), INSTAGRAM_COMMENT_CHARS))
    first = item.get("firstComment")
    if not found and isinstance(first, str) and first.strip():
        found.append(_trim(_one_line(first), INSTAGRAM_COMMENT_CHARS))
    return found[:INSTAGRAM_COMMENTS_SHOWN]


def _norm_instagram(item: dict[str, Any]) -> dict[str, Any] | None:
    caption = _strip_trailing_hashtags(str(item.get("caption") or ""))
    likes = _to_int(item.get("likesCount"))
    if likes is not None and likes < 0:
        likes = None  # hidden like counts come back as -1
    comments = _comment_texts(item)

    # The poster's username is deliberately left out: the LLM can't repeat what it never sees.
    lines = [
        _stats_line(
            _fmt(_to_int(item.get("videoPlayCount")), "plays"),
            _fmt(likes, "likes"),
            _fmt(_to_int(item.get("commentsCount")), "comments"),
        ),
        "",
        "Caption:", _trim(caption, INSTAGRAM_CAPTION_CHARS) or "(none)",
    ]
    if comments:
        lines += ["", "Comments:"] + [f"- {c}" for c in comments]
    return _make_structured_item(
        "instagram",
        _clip(caption) or "Instagram post",
        item.get("url"),
        "\n".join(lines),
    )


# `fetch`  = how many items to pull from the actor (this is what you pay for)
# `limit`  = max dataset records to download (Reddit needs room for posts AND their comments)
# `enrich` = optional async step that adds extra text (subtitles, transcripts) to the raw records
# `norm`   = turns the raw records into [{source, title, url, text}]
SOURCES: dict[str, dict[str, Any]] = {
    "reddit": {
        "actor": "fatihtahta~reddit-scraper-search-fast",
        "fetch": 6,
        "timeout": 120,
        "limit": 6 * (1 + REDDIT_COMMENTS_PER_POST),
        "build": _reddit_input,
        "norm": _norm_reddit,
    },
    "youtube": {
        "actor": "streamers~youtube-scraper",
        "fetch": 5,
        "timeout": 75,  # transcripts make this the slow one; the log line shows the real time
        "build": _youtube_input,
        "norm": _per_item(_norm_youtube),
    },
    "instagram": {
        "actor": "apify~instagram-hashtag-scraper",
        "fetch": 5,
        "timeout": 45,
        "build": _instagram_input,
        "norm": _per_item(_norm_instagram),
    },
}


# --------------------------------------------------------------- Apify call

_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}


async def _run_actor(
    client: httpx.AsyncClient,
    actor: str,
    payload: dict[str, Any],
    limit: int,
    timeout: int = ACTOR_RUN_TIMEOUT,
) -> list[Any]:
    r = await client.post(
        f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items",
        headers={"Authorization": f"Bearer {os.getenv('APIFY_TOKEN')}"},
        params={"timeout": timeout, "limit": limit, "clean": "true"},
        json=payload,
        # the shared client's 30 s default is too short here
        timeout=timeout + 20,
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
    limit = source_details.get("limit", source_details["fetch"])
    timeout = source_details.get("timeout", ACTOR_RUN_TIMEOUT)
    run_started = time.monotonic()
    try:
        raw = [r for r in await _run_actor(client, actor, payload, limit, timeout) if isinstance(r, dict)]
    finally:  # logged even when the run times out, so you can see how close to the limit it is
        print(
            f"[apify:{source}] actor took {time.monotonic() - run_started:.1f}s (limit {timeout}s)")
    if os.getenv("APIFY_DEBUG"):
        print(f"[apify:{source}] {len(raw)} raw items, first: {raw[:1]}")

    enrich: Callable[[httpx.AsyncClient, list[dict[str, Any]]],
                     Awaitable[None]] | None = source_details.get("enrich")
    if enrich:
        await enrich(client, raw)

    norm: Callable[[list[dict[str, Any]]],
                   list[dict[str, Any]]] = source_details["norm"]
    items = norm(raw)[:ITEMS_PER_SOURCE]

    if not items:
        kinds = sorted({str(r.get("kind")) for r in raw})
        print(f"[apify:{source}] 0 items from {len(raw)} raw records (kinds: {kinds}) "
              f"for topic={topic!r} timeframe={timeframe!r}; payload was {payload}")
        wider = WIDER_TIMEFRAME.get(timeframe)
        if source == "reddit" and wider:
            print(f"[apify:reddit] retrying with timeframe={wider!r}")
            return await fetch_source(client, source, topic, wider)

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
