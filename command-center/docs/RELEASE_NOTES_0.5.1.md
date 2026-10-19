# Infinite AI Command Center 0.5.1: release notes

Release date: 2026-10-19 · upgrade from 0.5.0 (or any earlier version) · package
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
4. **Safe failure.** `install.ps1` stops before building if that preparation
   fails, and stops without touching the running system if the build fails.
   `start.ps1` starts nothing if the preparation fails.

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
- **What the server does now.** cc-gate 1.2.0 (in the package's `server`
  folder):
  - Refuses an update that does not name a version.
  - Installs exactly the named version.
  - Refuses plugins that declare another update source, or that WordPress.org
    does not know at their installed version.
  - Fails if the plugin is any other version afterwards.
- **What the Command Center does now.** It sends the approved version and sends
  no plugin update to an older gate. Test connection flags an older gate as
  outdated.
- **On your servers.** None is connected yet, so there is nothing to do. When
  you set one up, follow docs/SERVER_SETUP.md, which installs 1.2.0. A server
  already running an older cc-gate needs `sh install-gate.sh` from the 0.5.1
  `server` folder ("Updating cc-gate" in SERVER_SETUP.md).

## 3. Upgrade from your running 0.5.0 installation

Nothing below touches your Docker volumes, your other Docker projects or XAMPP.

1. **Back up**, in PowerShell in your current 0.5.0 folder (for example
   `C:\InfiniteAI\InfiniteAI-CommandCenter`):

   ```powershell
   Set-Location C:\InfiniteAI\InfiniteAI-CommandCenter
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\backup.ps1
   Copy-Item .\.env C:\InfiniteAI\env-backup-0.5.0.txt
   ```

   Keep `C:\InfiniteAI\env-backup-0.5.0.txt` private: it holds your keys.

2. **Download and check** `InfiniteAI-CommandCenter-0.5.1-windows.zip` and its
   `.sha256` file from `command-center/releases/` on the GitHub branch
   `ccr-eb27a2af-8u7i39`:

   ```powershell
   Get-FileHash .\InfiniteAI-CommandCenter-0.5.1-windows.zip -Algorithm SHA256
   ```

   The hash must equal the one in the `.sha256` file.

3. **Extract into a new folder.** Right-click the zip → Extract All →
   `C:\InfiniteAI\v0.5.1`. That gives
   `C:\InfiniteAI\v0.5.1\InfiniteAI-CommandCenter`. Extracting over the old
   folder also works from 0.5.1 on, but a new folder keeps 0.5.0 untouched for
   going back.

4. **Copy your `.env`** (unchanged; it holds your passwords and encryption
   keys) and install:

   ```powershell
   Copy-Item C:\InfiniteAI\InfiniteAI-CommandCenter\.env C:\InfiniteAI\v0.5.1\InfiniteAI-CommandCenter\.env
   Set-Location C:\InfiniteAI\v0.5.1\InfiniteAI-CommandCenter
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
   ```

   - It prints "Build folders refreshed (… files) for version 0.5.1", builds,
     restarts the containers and runs the smoke test.
   - The same project name is used, so the same volumes are kept.
   - It must end with all **seven** checks `PASS`, including
     `versions match this folder  API 0.5.1, dashboard 0.5.1`, and
     `SMOKE TEST PASSED`.

5. **Reload** the dashboard tab if it was open (or open
   http://localhost:8090). The sidebar should show
   `Dashboard v0.5.1 · API v0.5.1`. If the old page shows a banner, reload it;
   do not follow the old banner's reinstall advice.

6. **Optional checks:**

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status.ps1
   Compare-Object (Get-Content .\.env) (Get-Content C:\InfiniteAI\env-backup-0.5.0.txt)
   ```

   - `status.ps1` should show version 0.5.1 for the folder, the API and the
     dashboard, and the new folder for the API container.
   - `Compare-Object` prints nothing when the `.env` is unchanged.

7. **If something fails:**
   - Your data is untouched: install.ps1 never removes volumes.
   - "this folder mixes releases": extract again into a new, empty folder.
   - A build that stops with "out-of-date copy": run `install.ps1` once more.
   - Anything else: send the output of `install.ps1` and `status.ps1`.
   - **Going back:** run `install.ps1` from the 0.5.0 folder. 0.5.1 has no
     database changes.

Once 0.5.1 is verified, the old folder can be deleted. Keep its `backups\`
folder.

## 4. Tests (cloud sandbox)

To be filled in from the final runs.

Nothing was run on your PC, your websites or your servers.

## 5. Known limitations

To be filled in.
