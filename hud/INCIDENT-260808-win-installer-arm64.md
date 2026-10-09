# INCIDENT: shipped win-x64 installer contains ARM64-only payload (v1.3.8)

Status: ROOT CAUSE FOUND 2026-08-08. Fix + guardrail in progress. Customer-facing
impact: every Windows x64 customer who downloaded v1.3.8 gets a broken install.

## Symptom (two customers, independently)

Installer runs to completion, then "Missing Shortcut — Windows is searching for
JARVIS HUD.exe" fires on auto-launch. Install dir
`%LOCALAPPDATA%\Programs\@jarvishud` contains ONLY `Uninstall JARVIS HUD.exe`
(191 KB / 195,006 bytes) — no app payload at all. Reproduced on:
- Scott Reed's machine (Windows 11, long upgrade history) — multiple times,
  live over AnyDesk, with Defender exclusions in place, Protection history
  clean, no third-party AV → AV definitively exonerated.
- Poran Singh (new buyer 2026-08-07, first install 2026-08-08) — identical
  dialog "after it says its installed". Fresh machine → not an upgrade issue.

## Root cause (verified locally 2026-08-08)

Extracting the shipped artifact `hud/dist-app/jarvis-hud-setup-1.3.8-win-x64.exe`
(7zz from `~/Library/Caches/electron-builder/7zip@1.0.0/*/bin/7zz`) shows
`$PLUGINSDIR/` contains **`app-arm64.7z` only — no `app-64.7z`**. The NSIS
script extracts the payload matching the machine's native arch; on x64 there is
nothing to extract, so it writes only the uninstaller + shortcuts and
"succeeds". `$R0/Uninstall JARVIS HUD.exe` is 195,006 bytes — byte-matches the
lone file on Scott's machine.

Chain:
1. `hud/package.json` script `"dist": "electron-builder"` — no arch flags →
   electron-builder defaults to the HOST arch → arm64 on the Apple Silicon
   build Mac. (`dist-app/win-arm64-unpacked/JARVIS HUD.exe` is
   `PE32+ Aarch64`, stamped 14:33 Aug 7 = the 1.3.8 build; the x64
   `win-unpacked` dir is from the 13:01 1.3.7 build, which WAS x64 — so the
   1.3.7 and 1.3.8 builds were invoked differently.)
2. The landing server upload route (`landing/server/app.js:568`) only accepts
   `jarvis-hud-*-win-x64.exe` — so at ship time the arm64-built exe was
   renamed to the mandatory `-win-x64` name. Nothing verified the payload
   arch matched the advertised name.
3. Size tell (missed at ship time): 1.3.8 exe is 88.6 MB vs 1.3.7's 94.2 MB —
   arm64 Electron payloads are smaller.

## Why testing didn't catch it (telemetry-confirmed 2026-08-08)

Prod `onboarding_events` (live since v1.3.8 shipped) shows every packaged
win32 run is **arch=arm64** — the pre-ship testing happened in a Windows-on-ARM
VM (on the Apple Silicon build Mac), where the arm64 payload installs and runs
fine. **Zero packaged win32-x64 runs have ever phoned home** — direct
confirmation that no Windows customer got a working install. Independent
verification (Codex, log 20260808-114209): SHA-256 of the `-win-x64.exe`
artifacts byte-match the generically named build outputs (pure renames), the
1.3.7 installer carries a healthy x64 `app-64.7z`, and the release commits
changed no packaging — only the build invocation/host differed. CI's Windows
job runs on an x64 host but also lacks an explicit `--x64`. Guardrail must
therefore verify payload arch, not trust host or filename.

## Fix plan

1. Make the Windows build arch-explicit (`--win --x64`; decide whether to also
   ship a real arm64 artifact later — separate question, NOT part of hotfix).
2. Add an automated artifact-arch verification step to the dist flow: extract
   each `*win-x64.exe` and assert `app-64.7z` exists (and the packed
   `JARVIS HUD.exe` is `PE32+ x86-64`); fail the build otherwise.
3. Rebuild 1.3.8 (or bump 1.3.9), verify, redeploy. Personal download links
   always serve the newest uploaded file — customers re-download from their
   existing links.
4. Support loop (owned in `support/`): notify Scott Reed, Poran Singh,
   Ventsislav when the fixed artifact is live; update `support/BUGS.md`,
   `support/PRODUCT-STATE.md`, add `support/releases/` note.

## Open adjacent bugs (do not conflate)

- Setup wizard hard-crash "failing on hermes and profiles" (Scott, pre-1.3.8
  install that DID run) — still unexplained, `support/BUGS.md`.
- Install dir is `@jarvishud` (scoped package name) instead of `JARVIS HUD` —
  cosmetic, support-hostile, `support/BUGS.md`.
- Onboarding telemetry: prod table has 0 rows ever — `support/BUGS.md`.
