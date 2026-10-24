import intent as intent_stage
import cartesia_stt
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, File, Form, Request, UploadFile
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()


MAX_AUDIO_BYTES = 25 * 1024 * 1024

if not os.getenv("CARTESIA_API_KEY"):
    print("WARNING: CARTESIA_API_KEY is not set. Copy .env.example to .env and fill it in.")
if not os.getenv("LLM_API_KEY"):
    print("WARNING: LLM_API_KEY is not set. /api/intent will fail until you add it to .env.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared client: keeps TLS connections warm so calls after the first are faster.
    app.state.http = httpx.AsyncClient(timeout=30.0)
    yield
    await app.state.http.aclose()


app = FastAPI(title="Trendcast", lifespan=lifespan)

# The Vite dev server proxies /api, so CORS only matters if you call the backend directly.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> Any:
    return {
        "ok": True,
        "cartesiaKeySet": bool(os.getenv("CARTESIA_API_KEY")),
        "llmKeySet": bool(os.getenv("LLM_API_KEY")),
    }


@app.post("/api/stt")
async def stt(request: Request, audio: UploadFile = File(...), language: str = Form("en")) -> Any:
    data = await audio.read()
    if not data:
        return JSONResponse({"error": "No audio received."}, status_code=400)
    if len(data) > MAX_AUDIO_BYTES:
        return JSONResponse({"error": "Audio too large."}, status_code=413)

    started = time.perf_counter()
    try:
        result = await cartesia_stt.transcribe(
            request.app.state.http, data, audio.content_type or "audio/webm", language
        )
    except cartesia_stt.MissingApiKey as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except httpx.HTTPStatusError as e:
        detail = e.response.text
        print("Cartesia STT error", e.response.status_code, detail)
        return JSONResponse(
            {"error": f"Cartesia returned {e.response.status_code}", "detail": detail},
            status_code=502,
        )
    except httpx.HTTPError as e:
        print("Cartesia STT network error:", repr(e))
        return JSONResponse({"error": "Could not reach Cartesia."}, status_code=502)

    return {
        "text": (result.get("text") or "").strip(),
        "duration": result.get("duration"),
        "ms": round((time.perf_counter() - started) * 1000),
    }


class IntentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@app.post("/api/intent")
async def parse_intent_route(request: Request, body: IntentRequest) -> Any:
    started = time.perf_counter()
    try:
        result = await intent_stage.parse_intent(request.app.state.http, body.text)
    except intent_stage.MissingApiKey as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except httpx.HTTPStatusError as e:
        detail = e.response.text
        print("LLM error", e.response.status_code, detail)
        return JSONResponse(
            {"error": f"LLM provider returned {e.response.status_code}", "detail": detail},
            status_code=502,
        )
    except httpx.HTTPError as e:
        print("LLM network error:", repr(e))
        return JSONResponse({"error": "Could not reach the LLM provider."}, status_code=502)
    except intent_stage.LLMError as e:
        print("LLM unusable response:", e)
        return JSONResponse({"error": str(e)}, status_code=502)

    return {**result, "ms": round((time.perf_counter() - started) * 1000)}
