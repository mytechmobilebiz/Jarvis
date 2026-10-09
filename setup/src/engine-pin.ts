/**
 * The engine root of trust, COMPILED INTO the setup bundle.
 *
 * hud/vendor/engine-pin.json is the source of truth for the build and release
 * verifiers; this is the same identity baked into the installed app so the
 * runtime can enforce it too (review r3, finding 9). It is deliberately a
 * TypeScript constant rather than a JSON import: the value must ship inside
 * the asar as immutable code, not as a file on disk beside the payload that an
 * attacker replacing the payload could replace as well.
 *
 * `engine-pin.test.ts` fails if this drifts from hud/vendor/engine-pin.json,
 * so the two can never disagree.
 */
export interface EnginePin {
  tag: string;
  commit: string;
  tarballSha256: string;
}

export const ENGINE_PIN: EnginePin = {
  tag: "v2026.7.1",
  commit: "7c1a029553d87c43ecff8a3821336bc95872213b",
  tarballSha256: "ec8a380629cc2f3f2102dd92cad50c4ded706fe59c0e359a05681166a0ae2991",
};

/** Source kinds a shipped payload may claim. */
const TRUSTED_KINDS = new Set(["git-tag-clean", "tarball-sha256"]);

export interface ProvenanceProblem {
  reason: string;
}

/**
 * Checks a payload's recorded provenance against the compiled-in pin. Returns
 * undefined when the payload is trustworthy.
 */
export function checkProvenance(
  source: { kind: string; commit?: string; tarballSha256?: string } | undefined,
  pin: EnginePin = ENGINE_PIN,
): ProvenanceProblem | undefined {
  if (source === undefined) {
    return { reason: "The bundled engine payload does not record where its engine came from." };
  }
  if (!TRUSTED_KINDS.has(source.kind)) {
    return { reason: `The bundled engine payload was built from an untrusted source (${source.kind}).` };
  }
  if (source.kind === "git-tag-clean") {
    if (source.commit !== pin.commit) {
      return {
        reason: `The bundled engine is commit ${short(source.commit)}, but this build of JARVIS expects ${short(pin.commit)}.`,
      };
    }
    return undefined;
  }
  // tarball-sha256
  if (pin.tarballSha256 !== "" && source.tarballSha256 !== pin.tarballSha256) {
    return {
      reason: `The bundled engine archive does not match the one this build of JARVIS expects.`,
    };
  }
  if (source.commit !== undefined && source.commit !== "" && source.commit !== pin.commit) {
    return {
      reason: `The bundled engine is commit ${short(source.commit)}, but this build of JARVIS expects ${short(pin.commit)}.`,
    };
  }
  return undefined;
}

function short(value: string | undefined): string {
  return value === undefined || value === "" ? "unknown" : value.slice(0, 12);
}
