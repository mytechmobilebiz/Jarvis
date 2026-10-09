#!/bin/sh
# Starts JARVIS from the source bundle (Mac or Linux). Run it in a terminal:
#   bash start.sh
# Mac and Windows users: the installer from your download page is easier --
# this script is for Intel Macs, Linux, servers, and people who like source.

support_error() {
  printf '\n%s\n' "$1"
  printf '%s\n' "Please email jarvis.agent.tech@pm.me with a copy of the text above."
  exit 1
}

node_is_ready() {
  [ -x "runtime/node/bin/node" ] || return 1
  _jarvis_version=$("runtime/node/bin/node" --version 2>/dev/null) || return 1
  _jarvis_major=${_jarvis_version#v}
  _jarvis_major=${_jarvis_major%%.*}
  case $_jarvis_major in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$_jarvis_major" -ge 22 ]
}

sha256_of() {
  if command -v shasum >/dev/null 2>&1; then
    _jarvis_line=$(shasum -a 256 "$1" 2>/dev/null) || return 1
  elif command -v sha256sum >/dev/null 2>&1; then
    _jarvis_line=$(sha256sum "$1" 2>/dev/null) || return 1
  else
    return 1
  fi
  printf '%s' "${_jarvis_line%% *}"
}

cd "$(dirname "$0")" || support_error "JARVIS could not open its own folder."

NODE_VERSION="v22.17.1"
NODE_BASE_URL="https://nodejs.org/dist/$NODE_VERSION"

OS_NAME=$(uname -s)
MACHINE_ARCH=$(uname -m)
# An Intel-translated shell on Apple Silicon reports x86_64; prefer the native build.
if [ "$OS_NAME" = "Darwin" ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = "1" ]; then
  MACHINE_ARCH="arm64"
fi

case "$OS_NAME/$MACHINE_ARCH" in
  Darwin/arm64)
    NODE_FILENAME="node-$NODE_VERSION-darwin-arm64.tar.gz"
    NODE_SHA256="a983f4f2a7b71512b78d7935b9ccf6b72120a255810070afd635c4146bca7b31"
    ;;
  Darwin/x86_64)
    NODE_FILENAME="node-$NODE_VERSION-darwin-x64.tar.gz"
    NODE_SHA256="b925103150fac0d23a44a45b2d88a01b73e5fff101e5dcfbae98d32c08d4bee3"
    ;;
  Linux/x86_64)
    NODE_FILENAME="node-$NODE_VERSION-linux-x64.tar.gz"
    NODE_SHA256="cfb6ac0cf339825fe36efd1f18a79016b02aca19fbfa6c9547c57e27dc09f6ea"
    ;;
  Linux/aarch64|Linux/arm64)
    NODE_FILENAME="node-$NODE_VERSION-linux-arm64.tar.gz"
    NODE_SHA256="f53510706998cf044f634190416f0588e7e1937aecea938768952e0f0ac1f41b"
    ;;
  *)
    support_error "This computer ($OS_NAME on $MACHINE_ARCH) is not supported yet."
    ;;
esac

NODE_ARCHIVE="runtime/$NODE_FILENAME.part"
NODE_URL="$NODE_BASE_URL/$NODE_FILENAME"

if node_is_ready; then
  printf '%s\n' "Step 1/3: the JARVIS engine is ready."
else
  printf '%s\n' "Step 1/3: downloading the JARVIS engine (about 45 MB, one time)..."

  mkdir -p "runtime" || support_error "JARVIS could not create its private engine folder."
  if [ -e "runtime/node" ] || [ -L "runtime/node" ]; then
    rm -rf "runtime/node" || support_error "JARVIS could not replace an incomplete engine download."
  fi

  DOWNLOAD_ATTEMPT=1
  while :; do
    rm -f "$NODE_ARCHIVE"
    if ! curl -L --fail -o "$NODE_ARCHIVE" "$NODE_URL"; then
      rm -f "$NODE_ARCHIVE"
      support_error "JARVIS could not download its engine. Please check your internet connection and try again."
    fi

    if ! ACTUAL_HASH=$(sha256_of "$NODE_ARCHIVE"); then
      rm -f "$NODE_ARCHIVE"
      support_error "JARVIS could not check that the engine download is safe."
    fi

    if [ "$ACTUAL_HASH" = "$NODE_SHA256" ]; then
      break
    fi

    rm -f "$NODE_ARCHIVE"
    if [ "$DOWNLOAD_ATTEMPT" -eq 1 ]; then
      printf '%s\n' "The download was incomplete. Trying it once more..."
      DOWNLOAD_ATTEMPT=2
    else
      support_error "JARVIS downloaded an incomplete engine twice. Please try again in a few minutes."
    fi
  done

  mkdir -p "runtime/node" || support_error "JARVIS could not create its private engine folder."
  if ! tar -xzf "$NODE_ARCHIVE" -C "runtime/node" --strip-components=1; then
    rm -rf "runtime/node"
    rm -f "$NODE_ARCHIVE"
    support_error "JARVIS could not unpack its engine."
  fi
  rm -f "$NODE_ARCHIVE"

  if ! node_is_ready; then
    rm -rf "runtime/node"
    support_error "The JARVIS engine did not start correctly."
  fi
fi

export PATH="$PWD/runtime/node/bin:$PATH"
export npm_config_update_notifier=false

# The scripts JARVIS runs call plain `pnpm`, so corepack must place its shims
# next to the bundled node binary before anything else runs.
if ! corepack enable --install-directory "$PWD/runtime/node/bin"; then
  support_error "JARVIS could not prepare its tools."
fi

printf '%s\n' "Step 2/3: getting JARVIS ready (downloads about 200 MB - a few quiet minutes and lots of text are normal)..."
if ! corepack pnpm install --frozen-lockfile; then
  support_error "JARVIS could not finish getting ready. Please check your internet connection and run this file again."
fi

# pnpm's package cache can restore Electron without the path.txt marker its
# loader requires; rerunning Electron's own install script repairs it.
if ! corepack pnpm --dir hud exec node -e "require('electron')" >/dev/null 2>&1; then
  printf '%s\n' "Repairing a JARVIS component (quick one-time fix)..."
  ELECTRON_DIR=$(corepack pnpm --dir hud exec node -p "require('path').dirname(require.resolve('electron/package.json'))" 2>/dev/null)
  if [ -z "$ELECTRON_DIR" ] || [ ! -f "$ELECTRON_DIR/install.js" ]; then
    support_error "JARVIS could not repair its display engine."
  fi
  (cd "$ELECTRON_DIR" && node install.js) || support_error "JARVIS could not repair its display engine. Please check your internet connection and run this file again."
fi

printf '%s\n' "Step 3/3: opening JARVIS..."
# A headless server has no display for the Electron HUD; the terminal wizard
# runs the same setup steps there.
if [ "$OS_NAME" = "Linux" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  printf '%s\n' "No graphical display found - starting the terminal setup wizard instead."
  if ! corepack pnpm run wizard; then
    support_error "JARVIS could not start the setup wizard."
  fi
elif ! corepack pnpm run hud; then
  support_error "JARVIS could not open the setup screen."
fi

printf '\n%s\n' "JARVIS has closed. Run 'bash start.sh' whenever you want to open it again."
exit 0
