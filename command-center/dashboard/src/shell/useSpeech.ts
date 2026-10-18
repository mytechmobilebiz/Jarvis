import { useCallback, useEffect, useRef, useState } from "react";

/* The Web Speech API is not in TypeScript's DOM library; this is the part used here. */
interface RecognitionResult {
  isFinal: boolean;
  0: { transcript: string };
}
interface RecognitionEvent {
  resultIndex: number;
  results: ArrayLike<RecognitionResult>;
}
interface Recognition {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((e: RecognitionEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
type RecognitionCtor = new () => Recognition;

function recognitionCtor(): RecognitionCtor | null {
  const w = window as unknown as { SpeechRecognition?: RecognitionCtor; webkitSpeechRecognition?: RecognitionCtor };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

/** Whether voice input can work here: the browser has speech recognition and the page may use the microphone. */
export function speechSupported(): boolean {
  if (typeof window === "undefined" || !recognitionCtor() || !window.isSecureContext) return false;
  const policy = (document as unknown as { permissionsPolicy?: { allowsFeature(f: string): boolean } }).permissionsPolicy;
  return policy ? policy.allowsFeature("microphone") : true;
}

const ERRORS: Record<string, string> = {
  "not-allowed": "Microphone access was blocked. Allow it for this site in the browser's address bar.",
  "service-not-allowed": "This browser does not allow speech recognition here.",
  "no-speech": "No speech was heard. Try again.",
  "audio-capture": "No microphone was found.",
  network: "The browser's speech service could not be reached.",
};

/** Dictation into a text field: the transcript only fills the field; nothing is sent until the user presses Execute. */
export function useSpeech(onText: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  useEffect(() => () => rec.current?.abort(), []);

  const start = useCallback(() => {
    const Ctor = recognitionCtor();
    if (!Ctor) return;
    setError(null);
    const r = new Ctor();
    r.lang = navigator.language || "en-US";
    r.interimResults = true;
    r.continuous = false;
    r.onresult = (e) => {
      let text = "";
      for (let i = 0; i < e.results.length; i++) text += e.results[i][0].transcript;
      onTextRef.current(text.trim());
    };
    r.onerror = (e) => setError(ERRORS[e.error] ?? `Voice input stopped (${e.error}).`);
    r.onend = () => setListening(false);
    rec.current = r;
    try {
      r.start();
      setListening(true);
    } catch {
      setError("Voice input could not start.");
    }
  }, []);

  const stop = useCallback(() => rec.current?.stop(), []);
  return { listening, error, start, stop };
}
