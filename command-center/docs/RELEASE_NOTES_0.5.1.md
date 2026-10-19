# Infinite AI Command Center 0.5.1: release notes

Release date: 2026-10-19 · upgrade from 0.5.0 (from older versions, see DEPLOY_WINDOWS.md) · package
`InfiniteAI-CommandCenter-0.5.1-windows.zip` in `command-center/releases/` on
the GitHub branch, with its SHA-256 in the `.sha256` file next to it

0.5.1 fixes the version bug in 0.5.0, where the smoke test failed with "the
API reports 0.2.0 but this folder is 0.5.0". It also makes the server install
exactly the plugin version an administrator approved.

**Nothing to migrate.** There are no database migrations, no new keys and no
new settings. Your `.env`, your encryption keys, your PostgreSQL and n8n
volumes, your data, your agents and your approval rules are not changed.

## 1. What was wrong in 0.5.0

The cause was not the API's version code. The API reads its version from
`backend/app/__init__.py`, and in the 0.5.0 package that file says 0.5.0. The
cause was how Docker built the images from the installation folder.

- My package builder gave every file in every release the same timestamp
  (1 January 2026, 00:00).
- Docker keeps a copy of the files it last built the images from. It reuses a
  file from that copy when its size and timestamp look unchanged, without
  comparing the content. This copy is shared by every folder of the
  installation.
- `backend/app/__init__.py`, which holds the API's version, is 65 bytes in
  every release. So the copy from your first install (0.2.0) was built into
  every later image. This happened whether a release was extracted over the
  old folder or into a new one, and `docker compose build --no-cache` did not
  help.

Only three files were affected, and all three only hold the version number:

- `backend/app/__init__.py`: the API reported 0.2.0.
- The dashboard's `package.json` and `package-lock.json`: the dashboard
  reported 0.4.0.

Every other file was built from the release you installed, including all the
0.3.0 to 0.5.0 code, migrations and security fixes.

Two earlier explanations of mine were wrong:

- The 0.5.0 notes said the API container "was still running 0.2.0 code".
- The 0.5.0 banner said the last installation "did not replace the API
  container".

The container was replaced; the new image held one old file. **The 0.5.0
banner on your PC may tell you to run install.ps1 from the 0.4.0 folder.**
That is because the dashboard was built with the old version number. Do not
follow that advice; install 0.5.1 as below.

## 2. What 0.5.1 changes

### Builds can no longer use out-of-date files without saying so

1. **The package.** Every file carries the time this release was built, so no
   file looks unchanged from an earlier release. The package build refuses to
   run if any version number disagrees with `VERSION`. That covers the API,
   the dashboard, the lock file and the software bill of materials.
2. **The image builds.** Both images compare the version in the code they
   copied with this release's version. If they differ, the build stops with a
   message naming the out-of-date file, instead of producing an image with old
   code.
3. **The scripts.** `install.ps1` and `start.ps1` run the new
   `scripts\prepare-build.ps1` before every build. It does three things:
   - **Checks the folder.** It confirms the folder holds one release. If
     `VERSION` and the code disagree, it stops with "this folder mixes
     releases" and changes nothing.
   - **Refreshes the files.** It gives the files Docker builds from a fresh
     timestamp. This also works for read-only files and in folders whose path
     contains a folder named `dist`.
   - **Passes the version.** It passes this release's version to the build.

   It changes nothing else in the folder.
4. **Safe failure.**
   - `install.ps1` stops before building if that preparation fails.
   - It stops without touching the running system if the build fails.
   - It never creates a new `.env` (new passwords and keys) when this PC
     already has Command Center data: it asks for the existing `.env` instead.
   - `start.ps1` starts nothing if the preparation fails.

### Messages that stay accurate after an upgrade

- **The smoke test.** The "versions match this folder" check stays strict. It
  now names both the API's and the dashboard's version.
- **`status.ps1`.** It also prints the dashboard's version.
- **A page left open during an update.** It now says **"This page is out of
  date"** and offers to reload it, instead of giving reinstall advice. The
  dashboard compares its own version with the dashboard the server now serves.
- **The mismatch banner.** It appears only when the server's dashboard and the
  API really disagree. It says to reload first.
- **The sidebar.** It says "Versions differ" instead of "API: not updated".
- **Documentation.** The Knowledge Base and DEPLOY_WINDOWS.md explain the real
  cause and the new messages.

### Plugin updates install exactly the approved version (security)

- **What was missing.** Since 0.5.0, a plugin-update approval has named the
  exact version, for example "update akismet 5.3 → 5.4". But the server still
  ran "update to the latest version". If WordPress.org published 5.5 between
  the check and the update, 5.5 was installed.
- **What the Command Center does now.**
  - It sends the server exactly the version named in the approval.
  - It sends no plugin update to a gate older than 1.2.0. Test connection flags
    such a gate as outdated.
  - It never offers an approval for an "update" that is not a newer release.
    A compromised site's code can influence what WP-CLI reports.
- **What the server does now.** cc-gate 1.2.0 (in the package's `server`
  folder) installs a plugin only when all of these hold:
  - The request names the version.
  - The version is newer than the installed one. An older release may be a
    known-vulnerable one.
  - The version is WordPress.org's current release, which the gate asks
    WordPress.org directly instead of trusting the site.
  - The plugin comes from WordPress.org: it declares no other update source,
    and WordPress.org knows its installed version.

  If the plugin is any other version afterwards, the update fails and the
  dashboard says so. A refusal before anything changed is reported as a
  refusal, not as a failed repair.
- **What the gate cannot know.** The gate cannot see the approval itself. A
  compromised Command Center could still ask it to update a plugin to
  WordPress.org's current release, the same as before 1.2.0. It can no longer
  ask for an older or any other release.
- **On your servers.** None is connected yet, so there is nothing to do. When
  you set one up, follow docs/SERVER_SETUP.md, which installs 1.2.0. A server
  already running an older cc-gate needs `sh install-gate.sh` from the 0.5.1
  `server` folder ("Updating cc-gate" in SERVER_SETUP.md).

## 3. Upgrade from your running 0.5.0 installation

These steps use your folders: 0.5.0 in `C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter`,
and 0.5.1 in a new folder next to it. Run them in one PowerShell window,
in order. Nothing below touches your Docker volumes, your other Docker projects
or XAMPP.

1. **Name the two folders and back up.** The `Test-Path` line must print
   `True`. If it prints `False`, set `$old` to the folder that holds your
   current `.env` first.

   ```powershell
   $old = "C:\InfiniteAI\v0.5.0\InfiniteAI-CommandCenter"
   $new = "C:\InfiniteAI\v0.5.1\InfiniteAI-CommandCenter"
   Test-Path "$old\.env"
   Set-Location $old
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
   Copy-Item "$old\.env" "C:\InfiniteAI\env-backup-0.5.0.txt"
   ```

   Keep `C:\InfiniteAI\env-backup-0.5.0.txt` private: it holds your passwords
   and encryption keys.

2. **Download and check the package.** Download it from GitHub, repository
   `mytechmobilebiz/Jarvis`, branch `ccr-eb27a2af-8u7i39`, folder
   `command-center/releases`. Get both files:
   - https://github.com/mytechmobilebiz/Jarvis/raw/ccr-eb27a2af-8u7i39/command-center/releases/InfiniteAI-CommandCenter-0.5.1-windows.zip
   - https://github.com/mytechmobilebiz/Jarvis/raw/ccr-eb27a2af-8u7i39/command-center/releases/InfiniteAI-CommandCenter-0.5.1-windows.zip.sha256

   Then check the hash:

   ```powershell
   Get-FileHash "$env:USERPROFILE\Downloads\InfiniteAI-CommandCenter-0.5.1-windows.zip" -Algorithm SHA256
   Get-Content "$env:USERPROFILE\Downloads\InfiniteAI-CommandCenter-0.5.1-windows.zip.sha256"
   ```

   The two hashes must be identical (case does not matter).

3. **Extract into a new folder.** In Explorer: right-click the zip → Extract
   All → `C:\InfiniteAI\v0.5.1`. That creates
   `C:\InfiniteAI\v0.5.1\InfiniteAI-CommandCenter`. Then check it:

   ```powershell
   Get-Content "$new\VERSION"
   ```

   It must print `0.5.1`.

4. **Bring your `.env` (unchanged) and your proposal template, then install:**

   ```powershell
   Copy-Item "$old\.env" "$new\.env"
   if (Test-Path "$old\templates\proposal-template.html") { Copy-Item "$old\templates\proposal-template.html" "$new\templates\" }
   Set-Location $new
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
   ```

   - It prints "Build folders refreshed (… files) for version 0.5.1", builds,
     restarts the containers and runs the smoke test.
   - The project name stays the same, so the same volumes are kept.
   - It must end with all **seven** checks `PASS`, including
     `versions match this folder  API 0.5.1, dashboard 0.5.1`, and
     `SMOKE TEST PASSED`.
   - If the `.env` copy was missed, it stops with "this folder has no .env, but
     this PC already has Command Center data" and changes nothing. Copy it and
     run it again.

5. **Reload** the dashboard tab if it was open (or open
   http://localhost:8090). The sidebar should show
   `Dashboard v0.5.1 · API v0.5.1`. A page left open shows "This page is out of
   date": reload it. Do not follow the old 0.5.0 banner's reinstall advice.

6. **Optional checks:**

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1
   Compare-Object (Get-Content "$new\.env") (Get-Content "C:\InfiniteAI\env-backup-0.5.0.txt")
   ```

   - `status.ps1` should show version 0.5.1 for the folder, the API and the
     dashboard, and the new folder for the API container.
   - `Compare-Object` prints nothing when your `.env` is unchanged.

7. **If something fails:**
   - Your data is untouched: install.ps1 never removes volumes.
   - "this folder mixes releases": extract the zip again into a new, empty
     folder.
   - A build that stops with "out-of-date copy": run `install.ps1` once more.
   - Anything else: send the output of `install.ps1` and `status.ps1`.
   - **Going back:** run `install.ps1` from `$old`. 0.5.1 has no database
     changes.

From now on, run the day-to-day scripts (`start.ps1`, `stop.ps1`,
`backup.ps1`, `status.ps1`) from the new folder. Once 0.5.1 is verified, the
0.5.0 folder can be deleted; keep its `backups\` folder.

## 4. Tests (cloud sandbox)

All on the final 0.5.1 code, in the cloud sandbox: Linux, Docker 29, and the
scripts run in PowerShell 7.4. Nothing was run on your PC, your websites or
your servers.

| Test | Result |
|---|---|
| Backend (pytest, real PostgreSQL 18) | **524 passed, 0 failed**. New in 0.5.1: the gate installs exactly the approved version, refuses an older, equal or intermediate release, and refuses one that is not WordPress.org's current release; a delivered version other than the approved one is reported as a failure; the Command Center offers no approval for a downgrade and refuses a gate older than 1.2.0 before any backup; the release checks (every version number agrees with `VERSION`, the package builder refuses a mismatch, per-build timestamps, the build arguments in docker-compose.yml, the Dockerfile check run for real, the script wiring) |
| Dashboard (Vitest, TypeScript check) | **171 passed**, type check clean. New: a page left open during an update says "This page is out of date"; the mismatch banner's wording; the `VERSION` file equals the dashboard's built-in version |
| End-to-end on the running stack (Playwright, Chromium) | **3 consecutive full runs passed**: 40 passed and 1 skipped, then 41 and 41. The skip is expected: on a fresh stack the first run reaches the server-page test before the test server exists. The disposable WordPress server runs real OpenSSH, sudo, WP-CLI and **cc-gate 1.2.0** installed by `install-gate.sh`; the gate refuses the old two-argument plugin update |
| Server programs on Python 3.6 (EL8) | cc-gate, cc-wp and cc-site-helper compile; cc-gate 1.2.0's version comparison and its WordPress.org lookup run correctly |
| PowerShell scripts | **all 12 parse** with PowerShell's own parser; ASCII-only |
| `prepare-build.ps1` behaviour (`testing/prepare-build-check.sh`) | **5 of 5 passed**: a normal folder (files refreshed, version passed on); a folder inside one named `dist`; a read-only file (refreshed, still read-only); a folder mixing releases (refused before touching anything); no `VERSION` file (refused) |

Nothing was run on your PC, your websites or your servers.

## 5. Known limitations

### Security

- **The gate cannot verify the approval itself.** The Command Center sends
  the approved version, and cc-gate 1.2.0 installs only that version, only if
  it is newer and only if it is WordPress.org's current release. Someone in
  control of the Command Center, or of its SSH key, could therefore still ask
  for an update to the current release of a WordPress.org plugin without an
  approval. Before 1.2.0 that was "update to the latest" anyway. They can no
  longer ask for an older or any other release. Binding the gate to the
  approval itself would need approvals signed outside the Command Center. That
  is not built.
- **The successful plugin update is not yet proven against WordPress.org.**
  The sandbox cannot reach WordPress.org. The gate's refusals, the version
  checks and the failure reporting are tested, with WordPress.org's answer
  simulated. The first real update should be done on a test site.

### Builds and installation

- **What the new build check covers.** It compares only the files that hold
  the version numbers. Every other file is protected by the fresh timestamps
  (from the package and from `prepare-build.ps1`), not by a content check.
- **Indirect Python dependencies are not locked.** Only the direct ones are
  pinned. A fresh build may install newer compatible versions of indirect
  ones than those tested; this was already the case before 0.5.1.
- **Windows PowerShell 5.1 itself was not available for testing.** The
  scripts were run under PowerShell 7.4 and checked with PowerShell's parser.
  The read-only-file handling in `prepare-build.ps1` exists for 5.1 and was
  only exercised under 7.4.

### Still open from earlier releases

- **Not yet run on your side.** Nothing has run on your PC, a real CWP server
  or real websites. That includes the 0.4.0 → 0.5.0 approval changes, the
  host-key scan of a real server, and monitoring of real sites.
- **From docs/REFERENCE_DESIGN_GAP.md:**
  - Deferred: C3-09 and C3-10.
  - Waiting for your decisions: C3-11 to C3-14 and C2-17 to C2-20.
  - The dashboard is about 1,280 px tall at desktop width, and on short
    screens the upper sidebar scrolls.
- **Remote access** is not set up; it waits for the DNS inventory and your
  approval.
