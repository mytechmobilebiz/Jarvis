/**
 * Streaming reply text -> speakable sentence chunks.
 *
 * Kokoro is materially worse on fragments below ~10 tokens (see
 * spec/v140/voice-codex-report.md), and synthesizing raw SSE deltas would hand
 * it two-word scraps. So tokens are buffered until a sentence actually ends, or
 * until the buffer grows past a soft word cap, and only whole clauses are
 * handed to TTS.
 *
 * Pure and synchronous on purpose: the queue, the barge-in logic and the child
 * process all sit downstream, and this is the piece that most needs to be
 * cheaply testable.
 */

/** Soft cap in whitespace-separated words before a chunk is forced out. */
export const MAX_CHUNK_WORDS = 80;
/**
 * Below this a fragment is held back and merged into the next chunk. Three,
 * not more: "Good morning, sir." is a legitimate opening line and delaying it
 * would cost the thing a listener notices most, time to first word. It is the
 * one- and two-word scraps ("Yes.", "Of course.") that Kokoro renders badly.
 */
export const MIN_CHUNK_WORDS = 3;

// Includes the CJK full stop, ideographic and fullwidth marks: a Unicode
// stream never reaches a word cap, so its own terminators must be recognised.
const SENTENCE_END = /[.!?…。！？][")'”’)\]]*$/u;

/**
 * Hard ceiling in CHARACTERS. The word cap cannot fire on scripts that do not
 * use spaces, so without this a long CJK reply would buffer to the end of the
 * stream and be spoken as one enormous utterance — or not at all.
 */
export const MAX_CHUNK_CHARS = 400;

/**
 * Tokens that end in a period but do not end a sentence. Deliberately short:
 * a missed split costs one slightly long chunk, an over-eager split costs an
 * audibly clipped sentence.
 */
const ABBREVIATIONS = new Set([
  "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
  "e.g.", "i.e.", "etc.", "vs.", "approx.", "no.", "inc.", "ltd.", "co.",
  "jan.", "feb.", "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "sept.", "oct.", "nov.", "dec.",
  "a.m.", "p.m.", "u.s.", "u.k.",
]);

export interface SentenceChunker {
  /** Feed a streamed token. Returns any chunks that became speakable. */
  push(token: string): string[];
  /** End of stream: returns whatever is left, however short. */
  flush(): string[];
  /** Drop buffered text without emitting it (barge-in). */
  reset(): void;
}

export function createSentenceChunker(
  options: { maxWords?: number; minWords?: number; maxChars?: number } = {},
): SentenceChunker {
  const maxWords = options.maxWords ?? MAX_CHUNK_WORDS;
  const minWords = options.minWords ?? MIN_CHUNK_WORDS;
  const maxChars = options.maxChars ?? MAX_CHUNK_CHARS;
  let buffer = "";
  /**
   * Whether the stream is currently inside a ``` fence. speakable() removes
   * complete fenced blocks, but it only ever sees one chunk at a time — so a
   * full stop INSIDE an unterminated fence would ship a line of code to the
   * voice before the closing fence ever arrived. Nothing is emitted while a
   * fence is open.
   */
  let inFence = false;

  function take(): string {
    const chunk = buffer.trim();
    buffer = "";
    return chunk;
  }

  return {
    push(token: string): string[] {
      buffer += token;
      const chunks: string[] = [];

      // Fence state is derived from the BUFFER, not from this token. Counting
      // delimiters per token misses a fence split across an SSE boundary
      // ("`" + "``"), which is common and leaked code in review.
      inFence = countFences(buffer) % 2 === 1;
      // Inside an open fence, hold everything: the closing fence decides what
      // is code and what is prose.
      if (inFence) return chunks;
      // The fence has closed (or never opened). Remove complete fenced blocks
      // from the BUFFER before looking for sentence boundaries — otherwise a
      // full stop inside the block splits it, and the resulting chunk carries
      // a half-fence that speakable() can no longer recognise as code.
      if (buffer.includes("```")) buffer = buffer.replace(/```[\s\S]*?```/g, " Code block omitted. ");

      if ((buffer.match(/`/g) ?? []).length % 2 === 1) return chunks;

      // A sentence is only complete once the NEXT character has arrived —
      // otherwise "3." in "3.5%" would ship as its own utterance.
      for (;;) {
        // A too-short candidate does not stop the scan, it extends it: "Yes."
        // is absorbed into "Yes. The invoice is paid in full." rather than
        // holding the whole reply hostage until the stream ends.
        let boundary = -1;
        let from = 0;
        for (;;) {
          const next = findSentenceBoundary(buffer, from);
          if (next === -1) break;
          if (countWords(buffer.slice(0, next)) >= minWords) {
            boundary = next;
            break;
          }
          // Past the boundary, not at it: index `next` is itself the
          // whitespace that terminated the sentence, so resuming there would
          // rediscover the same boundary forever.
          from = next + 1;
        }
        if (boundary === -1) break;
        chunks.push(buffer.slice(0, boundary).trim());
        buffer = buffer.slice(boundary);
      }

      if (countWords(buffer) >= maxWords) {
        const split = softSplit(buffer, maxWords);
        if (split !== null) {
          chunks.push(split.chunk);
          buffer = split.rest;
        }
      }

      // Character backstop for space-free scripts, where the word cap above
      // can never trigger. Split on the last terminator or punctuation in
      // range, else hard-cut at the limit.
      while ([...buffer].length >= maxChars) {
        const graphemes = [...buffer];
        const window = graphemes.slice(0, maxChars);
        let cut = window.length;
        for (let i = window.length - 1; i >= Math.floor(maxChars / 2); i -= 1) {
          if (/[.!?…。！？,;:、，]/u.test(window[i])) {
            cut = i + 1;
            break;
          }
        }
        chunks.push(graphemes.slice(0, cut).join("").trim());
        buffer = graphemes.slice(cut).join("");
      }

      return chunks.map((chunk) => speakable(chunk.replace(/`[^`]*`/g, ""))).filter((chunk) => chunk !== "");
    },
    flush(): string[] {
      // An unterminated fence at end of stream stays SILENT. Speaking it would
      // read raw source aloud, which is worse than saying nothing; the text is
      // on screen either way. Prose before the fence is still spoken.
      const remaining = take();
      inFence = false;
      const opensFence = remaining.indexOf("```");
      const speakablePart = opensFence === -1 ? remaining : remaining.slice(0, opensFence);
      const rest = speakable(speakablePart.replace(/`[^`]*(?:`|$)/g, ""));
      return rest === "" ? [] : [rest];
    },
    reset(): void {
      buffer = "";
      inFence = false;
    },
  };
}

/**
 * Index just past a sentence terminator that is followed by whitespace, or -1.
 * Requiring the trailing whitespace is what makes decimals, version numbers and
 * "jarvis-agent.tech" survive intact.
 */
function findSentenceBoundary(text: string, start = 0): number {
  for (let i = Math.max(start, 0); i < text.length; i += 1) {
    if (!/\s/.test(text[i])) continue;
    const head = text.slice(0, i);
    if (!SENTENCE_END.test(head)) continue;
    if (endsWithAbbreviation(head)) continue;
    if (/\s[A-Z]\.$/.test(head)) continue; // middle initials: "Kevin D. Dunn"
    return i;
  }
  return -1;
}

function endsWithAbbreviation(head: string): boolean {
  const lastWord = head.split(/\s+/).at(-1)?.toLowerCase() ?? "";
  return ABBREVIATIONS.has(lastWord);
}

/**
 * Forced split for a run-on with no punctuation. Prefers a comma/semicolon so
 * the break lands somewhere a listener forgives; falls back to the word cap.
 */
function softSplit(text: string, maxWords: number): { chunk: string; rest: string } | null {
  const words = text.trim().split(/\s+/);
  if (words.length < maxWords) return null;

  const window = words.slice(0, maxWords);
  let cut = window.length;
  for (let i = window.length - 1; i >= Math.floor(maxWords / 2); i -= 1) {
    if (/[,;:]$/.test(window[i])) {
      cut = i + 1;
      break;
    }
  }
  return {
    chunk: words.slice(0, cut).join(" "),
    rest: words.slice(cut).join(" ") + (/\s$/.test(text) ? " " : ""),
  };
}

/** Total ``` delimiters in the text so far. */
function countFences(text: string): number {
  return (text.match(/```/g) ?? []).length;
}

function countWords(text: string): number {
  const trimmed = text.trim();
  return trimmed === "" ? 0 : trimmed.split(/\s+/).length;
}

/** Flatten brief markdown and punctuation before synthesis. */
export function speakable(text: string): string {
  return text
    .replace(/```[\s\S]*?```/g, " code block omitted. ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " image. ")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/\bhttps?:\/\/\S+/gi, " a link ")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}(?:[-*+]|\d+[.)])\s+/gm, "")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/(\*|_)(\S(?:.*?\S)?)\1/g, "$2")
    .replace(/[|>]/g, " ")
    .replace(/^[ \t]*\.[ \t]*$/gm, "")
    .replace(/(?<!\.)\.{3}(?!\.)|…/g, ",")
    .replace(/\.{2,}/g, "")
    .replace(/\d+(?:\.\d+){2,}\b/g, (version) => version.replace(/\./g, " point "))
    .replace(/\be\.g\./gi, "for example")
    .replace(/\bi\.e\./gi, "that is")
    .replace(/\betc\./gi, "and so on")
    .replace(/\s+/g, " ")
    .trim();
}


export const SPOKEN_REPLY_INSTRUCTION = "Start every reply with <speak>one or two short spoken sentences summarising your answer, no markdown, no lists, no URLs</speak>, then write your full reply.";

/** Hide streaming gists; final/history renders preserve a gist-only answer. */
export function stripSpeak(text: string, streaming = false): string {
  const markers = /<\/?speak>|<\/?(?:s(?:p(?:e(?:a(?:k)?)?)?)?)?$/gi;
  if (streaming) return text.replace(/<speak>[\s\S]*?(?:<\/speak>|$)/gi, "").replace(markers, "").trimStart();
  const withoutBlocks = text.replace(/<speak>[\s\S]*?<\/speak>/gi, "").replace(markers, "");
  return (withoutBlocks.trim() ? withoutBlocks : text.replace(markers, "")).trimStart();
}

/** Speak each closed gist, or two short sentences when a model ignores the tags. */
export function createReplyChunker(): SentenceChunker {
  const sentences = createSentenceChunker({ minWords: 1 });
  let text = "";
  let mode: "prefix" | "gist" | "fallback" | "done" = "prefix";
  let count = 0;
  let chars = 0;
  function limit(chunks: string[]): string[] {
    const result: string[] = [];
    for (const chunk of chunks) {
      if (count >= 2 || chars >= 300) break;
      const clean = speakable(chunk).replace(/`[^`]*`/g, "")
        .replace(/\b[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\b|\b[a-f0-9]{8,}\b/gi, "").slice(0, 300 - chars).trim();
      if (clean) { result.push(clean); chars += clean.length; count++; }
    }
    if (mode === "fallback" && (count >= 2 || chars >= 300)) mode = "done";
    return result;
  }
  function push(token: string): string[] {
    if (mode === "done") return [];
    text += token;
    if (mode === "prefix") {
      const head = text.trimStart();
      if ("<speak>".startsWith(head.toLowerCase())) return [];
      mode = head.toLowerCase().startsWith("<speak>") ? "gist" : "fallback";
      if (mode === "fallback") return limit(sentences.push(text));
    } else if (mode === "fallback") return limit(sentences.push(token));
    const chunks: string[] = [];
    let block: RegExpExecArray | null;
    while ((block = /<speak>([\s\S]*?)<\/speak>/i.exec(text)) !== null) {
      text = text.slice(block.index + block[0].length);
      sentences.reset();
      count = 0;
      chars = 0;
      chunks.push(...limit([...sentences.push(block[1]), ...sentences.flush()]));
    }
    return chunks;
  }
  return {
    push,
    flush() {
      if (mode === "prefix") {
        if (text.trimStart().startsWith("<")) return [];
        mode = "fallback";
        return limit([...sentences.push(text), ...sentences.flush()]);
      }
      return mode === "fallback" ? limit(sentences.flush()) : [];
    },
    reset() { text = ""; mode = "prefix"; count = 0; chars = 0; sentences.reset(); },
  };
}
