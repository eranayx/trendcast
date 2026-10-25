import { useCallback, useEffect, useRef, useState } from "react";

export interface Recording {
    blob: Blob;
    durationMs: number;
}

const MIME_CANDIDATES = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4; codecs=mp4a.40.2",
    "audio/ogg;codecs=opus",
];

function pickMimeType(): string | undefined {
    if (typeof MediaRecorder === "undefined") return undefined;
    return MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported(m));
}

/**
 * Push-to-talk recorder.
 *   start(): begin capturing (asks for mic permission the first time)
 *   stop():  finish and resolve with the clip, or null if nothing usable was captured
 */
export function useRecorder() {
    const [recording, setRecording] = useState(false);
    const [micError, setMicError] = useState<string | null>(null);

    const streamRef = useRef<MediaStream | null>(null);
    const recorderRef = useRef<MediaRecorder | null>(null);
    const chunksRef = useRef<Blob[]>([]);
    const startedAtRef = useRef(0);
    const startingRef = useRef(false);
    const stopRequestedRef = useRef(false);

    const start = useCallback(async () => {
        if (recorderRef.current || startingRef.current) return;
        startingRef.current = true;
        stopRequestedRef.current = false;
        setMicError(null);

        try {
            if (!streamRef.current) {
                streamRef.current = await navigator.mediaDevices.getUserMedia({
                    audio: {
                        channelCount: 1,
                        echoCancellation: true,
                        noiseSuppression: true,
                        autoGainControl: true,
                    },
                });
            }
        } catch {
            setMicError(
                "Microphone access was blocked. Allow it in your browser and try again.",
            );
            startingRef.current = false;
            return;
        }

        // The user may have released the button while the permission prompt was open.
        if (stopRequestedRef.current) {
            startingRef.current = false;
            return;
        }

        const mimeType = pickMimeType();
        const recorder = new MediaRecorder(
            streamRef.current,
            mimeType ? { mimeType } : undefined,
        );
        chunksRef.current = [];
        recorder.ondataavailable = (e) => {
            if (e.data.size > 0) chunksRef.current.push(e.data);
        };
        recorder.start();
        recorderRef.current = recorder;
        startedAtRef.current = performance.now();
        startingRef.current = false;
        setRecording(true);
    }, []);

    const stop = useCallback((): Promise<Recording | null> => {
        stopRequestedRef.current = true;
        const recorder = recorderRef.current;
        if (!recorder) return Promise.resolve(null);

        recorderRef.current = null;
        setRecording(false);

        return new Promise((resolve) => {
            recorder.onstop = () => {
                const durationMs = performance.now() - startedAtRef.current;
                const blob = new Blob(chunksRef.current, {
                    type: recorder.mimeType || "audio/webm",
                });
                resolve(blob.size > 0 ? { blob, durationMs } : null);
            };
            recorder.stop();
        });
    }, []);

    // Release the microphone when the component unmounts.
    useEffect(() => {
        return () => {
            streamRef.current?.getTracks().forEach((t) => t.stop());
            streamRef.current = null;
        };
    }, []);

    return { recording, micError, start, stop };
}
