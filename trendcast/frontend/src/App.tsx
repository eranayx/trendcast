import { useCallback, useEffect, useState } from "react";
import {
    fetchTrends,
    parseIntent,
    transcribe,
    type Intent,
    type Source,
    type Trends,
} from "./api";
import { useRecorder } from "./hooks/useRecorder";
import "./css/intent.css";

type Phase = "idle" | "transcribing" | "thinking" | "fetching";

const MIN_CLIP_MS = 350;

const STATUS_BY_PHASE: Record<Phase, string> = {
    idle: "Ready",
    transcribing: "Transcribing…",
    thinking: "Working out what you asked…",
    fetching: "Pulling the latest data…",
};

const SOURCE_LABELS: Record<Source, string> = {
    reddit: "Reddit",
    youtube: "YouTube",
    tiktok: "TikTok",
    instagram: "Instagram",
};

export default function App() {
    const { recording, micError, start, stop } = useRecorder();
    const [phase, setPhase] = useState<Phase>("idle");
    const [transcript, setTranscript] = useState("");
    const [intent, setIntent] = useState<Intent | null>(null);
    const [trends, setTrends] = useState<Trends | null>(null);
    const [meta, setMeta] = useState("");
    const [error, setError] = useState<string | null>(null);

    const busy = phase !== "idle";

    const release = useCallback(async () => {
        const clip = await stop();
        if (!clip) return;
        if (clip.durationMs < MIN_CLIP_MS) {
            setError("Too short. Hold the button a little longer.");
            return;
        }

        setError(null);
        setIntent(null);
        setTrends(null);
        setMeta("");
        setPhase("transcribing");
        try {
            const t0 = performance.now();
            const stt = await transcribe(clip.blob);
            const sttMs = Math.round(performance.now() - t0);

            setTranscript(stt.text);
            if (!stt.text) {
                setError("Didn't catch anything. Try again.");
                return;
            }

            setPhase("thinking");
            const t1 = performance.now();
            const parsed = await parseIntent(stt.text);
            const intentMs = Math.round(performance.now() - t1);

            setIntent(parsed);
            let summary = `speech to text ${sttMs} ms · intent ${intentMs} ms`;
            setMeta(summary);

            if (parsed.kind === "trends") {
                setPhase("fetching");
                const t2 = performance.now();
                const data = await fetchTrends(parsed);
                const dataMs = Math.round(performance.now() - t2);

                setTrends(data);
                summary += ` · data ${dataMs} ms`;
                setMeta(summary);
                // Next stage hooks in here: send `data` to the LLM to write a spoken summary.
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : "Something went wrong.");
        } finally {
            setPhase("idle");
        }
    }, [stop]);

    const press = useCallback(() => {
        if (busy) return;
        setError(null);
        void start();
    }, [busy, start]);

    // Hold the spacebar to talk.
    useEffect(() => {
        const down = (e: KeyboardEvent) => {
            if (e.code !== "Space" || e.repeat) return;
            e.preventDefault();
            press();
        };
        const up = (e: KeyboardEvent) => {
            if (e.code !== "Space") return;
            e.preventDefault();
            void release();
        };
        window.addEventListener("keydown", down);
        window.addEventListener("keyup", up);
        return () => {
            window.removeEventListener("keydown", down);
            window.removeEventListener("keyup", up);
        };
    }, [press, release]);

    const label = recording ? "Listening…" : busy ? "Working…" : "Hold to talk";
    const status =
        micError ?? error ?? (recording ? "Recording" : STATUS_BY_PHASE[phase]);
    const isError = Boolean(micError ?? error);
    
    return (
        <main className="app">
            <h1>TrendCast</h1>
            <p className="sub">
                Hold the button (or the spacebar) and ask what&apos;s trending.
            </p>

            <button
                className={`mic${recording ? " rec" : ""}`}
                disabled={busy}
                aria-label="Hold to talk"
                onPointerDown={(e) => {
                    e.preventDefault();
                    e.currentTarget.setPointerCapture(e.pointerId);
                    press();
                }}
                onPointerUp={() => void release()}
                onPointerCancel={() => void release()}>
                {label}
            </button>

            <div className={`status${isError ? " error" : ""}`} role="status">
                {status}
            </div>

            <div className={`transcript${transcript ? "" : " empty"}`}>
                {transcript || "Your transcript will appear here."}
            </div>

            {intent && (
                <section className="intent" aria-label="Parsed request">
                    {intent.supported ? (
                        <>
                            <div className="label">Looking up</div>
                            <div className="topic">{intent.topic}</div>
                            <div className="chips">
                                {intent.sources.map((s) => (
                                    <span key={s} className="chip">
                                        {s}
                                    </span>
                                ))}
                                <span className="chip time">
                                    {intent.timeframe}
                                </span>
                            </div>
                        </>
                    ) : (
                        <>
                            <div className="label">
                                {intent.kind === "smalltalk"
                                    ? "TrendCast says"
                                    : "Can\u2019t help with that"}
                            </div>
                            <div className="unsupported">{intent.reply}</div>
                        </>
                    )}
                </section>
            )}

            {trends && (
                <section className="trends" aria-label="Trending now">
                    {trends.sources.map((s) => (
                        <div key={s.source} className="source-block">
                            <div className="label">
                                {SOURCE_LABELS[s.source]}
                                {s.cached ? " · cached" : ""}
                            </div>
                            
                            {s.items.length === 0 && (
                                <div className="note">No results.</div>
                            )}
                            <ol className="trend-list">
                                {s.items.map((item) => (
                                    <li
                                        key={`${item.source}-${item.url ?? item.title}`}>
                                        {item.url ? (
                                            <a
                                                className="trend-title"
                                                href={item.url}
                                                target="_blank"
                                                rel="noreferrer">
                                                {item.title}
                                            </a>
                                        ) : (
                                            <span className="trend-title">
                                                {item.title}
                                            </span>
                                        )}
                                        {(item.score !== null ||
                                            item.comments !== null) && (
                                            <div className="trend-meta">
                                                {item.score !== null &&
                                                    `${item.score.toLocaleString()} ${item.score_label}`}
                                                {item.score !== null &&
                                                    item.comments !== null &&
                                                    " · "}
                                                {item.comments !== null &&
                                                    `${item.comments.toLocaleString()} comments`}
                                            </div>
                                        )}
                                        {item.description && (
                                            <div className="trend-desc">
                                                {item.description}
                                            </div>
                                        )}
                                    </li>
                                ))}
                            </ol>
                        </div>
                    ))}
                    {Object.entries(trends.errors).map(([source, message]) => (
                        <div key={source} className="note error">
                            {SOURCE_LABELS[source as Source] ?? source}:{" "}
                            {message}
                        </div>
                    ))}
                </section>
            )}

            <div className="meta">{meta}</div>
        </main>
    );
}
