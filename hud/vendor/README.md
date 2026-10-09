# hud/vendor — offline engine payload

`engine/` holds the vendored Hermes engine payload that lets the packaged
installer install the engine with **zero network access** on the customer
machine. It is assembled at BUILD time by:

```
node hud/scripts/build-vendor-payload.mjs [--platform win32|darwin] [--arch x64|arm64|universal] [...]
```

(see the script's header comment for every option and the rationale behind
every choice). The payload is large (~320 MB uncompressed for win-x64, ~180 MB for the
universal mac dual-runtime build) and is **never committed** — only `engine/.gitkeep` is tracked so the directory
always exists for electron-builder's extraResources globs. CI (windows-latest)
builds the real payload before packaging.

## Layout (contract with the runtime installer — do not change casually)

```
engine/
  vendor-manifest.json   # written LAST; its presence means "payload complete"
  hermes-agent/          # pruned engine source tree (no .git, no tests/docs/web)
  wheelhouse/
    requirements.txt     # fully pinned name==version, one per line, LF, sorted
    *.whl                # every wheel for requirements.txt + the engine's own wheel
  python/                # relocatable CPython (python-build-standalone)
                         #   win32: python/python.exe   darwin: python/bin/python3
```

A `--arch universal` mac payload carries TWO runtimes instead of one, because
python-build-standalone publishes no universal2 build:

```
engine/
  python-arm64/bin/python3
  python-x64/bin/python3
```

The manifest names both under `python.variants`, and the installer picks the
one matching `process.arch` at install time. `verify-vendor-payload --release
--arch universal` REFUSES a payload that carries only one of them — a universal
DMG whose engine installs on just one architecture is the failure that check
exists to prevent.

The runtime install then is: create venv from `python/`, then
`pip install --no-index --find-links wheelhouse -r wheelhouse/requirements.txt`
plus the engine wheel named in the manifest. No git, no pypi, no installer
scripts on the customer machine.

## Notes

- Requirements come from the engine's `uv.lock` via `uv export --frozen` with
  PEP 508 markers evaluated for the TARGET platform (pip does not do this
  cross-platform — see the script header). Any locked dep without a compatible
  wheel fails the BUILD, never the customer install.
- A `--skip-python` payload is a dev artifact only; never ship one.
- `--arch universal` (mac) unions both arches' wheel sets AND bundles both
  runtimes (`python-arm64/`, `python-x64/`) — it is not a wheelhouse-only
  payload, and `--skip-python` is never used for a release. (This bullet used
  to say the opposite; the code has vendored dual runtimes since the reviewer
  found that a universal DMG could otherwise install on one architecture only.)
- mac baselines: arm64 payloads require macOS >= 14 (av/onnxruntime publish
  only `macosx_14_0_arm64` wheels at current pins); x86_64 requires >= 11.
  The `voice` extra is arm64-only on mac — onnxruntime ships no x86_64 mac
  wheels, so an Intel/universal mac voice payload fails the build (by design).
- Build machine needs: node >= 20, `uv`, a python with pip, `tar`, network.
- The payload must be byte-reproducible from a clean run: tools that leave
  droppings in the output (uv writes `wheelhouse/.gitignore`) have them pruned
  BEFORE the digest list is written, because an unlisted file on disk fails
  verification rather than being ignored.
