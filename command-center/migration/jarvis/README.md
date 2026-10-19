# JARVIS migration kit 0.1.0

Two read-only tools for the first step of moving JARVIS into the Infinite AI
Command Center. They change nothing on your PC:
- not JARVIS
- not the Command Center
- not any other program

| Command | What it makes |
|---|---|
| `Backup` | An encrypted backup of the JARVIS profile and content folders. |
| `Discover` | An inventory of JARVIS. It holds counts and structure only: no message text, no file contents, no secret values, no reasoning. |

The full design is in `docs/JARVIS_MIGRATION_DESIGN.md` (branch
`jarvis-migration`). Nothing is imported until you approve it.

## How it protects your data

- **Read-only access.** Each tool runs in a temporary Docker container started
  from the Command Center's own image.
  - The container has **no network**, so nothing can be sent anywhere.
  - Its system files are read-only.
  - The JARVIS folders and the database backup are mounted **read-only**.
  - Only a new output folder is writable. The kit refuses an output or restore
    folder inside the JARVIS folders.
- **The kit itself only reads.**
  - It opens files only for reading and never follows links out of the folders.
  - It opens the database backup read-only, through an SQLite guard that
    allows nothing but reads.
  - It never opens JARVIS's live `state.db`, and refuses if you point it there.
- **No JARVIS code runs.** It never runs or imports any JARVIS script, skill or
  instruction.
- **Secrets and personal data are only counted.**
  - Secret files are never copied. That covers:
    - `.env` and its variants, `auth.json`, `credentials.json` and token files;
    - SSH keys and certificates;
    - WhatsApp and other linked-device sessions;
    - Hermes's browser profiles, token folders and its own backup zips;
    - folders named secret or credentials.
  - Discovery shows only the setting names in them, never the values.
  - Inside the files that are copied, secret-looking values are replaced by
    `[REDACTED:<type>]`. That includes:
    - API keys and tokens;
    - passwords, including Google app passwords;
    - cookies and Basic auth;
    - webhook addresses;
    - keys inside URLs, including the Postiz MCP address;
    - WordPress application passwords;
    - private keys.
  - Scripts that only read a setting are left as they are. Redacted JSON,
    YAML and scripts keep their layout, and ordinary sentences are not changed.
  - Secrets and personal data elsewhere are counted, never shown.
- **Redaction has limits.** It recognises known formats only, and it cannot
  look inside PDFs and images.
  - The local list `backup-files.json` names every redacted file and every file
    it could not scan.
  - Treat the backup as confidential even so.
- **JARVIS can stay open.** The read-only check in the report lists any file
  JARVIS itself changed while the kit ran.

## Before you start

- Docker Desktop must be running.
- The Command Center 0.5.1 must be installed. The kit uses its image
  `infinite-command-center/backend:local` only as a Python runtime. It does not
  stop, restart or touch the Command Center's containers or data.

## Commands (one PowerShell window)

```powershell
# 1. Download the kit and its .sha256 file, then check it: this must print True
$zip = "$env:USERPROFILE\Downloads\jarvis-migration-kit-0.1.0.zip"
(Get-FileHash $zip -Algorithm SHA256).Hash -eq (Get-Content "$zip.sha256").Split(" ")[0]

# 2. Extract it (Explorer: right-click, Extract All, to C:\InfiniteAI\JarvisMigration),
#    which creates C:\InfiniteAI\JarvisMigration\jarvis-migration-kit
Set-Location C:\InfiniteAI\JarvisMigration\jarvis-migration-kit

# 3. Encrypted backup of the profile and content (Phase 1). You choose a passphrase.
powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action Backup

# 4. Check that the backup opens: step 3 prints this command with the real file name
powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action VerifyBackup -Archive "C:\InfiniteAI\JarvisMigration\backups\profile-backup-<time>\jarvis-profile-<time>.ccjb"

# 5. Inventory (Phase 2 and 3): a sample of 500 messages is scanned for counts
powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action Discover -Check -ListOtherProfiles
```

Your locations are the defaults:
- Profile: `%LOCALAPPDATA%\hermes\profiles\jarvis`
- Content: `%LOCALAPPDATA%\hermes\jarvis-content`
- Database backup:
  `C:\InfiniteAI\JarvisMigration\backups\jarvis-state-20261019-105625.db`
- Output: `C:\InfiniteAI\JarvisMigration`

Change them with `-ProfileDir`, `-ContentDir`, `-BackupDb` and `-OutRoot`.

**Options:**
- `-Check` runs SQLite's integrity quick check on the backup.
- `-ListOtherProfiles` writes only the names of other Hermes profiles next to
  JARVIS's.
- `-Full` scans every message instead of a sample. Run it only after you have
  looked at the sample result.

**How long it takes:** the kit prints a line for each step.
- On a synthetic database the size of yours, in the cloud sandbox, the default
  run took 29 seconds (with `-Check`) and `-Full` took about 25 minutes (one CPU core).
- Through Docker Desktop's file sharing it takes longer, so let it run.

## What to send back

`C:\InfiniteAI\JarvisMigration\discovery-<time>\` holds three files:

| File | What it holds | What to do with it |
|---|---|---|
| `discovery-summary.md` | Counts and structure. File names that are not JARVIS template names are replaced by numbers. | Read it. If you want it reviewed, send this file. |
| `discovery-summary.json` | The same, for the importer. | Keep it. |
| `discovery-detail.json` | Adds file paths and per-file findings, which can name customers. | **Keep it on your PC.** |

The backup folder also has `backup-report.json` (counts only) and
`backup-files.json` (every file in the backup, which were redacted and which
could not be scanned). Keep `backup-files.json` on your PC: file names can name
customers.

Never send the backup, its passphrase, `.env` or `auth.json`.

## If something goes wrong

| Message | What to do |
|---|---|
| `NOT RUN: ...` | A safety check stopped the kit before anything ran. The message says why. |
| `Docker is not running` | Start Docker Desktop and run the command again. |
| `the Docker image ... is not on this PC` | Install the Command Center 0.5.1 first (its `install.ps1` builds the image). |
| `wrong passphrase, or the archive was changed` | Check the passphrase. If it is right, the file was damaged. Nothing was restored. |
| `Docker stopped the kit before it finished` | Docker Desktop was closed or ran out of memory. Run the command again; nothing unfinished was kept. |

Development only (not part of the Windows package): `tests/` and
`make_templates.py`; the end-to-end check is `testing/jarvis-kit-check.sh`.
