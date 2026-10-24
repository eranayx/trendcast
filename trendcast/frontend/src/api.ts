export interface SttResult {
    text: string;
    duration: number | null;
    /** Server-side time spent waiting on Cartesia, in ms. */
    ms: number;
}

export type Source = "hackernews" | "reddit" | "github" | "producthunt";
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
    /** Server-side time spent waiting on o4-mini, in ms. */
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
