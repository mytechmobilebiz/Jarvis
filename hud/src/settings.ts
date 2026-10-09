export interface HudSettings {
  /**
   * The live gateway credential for THIS session. It is minted at spawn and
   * kept in memory only — never read from or written to settings.json. That is
   * the whole point: in 1.3.x this field could disagree with the profile .env
   * and the running gateway, which is the pairing-drift class (Kevin Dunn,
   * 2026-08-12). A value that cannot be persisted cannot go stale.
   */
  apiKey: string;
  /** The live gateway port for this session; also memory-only. */
  gatewayPort: number;
  stateServerPort: number;
  demoMode: boolean;
  kiosk: boolean;
  /** Start JARVIS at sign-in — the ONE auto-start entry the product owns. */
  startAtLogin: boolean;
  /** Speak chat replies aloud, using the OS voice if Kokoro is unavailable. */
  speakReplies: boolean;
}

export const DEFAULT_STATE_SERVER_PORT = 4870;

export const DEFAULT_SETTINGS: HudSettings = {
  apiKey: "",
  gatewayPort: 0,
  stateServerPort: DEFAULT_STATE_SERVER_PORT,
  demoMode: false,
  kiosk: false,
  startAtLogin: true,
  speakReplies: true,
};

/** The fields that live only in memory and must never reach settings.json. */
export const SESSION_ONLY_KEYS = ["apiKey", "gatewayPort"] as const;

export function normalizeSettings(raw: unknown): HudSettings {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ...DEFAULT_SETTINGS };
  }

  const source = raw as Record<string, unknown>;
  const stateServerPort =
    typeof source.stateServerPort === "number" ? source.stateServerPort : Number(source.stateServerPort);
  const port = typeof source.gatewayPort === "number" ? source.gatewayPort : Number(source.gatewayPort);

  return {
    // apiKey/gatewayPort are accepted from the in-memory patch the supervisor
    // applies, but persistSettings() strips them before any write.
    apiKey: typeof source.apiKey === "string" ? source.apiKey : DEFAULT_SETTINGS.apiKey,
    gatewayPort: Number.isInteger(port) && port > 0 && port < 65_536 ? port : DEFAULT_SETTINGS.gatewayPort,
    stateServerPort:
      Number.isInteger(stateServerPort) && stateServerPort > 0 && stateServerPort < 65_536
        ? stateServerPort
        : DEFAULT_SETTINGS.stateServerPort,
    demoMode: typeof source.demoMode === "boolean" ? source.demoMode : DEFAULT_SETTINGS.demoMode,
    kiosk: typeof source.kiosk === "boolean" ? source.kiosk : DEFAULT_SETTINGS.kiosk,
    startAtLogin:
      typeof source.startAtLogin === "boolean" ? source.startAtLogin : DEFAULT_SETTINGS.startAtLogin,
    speakReplies: typeof source.speakReplies === "boolean" ? source.speakReplies : DEFAULT_SETTINGS.speakReplies,
  };
}

/**
 * What actually gets written to settings.json. Dropping the session fields is
 * what makes a stale key structurally impossible: there is nowhere to keep one.
 * Migration from 1.3.x needs no code — the old apiKey line is simply never read
 * back and disappears on the next save.
 */
export function persistableSettings(settings: HudSettings): Omit<HudSettings, "apiKey" | "gatewayPort"> {
  const { apiKey: _apiKey, gatewayPort: _gatewayPort, ...rest } = settings;
  return rest;
}
