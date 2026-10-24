"""Cartesia speech-to-text (batch endpoint, ink-whisper model)."""
import os

import httpx

CARTESIA_STT_URL = "https://api.cartesia.ai/stt"
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-08-14")


class MissingApiKey(RuntimeError):
    pass


def _extension_for(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "mp4" in ct:
        return "mp4"
    if "ogg" in ct:
        return "ogg"
    if "wav" in ct:
        return "wav"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    return "webm"


async def transcribe(
    client: httpx.AsyncClient,
    audio: bytes,
    content_type: str = "audio/webm",
    language: str = "en",
) -> dict:
    """Send one audio clip to Cartesia. Returns the parsed JSON ({text, duration, ...}).

    Raises MissingApiKey if no key is configured, httpx.HTTPStatusError on non-2xx.
    """
    api_key = os.getenv("CARTESIA_API_KEY")
    if not api_key:
        raise MissingApiKey("CARTESIA_API_KEY is not set on the server.")

    # Strip codec params like "audio/webm;codecs=opus" -> "audio/webm"
    base_type = (
        content_type or "audio/webm").split(";")[0].strip() or "audio/webm"

    res = await client.post(
        CARTESIA_STT_URL,
        headers={"X-API-Key": api_key, "Cartesia-Version": CARTESIA_VERSION},
        files={"file": (f"clip.{_extension_for(base_type)}",
                        audio, base_type)},
        data={"model": "ink-whisper", "language": language},
    )
    res.raise_for_status()
    return res.json()
