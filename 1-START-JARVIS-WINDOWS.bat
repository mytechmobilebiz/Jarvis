@echo off
setlocal EnableExtensions
title JARVIS Setup

cd /d "%~dp0"
if errorlevel 1 goto error_folder

set "NODE_VERSION=v22.17.1"
set "NODE_FOLDER=node-%NODE_VERSION%-win-x64"
set "NODE_ARCHIVE=runtime\%NODE_FOLDER%.zip.part"
set "NODE_URL=https://nodejs.org/dist/%NODE_VERSION%/%NODE_FOLDER%.zip"
set "NODE_SHA256=b1fdb5635ba860f6bf71474f2ca882459a582de49b1d869451e3ad188e3943eb"
set "HASH_OUTPUT=runtime\node-sha256.txt"
set "npm_config_update_notifier=false"

where curl.exe >nul 2>&1
if errorlevel 1 goto error_windows_too_old
where tar.exe >nul 2>&1
if errorlevel 1 goto error_windows_too_old

call :node_is_ready
if not errorlevel 1 goto node_available

echo Step 1/3: downloading the JARVIS engine (about 30 MB, one time)...

if not exist "runtime\" mkdir "runtime"
if errorlevel 1 goto error_runtime_folder

if exist "runtime\node" rmdir /s /q "runtime\node"
if exist "runtime\node" goto error_cleanup
if exist "runtime\%NODE_FOLDER%" rmdir /s /q "runtime\%NODE_FOLDER%"
if exist "runtime\%NODE_FOLDER%" goto error_cleanup

set "DOWNLOAD_ATTEMPT=1"

:download_node
if exist "%NODE_ARCHIVE%" del /f /q "%NODE_ARCHIVE%"
if exist "%HASH_OUTPUT%" del /f /q "%HASH_OUTPUT%"

curl.exe -L --fail -o "%NODE_ARCHIVE%" "%NODE_URL%"
if errorlevel 1 goto error_download

certutil -hashfile "%NODE_ARCHIVE%" SHA256 > "%HASH_OUTPUT%"
if errorlevel 1 goto error_verify

rem Pick the hash line by shape (hex digits, possibly space-separated), not by
rem position - certutil's header layout varies across Windows builds and locales.
set "ACTUAL_HASH="
for /f "delims=" %%H in ('findstr /r /c:"^[0-9a-fA-F ][0-9a-fA-F ]*$" "%HASH_OUTPUT%"') do if not defined ACTUAL_HASH set "ACTUAL_HASH=%%H"
if not defined ACTUAL_HASH goto error_verify
set "ACTUAL_HASH=%ACTUAL_HASH: =%"

if /I "%ACTUAL_HASH%"=="%NODE_SHA256%" goto hash_ok

del /f /q "%NODE_ARCHIVE%" >nul 2>&1
del /f /q "%HASH_OUTPUT%" >nul 2>&1
if "%DOWNLOAD_ATTEMPT%"=="1" (
  echo The download was incomplete. Trying it once more...
  set "DOWNLOAD_ATTEMPT=2"
  goto download_node
)
goto error_hash

:hash_ok
del /f /q "%HASH_OUTPUT%" >nul 2>&1

tar.exe -xf "%NODE_ARCHIVE%" -C "runtime"
if errorlevel 1 goto error_extract
if not exist "runtime\%NODE_FOLDER%\node.exe" goto error_extract

move "runtime\%NODE_FOLDER%" "runtime\node" >nul
if errorlevel 1 goto error_extract
if not exist "runtime\node\node.exe" goto error_extract

del /f /q "%NODE_ARCHIVE%" >nul 2>&1
call :node_is_ready
if errorlevel 1 goto error_runtime
goto engine_ready

:node_available
echo Step 1/3: the JARVIS engine is ready.

:engine_ready
set "PATH=%~dp0runtime\node;%PATH%"

rem The scripts JARVIS runs call plain pnpm, so corepack must place its shims
rem next to the bundled node binary before anything else runs.
call corepack enable --install-directory "%~dp0runtime\node"
if errorlevel 1 goto error_tools

echo Step 2/3: getting JARVIS ready (downloads about 200 MB - a few quiet minutes and lots of text are normal)...
call corepack pnpm install --frozen-lockfile
if errorlevel 1 goto error_install

rem pnpm's package cache can restore Electron without the path.txt marker its
rem loader requires; rerunning Electron's own install script repairs it.
call corepack pnpm --dir hud exec node -e "require('electron')" >nul 2>&1
if not errorlevel 1 goto electron_ok

echo Repairing a JARVIS component (quick one-time fix)...
set "ELECTRON_DIR="
for /f "delims=" %%D in ('call corepack pnpm --dir hud exec node -p "require('path').dirname(require.resolve('electron/package.json'))" 2^>nul') do if not defined ELECTRON_DIR set "ELECTRON_DIR=%%D"
if not defined ELECTRON_DIR goto error_repair
if not exist "%ELECTRON_DIR%\install.js" goto error_repair
pushd "%ELECTRON_DIR%"
call node install.js
if errorlevel 1 (
  popd
  goto error_repair
)
popd

:electron_ok
echo Step 3/3: opening JARVIS...
call corepack pnpm run hud
if errorlevel 1 goto error_start

echo.
echo JARVIS has closed. You can double-click this file whenever you want to open it again.
pause
exit /b 0

:node_is_ready
set "NODE_MAJOR="
if not exist "runtime\node\node.exe" exit /b 1
for /f "tokens=1 delims=." %%V in ('"runtime\node\node.exe" --version 2^>nul') do set "NODE_MAJOR=%%V"
if not defined NODE_MAJOR exit /b 1
set "NODE_MAJOR=%NODE_MAJOR:v=%"
for /f "delims=0123456789" %%V in ("%NODE_MAJOR%") do set "NODE_MAJOR="
if not defined NODE_MAJOR exit /b 1
if %NODE_MAJOR% LSS 22 exit /b 1
exit /b 0

:error_folder
echo JARVIS could not open its own folder.
goto failure

:error_windows_too_old
echo This version of Windows is missing tools JARVIS needs (curl and tar).
echo Please install Windows updates (Windows 10 from 2018 or newer works), then try again.
goto failure

:error_runtime_folder
echo JARVIS could not create its private engine folder.
goto failure

:error_cleanup
echo JARVIS could not replace an incomplete engine download.
goto failure

:error_download
del /f /q "%NODE_ARCHIVE%" >nul 2>&1
echo JARVIS could not download its engine. Please check your internet connection and try again.
goto failure

:error_verify
del /f /q "%NODE_ARCHIVE%" >nul 2>&1
del /f /q "%HASH_OUTPUT%" >nul 2>&1
echo JARVIS could not check that the engine download is safe.
goto failure

:error_hash
echo JARVIS downloaded an incomplete engine twice. Please try again in a few minutes.
goto failure

:error_extract
if exist "runtime\node" rmdir /s /q "runtime\node"
if exist "runtime\%NODE_FOLDER%" rmdir /s /q "runtime\%NODE_FOLDER%"
del /f /q "%NODE_ARCHIVE%" >nul 2>&1
echo JARVIS could not unpack its engine.
goto failure

:error_runtime
if exist "runtime\node" rmdir /s /q "runtime\node"
echo The JARVIS engine did not start correctly.
goto failure

:error_tools
echo JARVIS could not prepare its tools.
goto failure

:error_install
echo JARVIS could not finish getting ready. Please check your internet connection and double-click this file again.
goto failure

:error_repair
echo JARVIS could not repair its display engine. Please check your internet connection and double-click this file again.
goto failure

:error_start
echo JARVIS could not open the setup screen.
goto failure

:failure
echo Please email jarvis.agent.tech@pm.me with a photo of this window.
pause
exit /b 1
