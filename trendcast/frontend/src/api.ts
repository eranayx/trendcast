export interface SttResult {
    text: string;
    duration: number | null;
    /** Server-side time spent waiting on Cartesia, in ms. */
    ms: number;
}

export type Source = "reddit" | "youtube" | "instagram";
export type Timeframe = "today" | "week" | "month";
export type IntentKind = "trends" | "smalltalk" | "offtopic";

export interface Intent {
    kind: IntentKind;
    /** True only for kind "trends", the one case that needs data fetched. */
    supported: boolean;
    topic: string;
    sources: Source[];
    timeframe: Timeframe;
    /** Spoken reply for smalltalk and offtopic; empty for trends. */
    reply: string;
    /** Server-side time spent waiting on LLM, in ms. */
    ms: number;
}

export interface TrendItem {
    source: Source;
    title: string;
    url: string | null;
    /** The exact text that will be fed to the LLM for this post or video. */
    text: string;
}

export interface SourceResult {
    source: Source;
    cached: boolean;
    items: TrendItem[];
}

export interface Trends {
    sources: SourceResult[];
    /** Sources that failed, keyed by source name, with a readable reason. */
    errors: Record<string, string>;
    /** Server-side time spent fetching, in ms. */
    ms: number;
}

async function request<T>(url: string, init: RequestInit): Promise<T> {
    const res = await fetch(url, init);

    let body: unknown = null;
    try {
        body = await res.json();
    } catch {
        // non-JSON error body (e.g. proxy error); fall through to the generic message
    }

    if (!res.ok) {
        const message =
            body &&
            typeof body === "object" &&
            "error" in body &&
            typeof body.error === "string"
                ? body.error
                : `Request failed (${res.status}). Is the backend running on port 8000?`;
        throw new Error(message);
    }
    return body as T;
}

export function transcribe(audio: Blob): Promise<SttResult> {
    const form = new FormData();
    form.append("audio", audio, "clip");
    return request<SttResult>("/api/stt", { method: "POST", body: form });
}

export function parseIntent(text: string): Promise<Intent> {
    return request<Intent>("/api/intent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
    });
}

export function fetchTrends(
    intent: Pick<Intent, "sources" | "topic" | "timeframe">,
): Promise<Trends> {
    return request<Trends>("/api/trends", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            sources: intent.sources,
            topic: intent.topic,
            timeframe: intent.timeframe,
        }),
    });
}

export interface Summary {
    /** The spoken briefing, plain text ready for text-to-speech. */
    summary: string;
    /** Server-side time spent waiting on the LLM, in ms. */
    ms: number;
}

export function summarize(
    intent: Pick<Intent, "topic" | "timeframe">,
    trends: Trends,
): Promise<Summary> {
    return request<Summary>("/api/summarize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            topic: intent.topic,
            timeframe: intent.timeframe,
            // only the text the LLM needs; drop sources that came back empty
            sources: trends.sources
                .filter((s) => s.items.length > 0)
                .map((s) => ({
                    source: s.source,
                    items: s.items.map((i) => ({
                        title: i.title,
                        text: i.text,
                    })),
                })),
        }),
    });
}

export interface WordTiming {
    word: string;
    start: number;
    end: number;
}

function toWav(pcmChunks: Uint8Array[], sampleRate: number): Blob {
    const dataLength = pcmChunks.reduce((length, chunk) => length + chunk.length, 0);
    const pcm = new Uint8Array(dataLength);
    let offset = 0;
    for (const chunk of pcmChunks) {
        pcm.set(chunk, offset);
        offset += chunk.length;
    }

    const header = new ArrayBuffer(44);
    const view = new DataView(header);
    const writeText = (position: number, value: string) => {
        for (let i = 0; i < value.length; i++) {
            view.setUint8(position + i, value.charCodeAt(i));
        }
    };
    writeText(0, "RIFF");
    view.setUint32(4, 36 + dataLength, true);
    writeText(8, "WAVE");
    writeText(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeText(36, "data");
    view.setUint32(40, dataLength, true);

    return new Blob([header, pcm], { type: "audio/wav" });
}

export async function streamSpeech(
    text: string,
    audio: HTMLAudioElement,
    retainAudioUrl: (url: string) => void,
    setWordTimings: (words: WordTiming[]) => void,
    onAutoplayBlocked: () => void,
): Promise<void> {
    const res = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
    });

    if (!res.ok) {
        let body: unknown = null;
        try {
            body = await res.json();
        } catch {
            // non-JSON error body (e.g. proxy error); use the generic message
        }
        const message =
            body &&
            typeof body === "object" &&
            "error" in body &&
            typeof body.error === "string"
                ? body.error
                : `Text-to-speech failed (${res.status}).`;
        throw new Error(message);
    }

    if (!res.body) {
        throw new Error("The text-to-speech response did not include a stream.");
    }

    const pcmChunks: Uint8Array[] = [];
    const words: WordTiming[] = [];
    const decoder = new TextDecoder();
    const reader = res.body.getReader();
    let buffer = "";

    const consumeEvent = (frame: string) => {
        const data = frame
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trim())
            .join("\n");
        if (!data) return;

        let event: unknown;
        try {
            event = JSON.parse(data);
        } catch {
            throw new Error("Cartesia returned an invalid speech event.");
        }
        if (!event || typeof event !== "object" || !("type" in event)) return;

        if (event.type === "error") {
            const message =
                "message" in event && typeof event.message === "string"
                    ? event.message
                    : "Cartesia could not generate the briefing audio.";
            throw new Error(message);
        }

        if (
            event.type === "chunk" &&
            "data" in event &&
            typeof event.data === "string"
        ) {
            const binary = atob(event.data);
            const chunk = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) {
                chunk[i] = binary.charCodeAt(i);
            }
            pcmChunks.push(chunk);
        }

        if (
            event.type === "timestamps" &&
            "word_timestamps" in event &&
            event.word_timestamps &&
            typeof event.word_timestamps === "object"
        ) {
            const timing = event.word_timestamps;
            if (
                "words" in timing &&
                Array.isArray(timing.words) &&
                "start" in timing &&
                Array.isArray(timing.start) &&
                "end" in timing &&
                Array.isArray(timing.end)
            ) {
                const count = Math.min(
                    timing.words.length,
                    timing.start.length,
                    timing.end.length,
                );
                for (let i = 0; i < count; i++) {
                    const word = timing.words[i];
                    const start = timing.start[i];
                    const end = timing.end[i];
                    if (
                        typeof word === "string" &&
                        typeof start === "number" &&
                        Number.isFinite(start) &&
                        typeof end === "number" &&
                        Number.isFinite(end)
                    ) {
                        words.push({ word, start, end });
                    }
                }
            }
        }
    };

    while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
        let delimiter = buffer.indexOf("\n\n");
        while (delimiter !== -1) {
            consumeEvent(buffer.slice(0, delimiter));
            buffer = buffer.slice(delimiter + 2);
            delimiter = buffer.indexOf("\n\n");
        }
        if (done) break;
    }
    if (buffer.trim()) consumeEvent(buffer);

    if (!pcmChunks.length) {
        throw new Error("Cartesia returned no audio chunks.");
    }
    if (!words.length) {
        throw new Error("Cartesia returned no word timings for the briefing.");
    }

    const url = URL.createObjectURL(toWav(pcmChunks, 44100));
    retainAudioUrl(url);
    setWordTimings(words);
    audio.src = url;
    const canPlay = new Promise<void>((resolve, reject) => {
        audio.addEventListener("canplay", () => resolve(), { once: true });
        audio.addEventListener(
            "error",
            () => reject(new Error("The browser could not decode Cartesia's audio.")),
            { once: true },
        );
    });
    audio.load();
    await canPlay;

    try {
        await audio.play();
    } catch (e) {
        if (e instanceof DOMException && e.name === "NotAllowedError") {
            onAutoplayBlocked();
            return;
        }
        throw e;
    }
}