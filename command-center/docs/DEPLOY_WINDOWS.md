# Deploying to a Windows 11 PC (Docker Desktop + WSL 2)

This puts the whole Command Center on your PC. It runs in its own Docker
Compose project, **`infinite-command-center`**, with its own containers,
network and volumes, so it does not touch XAMPP or other Docker projects
(including the Foundation Starter, project `infinite-ai-command-center`).

> **Security notice, 2026-10-20: 0.6.0 and 0.6.1 are NO-GO, for installation
> and for any JARVIS import.** A verification of the published 0.6.1 found a
> high-severity defect, reproduced three times independently on the exact
> published code. The text of a JARVIS message that an admin withheld comes
> back when a **later** JARVIS backup is imported. JARVIS's compaction stores
> copies of messages under new identifiers, and the importer withholds only
> the identifier that was withheld, so a copy is imported with its text.
> Operators, admins, the Supervisor's history and recall, and newly promoted
> memory items can then read it (viewers cannot), and the verification
> commands still report success. 0.6.0 has the same defect.
>
> **0.6.2 corrects it and is pending its verdict**
> (`releases/GO_NO_GO_0.6.2.md`; what changed:
> [RELEASE_NOTES_0.6.2.md](RELEASE_NOTES_0.6.2.md)). Until that verdict says
> GO:
>
> - Do not install 0.6.0, 0.6.1 or 0.6.2, and do not import JARVIS data with
>   any 0.6.x release.
> - If you are still on 0.5.0, stay there. 0.6.2 comes with a direct
>   0.5.0 -> 0.6.2 guide.
> - An installation of 0.6.0 or 0.6.1 that has imported nothing holds no
>   JARVIS data and is not exposed: leave it as it is and import nothing.
>
> The steps below describe 0.6.2.

| What | Where |
|---|---|
| Installation package | `InfiniteAI-CommandCenter-0.6.2-windows.zip` (in the GitHub repo at `command-center/releases/` on the branch `jarvis-migration`, with a `.sha256` file) |
| Source | GitHub `mytechmobilebiz/Jarvis`, branch `jarvis-migration` (0.6.2, 0.6.1 and 0.6.0; 0.5.1 and earlier on `ccr-eb27a2af-8u7i39`), folder `command-center/` |
| Install location on the PC | `C:\InfiniteAI\InfiniteAI-CommandCenter` (outside `C:\xampp`) |
| Dashboard | http://localhost:8090 (bound to 127.0.0.1 only) |
| n8n editor | http://localhost:5680 (bound to 127.0.0.1 only) |
| Data | Docker volumes `infinite-command-center_pgdata` and `infinite-command-center_n8n_data` |

Ports were chosen to avoid XAMPP (80, 443, 3306, Tomcat 8080) and an n8n
already running on 5678. Change them in `.env` if the preflight reports a clash.
No database port is published, so XAMPP's MySQL/MariaDB is never involved.

## Prerequisites

- Windows 11 with Docker Desktop running, using the WSL 2 engine and Linux containers.
- About 5 GB free disk space and an internet connection for the first build
  (images and packages are downloaded from Docker Hub, PyPI and npm).
- Nothing else: no Python, Node.js or Git is needed on the PC for the zip route.

## Install (PowerShell, as your normal user, no administrator needed)

The scripts are run with `-ExecutionPolicy Bypass` for that one process only;
the machine-wide execution policy is not changed. `preflight.ps1` and
`status.ps1` are read-only; read any script first if you wish.

1. Download `InfiniteAI-CommandCenter-0.6.2-windows.zip` and, optionally,
   check it against the `.sha256` file:
   `Get-FileHash .\InfiniteAI-CommandCenter-0.6.2-windows.zip -Algorithm SHA256`
2. Extract it to `C:\InfiniteAI` (you get `C:\InfiniteAI\InfiniteAI-CommandCenter`).
3. Open PowerShell and run:

```powershell
Set-Location C:\InfiniteAI\InfiniteAI-CommandCenter
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -AdminEmail you@yourdomain.com
```

`install.ps1` checks the machine, creates `.env` with fresh random secrets
(and prints the dashboard and n8n passwords once), validates the
configuration inside the application image, starts everything (the first
start creates the database roles, databases and tables) and runs the smoke
test. The first run takes 5-15 minutes. It must end with
`SMOKE TEST PASSED`; until it does on your PC, the application has not been
verified there. Re-running it is safe.

Then open http://localhost:8090 and sign in with the email and password that
were printed (they are also in `.env`).

Prefer Git? `git clone --branch jarvis-migration https://github.com/mytechmobilebiz/Jarvis.git`
and run the same command from `Jarvis\command-center`.

## Environment variables

`init-env.ps1` writes all of these; see `.env.example` for the full list.

| Variable | Purpose |
|---|---|
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | First dashboard admin; the email is also the n8n owner |
| `POSTGRES_PASSWORD`, `CC_DB_PASSWORD`, `N8N_DB_PASSWORD`, `CREDIT_REPAIR_DB_PASSWORD` | Database superuser and the three application roles (the Credit Repair role is separate; the main application cannot open that database) |
| `CC_SERVICE_TOKEN` | n8n calling the Command Center API |
| `CC_SECRETS_KEY` | Encrypts API keys entered on the Settings page. **Back it up.** |
| `CREDIT_REPAIR_DATA_KEY` | Encrypts Credit Repair client fields. **Back it up**; without it that data cannot be read. |
| `N8N_WEBHOOK_SECRET` | Command Center calling n8n's event webhook |
| `N8N_ENCRYPTION_KEY` | Encrypts n8n's stored credentials. **Back it up.** |
| `N8N_OWNER_PASSWORD` | n8n editor sign-in |
| `ANTHROPIC_API_KEY` (optional) | Deployment default for Claude; normally leave empty and enter your key on the dashboard's Settings page instead (stored encrypted) |
| `DASHBOARD_PORT`, `N8N_PORT`, `TZ` | Local ports and time zone |

Upgrading to 0.6.2 (from 0.6.1, 0.6.0 or 0.5.0, each directly) needs no new
keys and no `.env` change. Its migration `0008_brain_withhold_identity` ties
every withhold of a JARVIS message to the message itself, not to one of the
importer's keys for it, so a later import never brings the text back; it
repairs, before the first request, any database where 0.6.0 or 0.6.1 let the
text come back (on a PC with nothing imported it finds nothing), and locks the
JARVIS Brain data those versions imported, hidden from everyone, until
`jarvis-import.ps1 -Mode LegacyReset` and a new import of the same JARVIS
backup bring it back (RELEASE_NOTES_0.6.2.md, section 4). Going back after
0.6.2 means going back to 0.5.0 only. It also keeps
purged items purged when JARVIS moves or renames them, and makes `restore.ps1`
keep JARVIS Brain decisions (below, "Day to day"). Step by step:
`releases/UPGRADE_0.5.0_TO_0.6.2_WINDOWS.md` (on GitHub, in
`command-center/releases/` on the branch `jarvis-migration`, next to the zip;
from 0.5.0 straight to 0.6.2; for an installation of 0.6.0 or 0.6.1 its notes
say what to do); what changed, the defaults, going back
and the tests: [RELEASE_NOTES_0.6.2.md](RELEASE_NOTES_0.6.2.md). Use 0.6.2 on
the live installation and for the test copy before the JARVIS import below.
After the upgrade, `jarvis-import.ps1 -Target Production -Mode Status` must
say `identity repair done` (if not: `-Mode Repair`).

0.6.1 and 0.6.0 (NO-GO, security notice above) are described here for
reference only.

Upgrading to 0.6.1 (from 0.6.0, or from 0.5.0 directly) needs no new keys
and changes no existing data. Its migration `0007_brain_withhold` replaces one
check on a JARVIS Brain table (it allows one more value) and adds two
indexes; `db-init` sets short PostgreSQL error messages for the Command
Center's database user. 0.6.1 fixes withholding a JARVIS message that has a
promoted memory copy, and a withhold now removes the message's text from every
promoted copy and its versions, from the history answers that quote them and
from the import records. Step by step from 0.6.0:
[releases/UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md](../releases/UPGRADE_0.6.0_TO_0.6.1_WINDOWS.md)
(on GitHub, in `command-center/releases/` on the branch `jarvis-migration`,
next to the zip; it is not inside the zip);
what changed, going back and the tests:
[RELEASE_NOTES_0.6.1.md](RELEASE_NOTES_0.6.1.md).

Upgrading to 0.6.0 (from 0.5.0 directly, or from 0.5.1) needs no new keys
and changes no existing data: one migration adds the nine empty JARVIS Brain
tables; `.env`, the agents, approval rules, PostgreSQL and n8n volumes stay as
they are, and memory in AI prompts is off. **Installing 0.5.1 first is not
needed**: 0.6.0 contains its fix for "the API reports 0.2.0". Exact steps,
going back and backups: [RELEASE_NOTES_0.6.0.md](RELEASE_NOTES_0.6.0.md)
sections 3 to 6. Importing JARVIS's data is a separate step (below, "JARVIS
Brain: test copy and import").

Upgrading from 0.5.0 to 0.5.1 needs no new keys and changes no data: there
are no migrations, `.env` stays as it is, and the agents, approval rules,
PostgreSQL and n8n volumes are not touched. 0.5.1 fixes the wrong version
numbers after an upgrade (on the owner's PC the 0.5.0 smoke test failed with
"the API reports 0.2.0 but this folder is 0.5.0", and the dashboard showed
v0.4.0): only the files holding the version numbers had been built from old
copies Docker kept; all other code was current. **Do not follow the 0.5.0
banner's advice to run install.ps1 from an older folder**; install 0.5.1 as
in "Updating" below, then reload the dashboard page. Exact steps, and what
0.5.1 changes on servers (cc-gate 1.2.0 for plugin updates):
[RELEASE_NOTES_0.5.1.md](RELEASE_NOTES_0.5.1.md).

Upgrading from 0.4.0 to 0.5.0 needs no new keys and changes no
data: there are no new migrations, and the agents, their on/off states and the
approval rules stay as they are. Follow "Updating" below. One thing to know
first: pending approvals from 0.4.0 for a repair, a service restart, an email
or a publication cannot be approved after the upgrade (0.5.0 approvals name
the exact draft, version or key, and those older requests do not). Reject them
and request them again. See [RELEASE_NOTES_0.5.0.md](RELEASE_NOTES_0.5.0.md).

Upgrading from 0.3.0 to 0.4.0 needs no new keys and changes no data: 0.4.0
is the redesigned dashboard; the database, agents and approval rules are
unchanged.

Upgrading from 0.2.0 to 0.3.0 needs no new keys; follow "Updating" below. The
first start renames the agents to the seven official ones and keeps all your
data, switched-off agents included.

Upgrading from 0.1.0: `install.ps1` adds the three keys introduced since
then (`CC_SECRETS_KEY`, `CREDIT_REPAIR_DB_PASSWORD`, `CREDIT_REPAIR_DATA_KEY`)
to your existing `.env` and changes nothing else in it. To do only that step:
`.\scripts\init-env.ps1 -AddMissing`.

## Your proposal template (optional)

The Sales agent drafts proposals from `templates\proposal-template.html` if
the file exists, otherwise from a plain built-in template without prices.
Put your own template there (placeholders such as `{{CLIENT_NAME}}` and
`{{CLIENT_COMPANY}}`; see `templates\README.md`). The folder
is read-only to the application and is not included in backups or git.

## The dashboard (redesigned in 0.4.0, closer to the reference in 0.5.0)

The Dashboard page shows the four stat cards (AI agents, websites, tasks
today, website uptime), a card for each of the seven agents, task activity by
category, service status, recent tasks and active workflows. Every value comes
from the Command Center itself. Where nothing can be measured it says so:
**PC Resources** and the **Intern 2** card in the sidebar show "Not connected"
because no source for them exists yet. **Quick Actions** in the sidebar run
read-only checks and reports (and, for admins, open the n8n editor). The
bottom of the sidebar shows the dashboard's and the API's versions.

Type a request into the command input at the top of every page (or press
<kbd>Ctrl</kbd>+<kbd>K</kbd>) and press **Execute**; it goes to the AI
Supervisor with your sign-in and the usual approval rules. The answer appears
under the input. The sidebar holds every area; the **Knowledge Base** page
explains each one.

The microphone button next to the input is optional voice dictation. It
appears only in a browser that supports speech recognition (Chrome or Edge)
when the dashboard is opened as `http://localhost:8090` (browsers allow the
microphone only on secure addresses, and `localhost` counts as one). The first
time, the dashboard asks for your consent and the browser asks for microphone
permission. Chrome and Edge send the audio to Google or Microsoft to turn it
into text. Dictation only fills the input; nothing runs until you press
Execute.

## Servers and WordPress repairs (new in 0.3.0)

The **CWP / Servers** page connects the Command Center to your CWP servers through a
restricted, non-root SSH account that can only run an allowlist of operations
(`cc-gate`). Setting a server up is something you do on the server yourself;
follow [SERVER_SETUP.md](SERVER_SETUP.md). The `server` folder of this package
holds what you copy there. Until you do, everything else works as before and no
SSH connection is made.

**Scan host key** is carried out by the worker (since 0.5.0; before, the scan ran
in the API container, which has no route to the internet, and could not reach
a real server). It reads only the key the server presents; nothing is trusted
until you compare the fingerprint and press Trust.

## Remote access (optional, not set up)

Reaching the dashboard from outside the PC (for example at
`https://command.infinitemarketingai.com` through Cloudflare Tunnel and
Cloudflare Access) is planned but **not switched on**: see
[REMOTE_ACCESS_PLAN.md](REMOTE_ACCESS_PLAN.md). It needs the owner's approval at
each step, opens no router ports and leaves the local http://localhost:8090
unchanged. Its files (`docker-compose.tunnel.yml`, `remote-access\tunnel.conf`)
do nothing until `.env` switches them on.

## Credit Repair module

Off by default. To try it, sign in as admin, tick **Sensitive data** for your
own user on the Settings page, open **Credit Repair**, read the compliance
notice and enable the module. Use made-up test data only until the module has
had legal review (see COMMERCIALIZATION.md, blocker B5). Client data lives in
its own database (`credit_repair`) and is encrypted with
`CREDIT_REPAIR_DATA_KEY`; `backup.ps1` includes it.

## Uninstall

```powershell
.\scripts\uninstall.ps1               # removes containers and images, KEEPS your data
.\scripts\uninstall.ps1 -DeleteData   # also deletes the data, after taking a final backup
```

Only the project `infinite-command-center` is touched. Shared base images
(postgres, n8n, nginx) are left in place for other projects; remove them in
Docker Desktop if nothing else uses them.

## Day to day

```powershell
.\scripts\status.ps1     # what is running (read-only)
.\scripts\stop.ps1       # stop; data is kept
.\scripts\start.ps1 -NoBuild
.\scripts\backup.ps1     # dumps the databases to .\backups\<timestamp>\
.\scripts\restore.ps1 -BackupFolder .\backups\<timestamp>   # replaces current data; backs it up first, asks to confirm
```

Keep a copy of `.env` with your backups: the encryption keys in it are needed
to read stored credentials and Credit Repair data.

**What a restore keeps (0.6.2).** A restore replaces the whole database with
the backup, but `restore.ps1` keeps what must not be undone. It takes the
safety backup, refuses while a JARVIS Brain import, rollback or rescan is
running, stops the application, and only then (so nothing can be decided in
between) saves, in `backups\` (keys, ids, e-mail addresses and the audit
log; never message text, never a password hash):

- `removals-before-restore-<time>.json`: every message an admin withheld (with
  the title choice), every item an admin purged (also manual and promoted items
  and incoming versions of a changed file), every held row an admin released,
  the purge fingerprints, and every version an admin redacted;
- `access-before-restore-<time>.json`: who may sign in and what they may read,
  and the audit log.

After the restore, **before any service starts**, it applies them again in a
helper container of the api image (nothing serves the restored data yet): the
audit entries made after the backup are put back, every deactivation, revoked
sensitive-data permission and lowered role made after the backup is applied
again (never one that would leave no active admin), every sign-in is ended
(everyone signs in again), and the withholds, purges and releases are applied
again with who decided them and when. Then it starts the services. It prints
how many it applied again, and a **WARNING** listing the user changes the
restore does revert: a user created, reactivated, given a permission or a
higher role, or a password changed, after the backup. Redo those on the Users
page if they are still wanted.

Run it from the folder of the installed version: it stops before changing
anything when the api image is another version than the folder's `VERSION`
file, or when the containers were created from another folder
(`.\scripts\status.ps1` shows both). If it cannot save these files (for
example the api image is missing), or
cannot read the database to find out what to save, it starts the application
again and stops before changing anything. If it cannot apply them again, it
leaves the services stopped, prints the commands that apply the files by hand,
and ends with an error. Two switches accept the loss explicitly, and the script
says what is lost: `-SkipBrainRemovals` (withholds and purges made after the
backup are undone, their text readable again) and `-KeepRestoredAccess` (users
and permissions exactly as in the backup). Use the `restore.ps1` of 0.6.2 or
later; older versions do none of this.

If a restore is interrupted (Ctrl+C, a closed window, a reboot), nothing is
lost: `backups\restore-pending.json` names the files it saved, and the
database keeps a mark too. Until they are applied, `start.ps1` starts nothing
and the api and the worker refuse to start (their log says why). Finish it
with `.\scripts\restore.ps1 -FinishPending` (it applies them, then starts the
services), or run `restore.ps1` again: it applies them first, never exports
again from the restored data. Only if those files are lost for good, and you
accept that the withholds, purges and access changes made after that backup
stay undone: delete `backups\restore-pending.json` and run
`docker compose exec -T postgres psql -U postgres -d command_center -c "DELETE FROM brain_maintenance WHERE name = 'restore_reapply_pending'"`.
Each apply clears its part of the database's mark: a `restore-pending.json`
left behind after the files were applied (the mark is gone) is not applied
again, so it cannot undo what admins decided since; `restore.ps1` renames it.
Only 0.6.2 and later read the mark: never start 0.6.0 or 0.6.1 on a restored
database (going back means going back to 0.5.0 only).

Keep the `removals-before-restore-*.json` and `access-before-restore-*.json`
files like backups and delete them when you no longer need them: they hold
no text, but the key of a purged memory entry carries a 64-bit digest of the
entry's text.

(Prefix each with `powershell -NoProfile -ExecutionPolicy Bypass -File` if
script execution is restricted.)

Never run `docker compose down -v` unless you intend to delete all data.

## Updating

Your data lives in Docker volumes and your settings in `.env`, so an update
only replaces the program files:

```powershell
# Your current installation folder (the one with .env), and a new folder for the new version:
$old = "C:\InfiniteAI\InfiniteAI-CommandCenter"
$new = "C:\InfiniteAI\new\InfiniteAI-CommandCenter"   # Extract All of the zip into C:\InfiniteAI\new creates this
Set-Location $old
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
# now extract the new zip into C:\InfiniteAI\new, then:
Copy-Item "$old\.env" "$new\.env"
if (Test-Path "$old\templates\proposal-template.html") { Copy-Item "$old\templates\proposal-template.html" "$new\templates\" }
Set-Location $new
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1   # rebuilds; migrations apply automatically
```

If `.env` was not copied, `install.ps1` stops with "this folder has no .env, but
this PC already has Command Center data" (from 0.5.1): it never creates new
keys over existing data.

If remote access is switched on, the copied `.env` keeps it on; its token stays
in `C:\InfiniteAI\secrets`, outside the installation folder.

From 0.5.1, `install.ps1` gives the files Docker builds from a fresh
timestamp and the build checks that the copied code is this folder's
version, so extracting into a new folder (as above) or over the old one both
give current images. If the dashboard was open in a browser during the
update, reload the page afterwards: an open page keeps the previous version's
code until it is reloaded.

(With Git instead: `git pull`, then `install.ps1`.) Once the new folder is
verified, the old one can be deleted; keep its `backups\` folder.

## JARVIS Brain: test copy and import

JARVIS's knowledge and history come into the Command Center's memory in
stages: first into a **test copy** on this PC, where you review everything,
and only after your approval into the live installation. The live
installation is not touched by the test: `brain-test.ps1` and
`jarvis-import.ps1` print a *fingerprint* of it (its images, containers and
volumes) before and after each run and stop with a large red warning if
anything changed. `brain-precheck.ps1` only reads the fingerprint and writes
it to its report; you compare two reports yourself (step 5). A script
that finds a problem before it starts prints `NOT RUN: ...` and
`Nothing was changed.`

You need, from the JARVIS migration kit (`migration\jarvis\README.md`):
- the JARVIS database backup, for example
  `C:\InfiniteAI\JarvisMigration\backups\jarvis-state-<time>.db`;
- the profile backup restored into a new or empty folder, from the kit's
  folder:
  `Set-Location C:\InfiniteAI\JarvisMigration\jarvis-migration-kit`, then
  `powershell -NoProfile -ExecutionPolicy Bypass -File .\jarvis-kit.ps1 -Action VerifyBackup -Archive "<the .ccjb file>" -ExtractTo C:\InfiniteAI\JarvisMigration\restored`
  (it asks for the backup passphrase; it gets `restored\profile` and
  `restored\content`);
- optionally the kit's `discovery-summary.json` (the importer then checks that
  the backup holds the same number of sessions and messages). If you do not
  have it, leave the `$discovery = ...` line out of step 1 and remove
  `-Discovery $discovery` from the `Plan` and `Import` commands in steps 4
  and 7. Do not set `$discovery` to `""`: Windows PowerShell then passes
  `-Discovery` without a value and the command stops.
- free space: three times the size of the JARVIS backup on C: and on the
  drive that holds Docker's data (about 4.9 GB each for a 1.7 GB backup);
  `jarvis-import.ps1` refuses to run with less.

| Folder | What it is |
|---|---|
| `C:\InfiniteAI\InfiniteAI-CommandCenter` (or wherever it is now) | the live installation, with its `.env`. Stays as it is. |
| `C:\InfiniteAI\v0.6.2-brain-test\InfiniteAI-CommandCenter` | 0.6.2 extracted into a new folder, **without** a `.env`. The test copy runs only from here. |
| `C:\InfiniteAI\JarvisMigration` | the kit, the JARVIS backups, `restored`, the pre-check reports and `import-reports` |

The test copy is a separate Docker project, `cc-brain-test`: its own
containers, database, images and secrets (in `.env.brain-test`, never
`.env`), its dashboard at **http://127.0.0.1:8190** with a `cc-brain-test`
label on every page, no n8n, and a worker without internet. It needs Docker
Desktop with Docker Compose 2.24.4 or later.

**1. Folders.** Extract the 0.6.2 zip into `C:\InfiniteAI\v0.6.2-brain-test`
(Explorer: right-click, Extract All). Do not copy `.env` there.

```powershell
$test = "C:\InfiniteAI\v0.6.2-brain-test\InfiniteAI-CommandCenter"
$db = "C:\InfiniteAI\JarvisMigration\backups\jarvis-state-<time>.db"
$restored = "C:\InfiniteAI\JarvisMigration\restored"
$discovery = "C:\InfiniteAI\JarvisMigration\discovery-<time>\discovery-summary.json"
Set-Location $test
```

These names last only as long as this PowerShell window. If you open a new
window later (for example after the review, or after the upgrade in step 6),
run these lines again before you go on.

**2. Before the test: a read-only check of the live installation.** It writes
`C:\InfiniteAI\JarvisMigration\precheck-<time>.txt` and changes nothing.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-precheck.ps1
```

**3. The test copy.** Empty, or with a copy of your live data from the folder
of a `backup.ps1` run (schedules then come in switched off, open tasks are
cancelled, pending approvals rejected, and stored keys and sign-ins deleted).
The first start builds the images and takes a few minutes.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Start
# or, with a copy of the live data:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Start -SeedFromBackup "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter\backups\<timestamp>"
```

`-SeedFromBackup` takes the folder of a `backup.ps1` run, in the `backups`
folder of the installation you ran it from (it must contain
`command_center.dump`): `C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter\backups\<timestamp>`
before the upgrade, `C:\InfiniteAI\v0.6.2\InfiniteAI-CommandCenter\backups\<timestamp>`
after it (or the `v0.6.0` or `v0.6.1` folder, if one of those ran). Purged-item
fingerprints of the live installation are left out of the copy (they are keyed
with its database password; the purges themselves stay).

It prints the address and how to sign in: without a seed, the email it shows
and the `ADMIN_PASSWORD` in `.env.brain-test`; after a seed, your usual
accounts. `-Action Status` shows how it is doing, `-Action Stop` stops it
(data kept), `-Action Start` starts it again.

**4. The staged import into the test copy.** First a plan (nothing is
written), then 5 sessions, review them, then about 10 % of the sessions (the
plan shows how many there are), review again, then everything. Each import
prints its run number; check each run with `Verify`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Plan -All -BackupDb $db -Restored $restored -Discovery $discovery
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Import -Sample 5 -BackupDb $db -Restored $restored -Discovery $discovery
#   review at http://127.0.0.1:8190 (JARVIS Brain page), then:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Import -Sample <about 10 % of the sessions> -BackupDb $db -Restored $restored -Discovery $discovery
#   review again, then:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Import -All -BackupDb $db -Restored $restored -Discovery $discovery
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Verify -Run <run number> -BackupDb $db -Restored $restored
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Status
```

Reports go to `C:\InfiniteAI\JarvisMigration\import-reports`. To undo one
import in the test copy: `-Mode Rollback -Run <run number>` (newest first).
If you have already reviewed, edited or deleted any item of that run, the
rollback is refused until you add `-DiscardReviewed`; those items are then
deleted too.
An interrupted import continues with `-Mode Resume -Run <run number>` and the
same `-BackupDb` and `-Restored`.

**5. Your review, finishing your decisions, and the export** happen in the
test copy at http://127.0.0.1:8190 (JARVIS Brain page). Decisions are taken on
one JARVIS backup file, the `$db` of step 1, and travel with it: live later
imports **that same file** and replays them word for word.

- **Withhold.** The dialog names the JARVIS backup the decision is taken on.
  It says how many other stored rows are the same JARVIS message (withheld
  too) and how many share its JARVIS id with other text (held for your
  review). It lists, ticked, the stored rows of the conversation and its
  continuations and branches that equal, contain, restate or quote the
  message, each with what links it; untick a row to leave it readable (your
  decision is recorded). It hides the conversation's title when this is its
  first user message (untick to keep it), and lists the memory items that
  contain the text, for you to purge.
- **Finish your decisions (Settle)** once you have withheld what you want. It
  goes through every conversation family that holds a withhold on this backup,
  also the sessions a `-Sample` import skipped, with the withheld text in
  memory: rows that are certainly the withheld message are withheld for good,
  rows that may be are held for your review with what links them. Until you
  run it, the JARVIS Brain page shows **Finish decisions needed** and the
  export refuses.

  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode Settle -BackupDb $db -Restored $restored
  ```

- **Review** on the Review tab, "Held for review", group **Found when
  finishing decisions**: keep each row withheld, or release it. Rows that look
  independent (their own JARVIS id, another time, not a copy JARVIS made) are
  pre-selected for release; nothing is released until you confirm. A row whose
  text is, contains, restates or quotes the withheld text, or has any other
  relation to it, is released only after you type `RELEASE`. The export
  refuses while one of these rows waits for you.
- **Export** what you decided: the memory items (approved, rejected,
  archived, deleted, your edits), every withhold with the JARVIS row it names
  and the backup it was decided on, every hold you kept or released, the rows
  you left readable, hidden titles, conversations you stopped importing and
  the items you purged (keys, ids and times only, never their text). The file
  is version 5. A decisions file exported by 0.6.0 or 0.6.1 (version 3)
  applies only through `-Mode LegacyReset -Decisions <file> -Actor <admin
  email>`: export again with 0.6.2.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Test -Mode ExportDecisions
```

Not carried over, so write them down and do them again in the live
installation after step 7: edits of customer or Credit Repair items (the
export prints how many it left out, and step 7 lists them), conflicts you
resolved, workflow drafts you reviewed, changed or converted, messages you
promoted, and redacted versions.

It writes `import-reports\decisions-test-<time>.json`. Then run the pre-check
of step 2 again and compare the two reports. These lines must be the same:
the image IDs, the container IDs and names, the volume names and the applied
migrations. Start times change after a restart of the PC or of Docker
Desktop, and the counts (agents switched off, schedules enabled, sites,
pending approvals, users) change with normal use; neither means the test
touched the live installation.

**6. Approval.** Upgrading the live installation to 0.6.2 is a separate step
that you approve, in a new PowerShell window. Follow
`UPGRADE_0.5.0_TO_0.6.2_WINDOWS.md` step by step (from 0.5.0; for 0.6.0 or
0.6.1 its notes say what to do; on GitHub in
`command-center/releases/`, branch `jarvis-migration`; it is not in the zip):
`backup.ps1` in your current folder, check the zip's SHA-256, extract it into
the new folder `C:\InfiniteAI\v0.6.2`, copy `.env` there, then `install.ps1`,
which must end with `SMOKE TEST PASSED`. From then on,
`C:\InfiniteAI\v0.6.2\InfiniteAI-CommandCenter` is the live installation's
folder. The live import refuses to run on an installation without 0.6.2: its
migration `0008_brain_withhold_identity`, importer 1.1.0, the identity repair
done at its first start (if `-Mode Status` says `not done`, run
`-Mode Repair` and type `REPAIR PRODUCTION`), and no purged-item fingerprint
made with another database password. Set `$db`, `$restored` and `$discovery`
again (step 1) before step 7.

**7. The import into the live installation.** From the live installation's
folder only (the script refuses anywhere else). Import **the same JARVIS
backup file** the test copy imported, with the decisions file of step 5:
`-Decisions` applies your decisions first and then imports, so a withheld
message is never readable on live, not even for a moment. The script prints
the backup's SHA-256 and says whether it is the file the decisions were taken
on (`The decisions were taken on this very backup file ... the import replays
them`). If it warns that it is not, stop: another file is a *later backup* for
every conversation that holds a withheld message, and their new rows arrive
held for review (section "Later JARVIS backups" below).

`Import` asks you to type `IMPORT INTO PRODUCTION`; before importing it writes
a dump of the `command_center` database to `import-reports\pre-import\`,
checks it and prints its path and SHA-256. To undo an import use
`-Mode Rollback -Run <run number>` (it asks you to type `ROLL BACK
PRODUCTION`); once you have reviewed any item of that run, also add
`-DiscardReviewed`, or the rollback is refused; the reviewed items are then
deleted too. Restoring the dump is only for a disaster, because it also loses
everything done since. The dump holds your data unencrypted: keep it on this
PC and delete it once the import is verified and reviewed. The decisions file
must be in the reports folder (`import-reports`), where step 5 wrote it.

```powershell
$db = "C:\InfiniteAI\JarvisMigration\backups\jarvis-state-<time>.db"   # the SAME file as in the test copy
$restored = "C:\InfiniteAI\JarvisMigration\restored"
$discovery = "C:\InfiniteAI\JarvisMigration\discovery-<time>\discovery-summary.json"
Set-Location "C:\InfiniteAI\v0.6.2\InfiniteAI-CommandCenter"
Get-FileHash -Algorithm SHA256 $db   # the same SHA-256 the test copy's import report and the decisions file name
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1   # a full backup (Command Center, n8n, Credit Repair) before every live import
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Production -Mode Plan -All -BackupDb $db -Restored $restored -Discovery $discovery
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Production -Mode Import -All -BackupDb $db -Restored $restored -Discovery $discovery -Decisions "C:\InfiniteAI\JarvisMigration\import-reports\decisions-test-<time>.json" -Actor <your admin email>
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Production -Mode Verify -Run <run number> -BackupDb $db -Restored $restored
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\jarvis-import.ps1 -Target Production -Mode Status
```

`-Mode Status` must say `readable rows matching a withhold: 0` and
`withhold decisions: not finished on their backup (run Settle): 0`. Every row
has the same state as on the test copy: withheld, held or readable. Review
any row held for review on the live JARVIS Brain page (Review tab, "Held for
review"). `-Mode ApplyDecisions` after an import is only for a version 4 file
of a 0.6.2 development build: it applies fail-safe (its releases are listed
for you to release again).

Workflows converted from JARVIS's jobs are created **switched off**. Switch
the matching job off in JARVIS first, then switch the Command Center's
schedule on.

**8. Clean-up.** Remove the test copy (type `REMOVE TEST COPY` when asked).
Then delete the restored JARVIS files (they are plain text), the test copy's
secrets file, the review decisions file (it holds the text of the items you
edited, and the key of a purged memory entry carries a digest of the entry's
text) and, once the live import is verified and reviewed, the pre-import
dump (it holds your whole live database, unencrypted):

```powershell
$test = "C:\InfiniteAI\v0.6.2-brain-test\InfiniteAI-CommandCenter"
Set-Location $test
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\brain-test.ps1 -Action Remove
Remove-Item -Recurse -Force C:\InfiniteAI\JarvisMigration\restored
Remove-Item "$test\.env.brain-test"
Remove-Item C:\InfiniteAI\JarvisMigration\import-reports\decisions-*.json
Remove-Item C:\InfiniteAI\JarvisMigration\import-reports\pre-import\*.dump
```

If a step stops with `NOT RUN`, the message says why and nothing was changed;
fix that and run the same command again. If one prints
`THE LIVE INSTALLATION (infinite-command-center) CHANGED DURING THIS RUN`,
stop and send the whole output together with the output of `status.ps1` from
the live folder.

### Later JARVIS backups

Each later import of a newer JARVIS backup into the live installation is a
*later backup*. What it does with the conversations that hold a message you
withheld (a conversation and its continuations and branches, a "family"):
- **Nothing you decided changes.** A stored row of the family never gains
  text or becomes readable without your release, and no withhold, hold,
  release, left-readable choice, hidden title or stopped conversation is
  re-evaluated.
- **Every new row of the family gets one of three states:** withheld, when it
  is provably the withheld message (the same JARVIS row, or JARVIS's own copy
  of it: the same message id, time, role and tool call, or a message that
  absorbed it); readable, only when it is identical in role, tool call, time
  and text to a readable stored row of the family; otherwise **held for your
  review**, with what links it to the withheld text (identical, contains,
  part of it (or quotes it), restated, the same id, tool call or time, or no relation
  found). A new conversation of the family keeps its title hidden until you
  release its rows; after that the title comes back at the next import only
  when that backup holds every withheld text of the family and the title has
  no relation to any of them (JARVIS names a conversation after its first
  message). Otherwise it stays hidden.
- **Conversations outside every family** import as usual.
- **Before importing**, `-Mode Plan` says how many rows would be held, per
  withheld conversation.
- **After importing**, the Review tab's group **New in a later backup** lists
  the held rows by conversation. Per row: **Keep withheld** or **Release**
  (type `RELEASE` when it has any relation to the withheld text). Per
  conversation: **Release this conversation's rows with no relation** (offered
  only when the backup held every withheld text of that conversation, so the
  comparison could be made; a withheld text counts as held only as its own
  message or an unchanged copy of it, so when JARVIS merged it into another
  message or into a summary the rows show "nothing to compare" and are
  released one at a time), or **Stop importing this conversation** (later
  backups leave it out and count it; what is stored stays as it is).
- **A check for withheld text (tripwire).** Every import compares the rows it
  makes readable with the withheld texts the backup holds: inside a family a
  match is held and the run's `Verify` fails; outside, `-Mode Status` lists
  the rows by location (never their text) for an admin to look at.

JARVIS's own later edits and deletions inside a withheld conversation are not
copied into the Command Center: it keeps what it had and never brings text
back.

## Trying it against a throwaway WordPress

The test overlay (in the Git repository, not in the zip) adds a disposable
local WordPress the monitor can watch:

```powershell
docker compose -f docker-compose.yml -f docker-compose.testing.yml up -d --build
```

Add `http://wp-test` on the Websites page. Remove it with
`docker compose -f docker-compose.yml -f docker-compose.testing.yml rm -sf wp-test wp-test-db wp-test-setup`
followed by `docker volume rm infinite-command-center_wp_test_html`.

## Troubleshooting

- **"This page is out of date" banner**: the page was open during an update and still runs the previous dashboard. Reload the page.
- **Version mismatch banner, or the smoke test's "versions match this folder" fails**: first reload the dashboard page. If it stays, the API or the dashboard was not built from the files of the version you installed. Up to 0.5.0, Docker could build a new release with out-of-date copies of the files that hold the version numbers (it kept them from an earlier build, whether the zip was extracted over the old folder or into a new one; `--no-cache` did not help). From 0.5.1, `install.ps1` and `start.ps1` refresh those files before every build and the build stops if the copied code is the wrong version, so run `install.ps1` again from the new version's folder (with the `.env` copied there), then `smoke.ps1`. `.\scripts\status.ps1` prints the folder's version, the versions the API and the dashboard report, and the folder the API container was created from.
- **The build stops with "... in this build says X, but this release is Y: Docker used an out-of-date copy ..."**: the check above caught an image about to be built from old files; nothing was changed. Run `install.ps1` again (it refreshes the files first). If it stops again, send the message and the output of `status.ps1`.
- **"Could not prepare the build folders: this folder mixes releases"**: the folder's `VERSION` file and its code name different versions (an extraction that was interrupted, or files skipped when extracting over an older folder). Extract the zip again into a new, empty folder, copy `.env` there and run `install.ps1` from it. Nothing was built or changed.
- **"No internet on this PC" banner**: the monitor could not reach api.wordpress.org, www.cloudflare.com or www.google.com, so websites are marked "not checked" instead of down. If your network blocks those hosts on purpose, set `MONITOR_CONTROL_URLS` in `.env` to addresses that answer (see `.env.example`), then `start.ps1 -NoBuild`.
- **Scan host key says the worker is not running**: `.\scripts\status.ps1`, then `start.ps1 -NoBuild`.
- **No microphone button**: open the dashboard as `http://localhost:8090` (not the PC's IP address) in Chrome or Edge, and sign in as an admin or operator; viewers cannot send commands. If the browser blocked the microphone, allow it from the padlock/site icon in the address bar.
- **Port in use**: set `DASHBOARD_PORT` / `N8N_PORT` in `.env`, then `start.ps1`.
- **`docker compose up` fails pulling images**: check Docker Desktop is signed in or that Docker Hub is reachable.
- **n8n unhealthy**: `docker compose logs --tail=100 n8n`. First start takes about a minute.
- **Cannot sign in to n8n over plain HTTP from another machine**: by design; n8n is local-only until a TLS reverse proxy is added.
