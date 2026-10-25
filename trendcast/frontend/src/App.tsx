import { useCallback, useEffect, useRef, useState } from "react";
import {
    fetchTrends,
    parseIntent,
    summarize,
    streamSpeech,
    transcribe,
    type Intent,
    type Source,
    type Trends,
    type WordTiming,
} from "./api";
import { useRecorder } from "./hooks/useRecorder";
import "./css/intent.css";

type Phase =
    | "idle"
    | "transcribing"
    | "thinking"
    | "fetching"
    | "summarizing"
    | "speaking";

const MIN_CLIP_MS = 350;

const FETCHING_MESSAGES = [
    "Fetching the latest data...",
    "Looking for fresh conversations…",
    "Checking recent posts and videos…",
    "Finding the stories picking up momentum…",
    "Pulling together what people are discussing…",
];

const STATUS_BY_PHASE: Record<Phase, string> = {
    idle: "Ready",
    transcribing: "Transcribing…",
    thinking: "Working out what you asked…",
    fetching: FETCHING_MESSAGES[0],
    summarizing: "Writing your briefing…",
    speaking: "Loading your briefing audio…",
};

const SOURCE_LABELS: Record<Source, string> = {
    reddit: "Reddit",
    youtube: "YouTube",
    instagram: "Instagram",
};

export default function App() {
    const { recording, micError, start, stop } = useRecorder();
    const [phase, setPhase] = useState<Phase>("idle");
    const [fetchingMessageIndex, setFetchingMessageIndex] = useState(0);
    const [transcript, setTranscript] = useState("");
    const [intent, setIntent] = useState<Intent | null>(null);
    const [trends, setTrends] = useState<Trends | null>(null);
    const [summary, setSummary] = useState("");
    const [audioHint, setAudioHint] = useState("");
    const [wordTimings, setWordTimings] = useState<WordTiming[]>([]);
    const [activeWordIndex, setActiveWordIndex] = useState(-1);
    const [captionStart, setCaptionStart] = useState(0);
    const [showFullTranscript, setShowFullTranscript] = useState(false);
    const [meta, setMeta] = useState("");
    const [error, setError] = useState<string | null>(null);
    const audioRef = useRef<HTMLAudioElement>(null);
    const audioUrlRef = useRef<string | null>(null);

    const busy = phase !== "idle";

    useEffect(() => {
        if (phase !== "fetching") return;

        let timer: number;
        const rotateMessage = () => {
            setFetchingMessageIndex((current) => {
                const offset =
                    1 + Math.floor(Math.random() * (FETCHING_MESSAGES.length - 1));
                return (current + offset) % FETCHING_MESSAGES.length;
            });
            timer = window.setTimeout(
                rotateMessage,
                5000 + Math.random() * 5000,
            );
        };

        timer = window.setTimeout(rotateMessage, 5000 + Math.random() * 5000);
        return () => window.clearTimeout(timer);
    }, [phase]);

    const clearAudio = useCallback(() => {
        const audio = audioRef.current;
        audio?.pause();
        audio?.removeAttribute("src");
        audio?.load();
        if (audioUrlRef.current) {
            URL.revokeObjectURL(audioUrlRef.current);
            audioUrlRef.current = null;
        }
        setAudioHint("");
        setWordTimings([]);
        setActiveWordIndex(-1);
        setCaptionStart(0);
        setShowFullTranscript(false);
    }, []);

    const publishAudioUrl = useCallback((url: string) => {
        if (audioUrlRef.current && audioUrlRef.current !== url) {
            URL.revokeObjectURL(audioUrlRef.current);
        }
        audioUrlRef.current = url;
    }, []);

    const updateActiveWord = useCallback(() => {
        const audio = audioRef.current;
        if (!audio) return;
        let lastStartedIndex = -1;
        let activeIndex = -1;
        for (let i = 0; i < wordTimings.length; i++) {
            const timing = wordTimings[i];
            if (audio.currentTime < timing.start) break;
            lastStartedIndex = i;
            if (audio.currentTime < timing.end) activeIndex = i;
        }
        setActiveWordIndex(activeIndex);
        setCaptionStart(Math.max(0, Math.floor(lastStartedIndex / 6) * 6));
    }, [wordTimings]);

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
        setSummary("");
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
            let timings = `speech to text ${sttMs} ms · intent ${intentMs} ms`;
            setMeta(timings);
            let spokenText = parsed.reply;

            if (parsed.kind === "trends") {
                setFetchingMessageIndex(0);
                setPhase("fetching");
                const t2 = performance.now();
                const data = await fetchTrends(parsed);
                const dataMs = Math.round(performance.now() - t2);

                setTrends(data);
                timings += ` · data ${dataMs} ms`;
                setMeta(timings);

                setPhase("summarizing");
                const t3 = performance.now();
                const brief = await summarize(parsed, data);
                const summaryMs = Math.round(performance.now() - t3);

                setSummary(brief.summary);
                spokenText = brief.summary;
                timings += ` · summary ${summaryMs} ms`;
                setMeta(timings);
            } else {
                setSummary(spokenText);
            }

            setPhase("speaking");
            const audio = audioRef.current;
            if (!audio) throw new Error("The audio player is not ready.");
            setCaptionStart(0);
            setActiveWordIndex(-1);
            setShowFullTranscript(false);
            const t4 = performance.now();
            await streamSpeech(
                spokenText,
                audio,
                publishAudioUrl,
                setWordTimings,
                () =>
                    setAudioHint(
                        "Autoplay was blocked. Press play to listen.",
                    ),
            );
            timings += ` · speech ${Math.round(performance.now() - t4)} ms`;
            setMeta(timings);
        } catch (e) {
            setError(e instanceof Error ? e.message : "Something went wrong.");
        } finally {
            setPhase("idle");
        }
    }, [publishAudioUrl, stop]);

    const press = useCallback(() => {
        if (busy) return;
        clearAudio();
        setError(null);
        void start();
    }, [busy, clearAudio, start]);

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

    useEffect(
        () => () => {
            audioRef.current?.pause();
            if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
        },
        [],
    );

    const label = recording ? "Listening…" : busy ? "Working…" : "Hold to talk";
    const status = micError ??
        error ??
        (recording
            ? "Recording"
            : phase === "fetching"
              ? FETCHING_MESSAGES[fetchingMessageIndex]
              : STATUS_BY_PHASE[phase]);
    const isError = Boolean(micError ?? error);
    const visibleWords = wordTimings.slice(captionStart, captionStart + 6);

    return (
        <main className="app">
            <header className="hero">
                <div className="eyebrow">Your voice-powered trend briefing</div>
                <h1>TrendCast</h1>
                <p className="sub">
                    Get a quick, spoken snapshot of what tech-related things people
                    are talking about across Reddit, YouTube, and Instagram.
                </p>
            </header>

            <section className="how-to" aria-label="How to use TrendCast">
                <div className="how-to-heading">
                    <span className="how-to-icon" aria-hidden="true">✦</span>
                    <div>
                        <div className="how-to-title">How to get your briefing</div>
                        <div className="how-to-subtitle">
                            Ask naturally. TrendCast handles the rest.
                        </div>
                    </div>
                </div>
                <ol className="steps">
                    <li>
                        <span className="step-number">01</span>
                        <span><strong>Press and hold</strong> the mic or hold the spacebar.</span>
                    </li>
                    <li>
                        <span className="step-number">02</span>
                        <span><strong>Ask a question</strong> and mention a topic or timeframe
                        (today, this week, this month) if you like.</span>
                    </li>
                    <li>
                        <span className="step-number">03</span>
                        <span><strong>Let go</strong> to get your spoken briefing with synced captions.</span>
                    </li>
                </ol>
                <p className="prompt-example">
                    Try: <span>“What’s trending in AI this week?”</span>
                </p>
            </section>

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
                <div className="label">You said</div>
                {transcript || "Your transcript will appear here."}
            </div>

            {intent?.supported && (
                <section className="intent" aria-label="Parsed request">
                    <div className="label">Looking up</div>
                    <div className="topic">{intent.topic}</div>
                    <div className="chips">
                        {intent.sources.map((s) => (
                            <span key={s} className="chip">
                                {s}
                            </span>
                        ))}
                        <span className="chip time">{intent.timeframe}</span>
                    </div>
                </section>
            )}

            {summary && (
                <section className="summary" aria-label="Your briefing">
                    <div className="label">
                        {intent?.kind === "trends"
                            ? "Your briefing"
                            : "TrendCast says"}
                    </div>
                    <div
                        className={`captions${showFullTranscript ? " full-transcript" : ""}`}
                        aria-label={
                            showFullTranscript
                                ? "Full briefing transcript"
                                : "Live captions"
                        }>
                        {showFullTranscript
                            ? summary
                            : wordTimings.length
                            ? visibleWords.map((timing, offset) => {
                                  const index = captionStart + offset;
                                  return (
                                      <span
                                          key={`${timing.start}-${index}`}
                                          className={
                                              index === activeWordIndex
                                                  ? "caption-word active"
                                                  : "caption-word"
                                          }>
                                          {timing.word}{" "}
                                      </span>
                                  );
                              })
                            : <span className="captions-loading">Preparing your live captions…</span>}
                    </div>
                    {wordTimings.length > 0 && (
                        <button
                              className="transcript-toggle"
                              type="button"
                              aria-expanded={showFullTranscript}
                              onClick={() =>
                                  setShowFullTranscript((expanded) => !expanded)
                              }>
                              {showFullTranscript
                                  ? "Show live captions"
                                  : "Show full transcript"}
                        </button>
                    )}
                    {audioHint && <div className="note">{audioHint}</div>}
                </section>
            )}

            <audio
                className="briefing-audio"
                ref={audioRef}
                controls
                hidden={!summary}
                onTimeUpdate={updateActiveWord}
                aria-label="Listen to your briefing"
            />

            {trends && (
                <details className="trends" aria-label="Sources used">
                    <summary>Sources used</summary>
                    {trends.sources.map((s) => (
                        <div key={s.source} className="source-block">
                            <div className="label">
                                {SOURCE_LABELS[s.source]}
                                {s.cached ? " · cached" : ""}
                                {` · ${s.items.reduce((n, i) => n + i.text.length, 0).toLocaleString()} characters for the LLM`}
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
                                        <pre className="trend-text">
                                            {item.text}
                                        </pre>
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
                </details>
            )}

            <div className="meta">{meta}</div>
        </main>
    );
}
