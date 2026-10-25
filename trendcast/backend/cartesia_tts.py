"""Cartesia text-to-speech streaming."""
import os
from collections.abc import AsyncIterator

import httpx

CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/sse"
CARTESIA_VERSION = os.getenv("CARTESIA_VERSION", "2026-08-14")


class MissingConfiguration(RuntimeError):
    pass


async def open_stream(client: httpx.AsyncClient, text: str) -> httpx.Response:
    """Open a Cartesia MP3 response stream after validating its HTTP status."""
    api_key = os.getenv("CARTESIA_API_KEY")
    if not api_key:
        raise MissingConfiguration("CARTESIA_API_KEY is not set on the server.")

    voice_id = os.getenv("CARTESIA_VOICE_ID")
    if not voice_id:
        raise MissingConfiguration("CARTESIA_VOICE_ID is not set on the server.")

    req = client.build_request(
        "POST",
        CARTESIA_TTS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Cartesia-Version": CARTESIA_VERSION,
        },
        json={
            "model_id": os.getenv("CARTESIA_TTS_MODEL", "sonic-3.6"),
            "transcript": text,
            "voice": voice_id,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": 44100,
            },
            "add_timestamps": True,
            "generation_config": {
                "speed": 1.15,
                "emotion": "excited",
            },
        },
    )
    response = await client.send(req, stream=True)
    if response.is_error:
        await response.aread()
    response.raise_for_status()
    return response


async def audio_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    finally:
        await response.aclose()
