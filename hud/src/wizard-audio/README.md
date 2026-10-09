# Wizard audio cues

Pre-generated JARVIS voice clips played by the setup wizard (see
`../wizard-audio.ts` for the catalog, the queue/serialization rules, and how
to add a moment).

**These are the SHIPPED recordings** — Peter's picked lines (2026-08-14),
ElevenLabs in the ads' voice (Daniel, onwK4e9ZLuTAKqWW03F9), loudness-
normalized to the ads' mastering (loudnorm I=-16, TP=-1.5). The
`WIZARD_AUDIO_CUES` transcripts are the clips' exact spoken words — change a
clip and its transcript together, never one alone. Replacing a clip = drop
the new MP3 here (same filename) + update its transcript + update the
`verify-wizard-audio.mjs` gate if the moment set changes.

Current moments: `greeting-{morning,afternoon,evening,rerun}` (wizard-open),
`migrate-found` (M2), `engine-install` (M3), `model-idle` (M5, 60 s at the
key prompt, once per run, queue-empty rule), `model-verified` (M4, the
affirmative success moment), `voice-handoff` (M7), `setup-complete` (M8).

Format as shipped: MP3, mono, 44.1 kHz, 128 kbps.

Length: the shipped clips run 4.4–6.3 s. Cues QUEUE and play strictly in
order — work never waits, only the presentation serializes; narration
*printing* defers per clip until it finishes (8.5 s failsafe), and clips at
input-wait moments simply play over the waiting form. The one hard cap:
**nothing over ~8 s** (past that a butler stops being a butler and becomes
hold music; the playback watchdog gives up at 15 s).

Shipped inside the ASAR (covered by the app signature); deliberately NOT part
of the voice-payload digest chain. The `verify-wizard-audio.mjs` gate runs in
every dist chain: closed moment set, per-file MP3 frame validation, size
floor, unique filenames, path containment.
