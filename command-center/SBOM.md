# Software bill of materials: Infinite AI Command Center 0.5.1

Generated 2026-10-19 by `scripts/generate-sbom.py`; machine-readable copy: `sbom.cdx.json` (CycloneDX 1.5).

Infinite Marketing's own code (everything under `command-center/` except the dependencies below) is proprietary.
No third-party component is vendored into this repository: each is fetched from its publisher at build or install time.

## Container images

| Image | Version | License | Used | Role |
|---|---|---|---|---|
| postgres | 18.6-alpine | PostgreSQL License (permissive) | runtime | database |
| n8nio/n8n | 2.43.7 | Sustainable Use License (fair-code; NOT open source; commercial redistribution restricted) | runtime | workflow engine |
| nginx | 1.27-alpine | BSD-2-Clause | runtime | serves the dashboard |
| python | 3.12-slim | PSF-2.0 (Python) + Debian package licenses | build base | backend image base |
| node | 22-alpine | MIT (Node.js) + Alpine package licenses | build only | builds the dashboard; not in the shipped image |
| wordpress | 6-apache | GPL-2.0-or-later | test only | throwaway site in docker-compose.testing.yml |
| mariadb | 11 | GPL-2.0 | test only | database for the throwaway WordPress |

## Python packages (installed in the backend image)

| Package | Version | License | Direct | Scope |
|---|---|---|---|---|
| annotated-doc | 0.0.5 | MIT |  | runtime |
| annotated-types | 0.8.0 | MIT |  | runtime |
| anthropic | 1.15.0 | MIT | yes | runtime |
| anyio | 4.15.1 | MIT |  | runtime |
| asyncpg | 0.32.0 | Apache-2.0 | yes | runtime |
| asyncssh | 2.24.1 | EPL-2.0 OR GPL-2.0-or-later | yes | runtime |
| bcrypt | 5.0.0 | Apache-2.0 | yes | runtime |
| certifi | 2026.10.18 | MPL-2.0 |  | runtime |
| cffi | 2.1.1 | MIT-0 |  | runtime |
| click | 8.5.0 | BSD-3-Clause |  | runtime |
| croniter | 6.2.4 | MIT | yes | runtime |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | yes | runtime |
| docstring_parser | 0.18.0 | MIT |  | runtime |
| fastapi | 0.143.3 | MIT | yes | runtime |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |  | runtime |
| h11 | 0.16.0 | MIT |  | runtime |
| httpcore | 1.0.9 | BSD-3-Clause |  | runtime |
| httpcore2 | 2.14.0 | BSD-3-Clause |  | runtime |
| httptools | 0.9.0 | MIT |  | runtime |
| httpx | 0.28.1 | BSD-3-Clause | yes | runtime |
| httpx2 | 2.14.0 | BSD-3-Clause |  | runtime |
| idna | 3.20 | BSD-3-Clause |  | runtime |
| jiter | 0.17.0 | MIT |  | runtime |
| opentelemetry-api | 1.45.1 | Apache-2.0 |  | runtime |
| pip | 25.0.1 | MIT |  | build tool |
| pycparser | 3.11 | BSD-3-Clause |  | runtime |
| pydantic | 2.14.1 | MIT | yes | runtime |
| pydantic-settings | 2.16.0 | MIT | yes | runtime |
| pydantic_core | 2.50.1 | MIT |  | runtime |
| python-dateutil | 2.9.0.post0 | Dual License |  | runtime |
| python-dotenv | 1.2.4 | BSD-3-Clause |  | runtime |
| PyYAML | 6.0.3 | MIT |  | runtime |
| six | 1.17.0 | MIT |  | runtime |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |  | runtime |
| SQLAlchemy | 2.1.5 | MIT | yes | runtime |
| starlette | 1.9.0 | BSD-3-Clause |  | runtime |
| truststore | 0.10.5 | MIT |  | runtime |
| typing-inspection | 0.4.4 | MIT |  | runtime |
| typing_extensions | 4.16.0 | PSF-2.0 |  | runtime |
| uvicorn | 0.54.0 | BSD-3-Clause | yes | runtime |
| uvloop | 0.23.0 | MIT License |  | runtime |
| watchfiles | 1.3.0 | MIT |  | runtime |
| websockets | 17.2 | BSD-3-Clause |  | runtime |

## npm packages shipped in the dashboard bundle (9)

| Package | Version | License | Direct |
|---|---|---|---|
| @fontsource-variable/inter | 5.2.8 | OFL-1.1 | yes |
| @remix-run/router | 1.21.0 | MIT |  |
| js-tokens | 4.0.0 | MIT |  |
| loose-envify | 1.4.0 | MIT |  |
| react | 18.3.1 | MIT | yes |
| react-dom | 18.3.1 | MIT | yes |
| react-router | 6.28.0 | MIT |  |
| react-router-dom | 6.28.0 | MIT | yes |
| scheduler | 0.23.2 | MIT |  |

## npm packages used only to build or test the dashboard (226)

Not distributed: they run during `npm run build` / `npm test` and are absent from the shipped image.

| Package | Version | License |
|---|---|---|
| @adobe/css-tools | 4.5.0 | MIT |
| @asamuzakjp/css-color | 3.2.0 | MIT |
| @babel/code-frame | 7.29.7 | MIT |
| @babel/compat-data | 7.29.7 | MIT |
| @babel/core | 7.29.7 | MIT |
| @babel/generator | 7.29.8 | MIT |
| @babel/helper-compilation-targets | 7.29.7 | MIT |
| @babel/helper-globals | 7.29.7 | MIT |
| @babel/helper-module-imports | 7.29.7 | MIT |
| @babel/helper-module-transforms | 7.29.7 | MIT |
| @babel/helper-plugin-utils | 7.29.7 | MIT |
| @babel/helper-string-parser | 7.29.7 | MIT |
| @babel/helper-validator-identifier | 7.29.7 | MIT |
| @babel/helper-validator-option | 7.29.7 | MIT |
| @babel/helpers | 7.29.10 | MIT |
| @babel/parser | 7.29.9 | MIT |
| @babel/plugin-transform-react-jsx-self | 7.29.7 | MIT |
| @babel/plugin-transform-react-jsx-source | 7.29.7 | MIT |
| @babel/runtime | 7.29.10 | MIT |
| @babel/template | 7.29.7 | MIT |
| @babel/traverse | 7.29.10 | MIT |
| @babel/types | 7.29.8 | MIT |
| @csstools/color-helpers | 5.1.0 | MIT-0 |
| @csstools/css-calc | 2.1.4 | MIT |
| @csstools/css-color-parser | 3.1.0 | MIT |
| @csstools/css-parser-algorithms | 3.0.5 | MIT |
| @csstools/css-tokenizer | 3.0.4 | MIT |
| @esbuild/aix-ppc64 | 0.21.5 | MIT |
| @esbuild/android-arm | 0.21.5 | MIT |
| @esbuild/android-arm64 | 0.21.5 | MIT |
| @esbuild/android-x64 | 0.21.5 | MIT |
| @esbuild/darwin-arm64 | 0.21.5 | MIT |
| @esbuild/darwin-x64 | 0.21.5 | MIT |
| @esbuild/freebsd-arm64 | 0.21.5 | MIT |
| @esbuild/freebsd-x64 | 0.21.5 | MIT |
| @esbuild/linux-arm | 0.21.5 | MIT |
| @esbuild/linux-arm64 | 0.21.5 | MIT |
| @esbuild/linux-ia32 | 0.21.5 | MIT |
| @esbuild/linux-loong64 | 0.21.5 | MIT |
| @esbuild/linux-mips64el | 0.21.5 | MIT |
| @esbuild/linux-ppc64 | 0.21.5 | MIT |
| @esbuild/linux-riscv64 | 0.21.5 | MIT |
| @esbuild/linux-s390x | 0.21.5 | MIT |
| @esbuild/linux-x64 | 0.21.5 | MIT |
| @esbuild/netbsd-x64 | 0.21.5 | MIT |
| @esbuild/openbsd-x64 | 0.21.5 | MIT |
| @esbuild/sunos-x64 | 0.21.5 | MIT |
| @esbuild/win32-arm64 | 0.21.5 | MIT |
| @esbuild/win32-ia32 | 0.21.5 | MIT |
| @esbuild/win32-x64 | 0.21.5 | MIT |
| @jridgewell/gen-mapping | 0.3.13 | MIT |
| @jridgewell/remapping | 2.3.5 | MIT |
| @jridgewell/resolve-uri | 3.1.2 | MIT |
| @jridgewell/sourcemap-codec | 1.6.0 | MIT |
| @jridgewell/trace-mapping | 0.3.31 | MIT |
| @napi-rs/lzma-linux-x64-gnu | 1.5.1 | MIT |
| @rollup/rollup-android-arm-eabi | 4.64.9 | MIT |
| @rollup/rollup-android-arm64 | 4.64.9 | MIT |
| @rollup/rollup-darwin-arm64 | 4.64.9 | MIT |
| @rollup/rollup-darwin-x64 | 4.64.9 | MIT |
| @rollup/rollup-freebsd-arm64 | 4.64.9 | MIT |
| @rollup/rollup-freebsd-x64 | 4.64.9 | MIT |
| @rollup/rollup-linux-arm-gnueabihf | 4.64.9 | MIT |
| @rollup/rollup-linux-arm-musleabihf | 4.64.9 | MIT |
| @rollup/rollup-linux-arm64-gnu | 4.64.9 | MIT |
| @rollup/rollup-linux-arm64-musl | 4.64.9 | MIT |
| @rollup/rollup-linux-loong64-gnu | 4.64.9 | MIT |
| @rollup/rollup-linux-loong64-musl | 4.64.9 | MIT |
| @rollup/rollup-linux-ppc64-gnu | 4.64.9 | MIT |
| @rollup/rollup-linux-ppc64-musl | 4.64.9 | MIT |
| @rollup/rollup-linux-riscv64-gnu | 4.64.9 | MIT |
| @rollup/rollup-linux-riscv64-musl | 4.64.9 | MIT |
| @rollup/rollup-linux-s390x-gnu | 4.64.9 | MIT |
| @rollup/rollup-linux-x64-gnu | 4.64.9 | MIT |
| @rollup/rollup-linux-x64-musl | 4.64.9 | MIT |
| @rollup/rollup-openbsd-x64 | 4.64.9 | MIT |
| @rollup/rollup-openharmony-arm64 | 4.64.9 | MIT |
| @rollup/rollup-win32-arm64-msvc | 4.64.9 | MIT |
| @rollup/rollup-win32-ia32-msvc | 4.64.9 | MIT |
| @rollup/rollup-win32-x64-gnu | 4.64.9 | MIT |
| @rollup/rollup-win32-x64-msvc | 4.64.9 | MIT |
| @testing-library/dom | 10.4.2 | MIT |
| @testing-library/jest-dom | 6.6.3 | MIT |
| @testing-library/react | 16.1.0 | MIT |
| @types/aria-query | 5.0.4 | MIT |
| @types/babel__core | 7.20.5 | MIT |
| @types/babel__generator | 7.27.0 | MIT |
| @types/babel__template | 7.4.4 | MIT |
| @types/babel__traverse | 7.28.0 | MIT |
| @types/estree | 1.0.9 | MIT |
| @types/prop-types | 15.7.15 | MIT |
| @types/react | 18.3.12 | MIT |
| @types/react-dom | 18.3.1 | MIT |
| @vitejs/plugin-react | 4.3.4 | MIT |
| @vitest/expect | 2.1.8 | MIT |
| @vitest/mocker | 2.1.8 | MIT |
| @vitest/pretty-format | 2.1.9 | MIT |
| @vitest/pretty-format | 2.1.8 | MIT |
| @vitest/pretty-format | 2.1.8 | MIT |
| @vitest/runner | 2.1.8 | MIT |
| @vitest/snapshot | 2.1.8 | MIT |
| @vitest/spy | 2.1.8 | MIT |
| @vitest/utils | 2.1.8 | MIT |
| agent-base | 7.1.4 | MIT |
| ansi-regex | 5.0.1 | MIT |
| ansi-styles | 4.3.0 | MIT |
| ansi-styles | 5.2.0 | MIT |
| aria-query | 5.3.0 | Apache-2.0 |
| assertion-error | 2.0.1 | MIT |
| asynckit | 0.4.0 | MIT |
| baseline-browser-mapping | 2.11.29 | Apache-2.0 |
| browserslist | 4.29.3 | MIT |
| cac | 6.7.14 | MIT |
| call-bind-apply-helpers | 1.0.2 | MIT |
| caniuse-lite | 1.0.30001816 | CC-BY-4.0 |
| chai | 5.3.3 | MIT |
| chalk | 3.0.0 | MIT |
| check-error | 2.1.3 | MIT |
| color-convert | 2.0.1 | MIT |
| color-name | 1.1.4 | MIT |
| combined-stream | 1.0.8 | MIT |
| convert-source-map | 2.0.0 | MIT |
| css.escape | 1.5.1 | MIT |
| cssstyle | 4.6.0 | MIT |
| csstype | 3.2.3 | MIT |
| data-urls | 5.0.0 | MIT |
| debug | 4.4.3 | MIT |
| decimal.js | 10.6.0 | MIT |
| deep-eql | 5.0.2 | MIT |
| delayed-stream | 1.0.0 | MIT |
| dequal | 2.0.3 | MIT |
| dom-accessibility-api | 0.6.3 | MIT |
| dom-accessibility-api | 0.5.16 | MIT |
| dunder-proto | 1.0.1 | MIT |
| electron-to-chromium | 1.5.459 | ISC |
| entities | 6.0.1 | BSD-2-Clause |
| es-define-property | 1.0.1 | MIT |
| es-errors | 1.3.0 | MIT |
| es-module-lexer | 1.7.0 | MIT |
| es-object-atoms | 1.1.2 | MIT |
| es-set-tostringtag | 2.1.0 | MIT |
| esbuild | 0.21.5 | MIT |
| escalade | 3.2.0 | MIT |
| estree-walker | 3.0.3 | MIT |
| expect-type | 1.4.0 | Apache-2.0 |
| form-data | 4.0.6 | MIT |
| fsevents | 2.3.3 | MIT |
| function-bind | 1.1.2 | MIT |
| gensync | 1.0.0-beta.2 | MIT |
| get-intrinsic | 1.3.0 | MIT |
| get-proto | 1.0.1 | MIT |
| gopd | 1.2.0 | MIT |
| has-flag | 4.0.0 | MIT |
| has-symbols | 1.1.0 | MIT |
| has-tostringtag | 1.0.2 | MIT |
| hasown | 2.0.4 | MIT |
| html-encoding-sniffer | 4.0.0 | MIT |
| http-proxy-agent | 7.0.2 | MIT |
| https-proxy-agent | 7.0.6 | MIT |
| iconv-lite | 0.6.3 | MIT |
| indent-string | 4.0.0 | MIT |
| is-potential-custom-element-name | 1.0.1 | MIT |
| jsdom | 25.0.1 | MIT |
| jsesc | 3.1.0 | MIT |
| json5 | 2.2.3 | MIT |
| lodash | 4.18.1 | MIT |
| loupe | 3.2.1 | MIT |
| lru-cache | 10.4.3 | ISC |
| lru-cache | 5.1.1 | ISC |
| lz-string | 1.5.0 | MIT |
| magic-string | 0.30.21 | MIT |
| math-intrinsics | 1.1.0 | MIT |
| mime-db | 1.52.0 | MIT |
| mime-types | 2.1.35 | MIT |
| min-indent | 1.0.1 | MIT |
| ms | 2.1.3 | MIT |
| nanoid | 3.3.20 | MIT |
| node-releases | 2.0.59 | MIT |
| nwsapi | 2.2.28 | MIT |
| parse5 | 7.3.0 | MIT |
| pathe | 1.1.2 | MIT |
| pathval | 2.0.1 | MIT |
| picocolors | 1.1.1 | ISC |
| postcss | 8.5.30 | MIT |
| pretty-format | 27.5.1 | MIT |
| punycode | 2.3.1 | MIT |
| react-is | 17.0.2 | MIT |
| react-refresh | 0.14.2 | MIT |
| redent | 3.0.0 | MIT |
| rollup | 4.64.9 | MIT |
| rrweb-cssom | 0.8.0 | MIT |
| rrweb-cssom | 0.7.1 | MIT |
| safer-buffer | 2.1.2 | MIT |
| saxes | 6.0.0 | ISC |
| semver | 6.3.1 | ISC |
| siginfo | 2.0.0 | ISC |
| source-map-js | 1.2.2 | BSD-3-Clause |
| stackback | 0.0.2 | MIT |
| std-env | 3.10.0 | MIT |
| strip-indent | 3.0.0 | MIT |
| supports-color | 7.2.0 | MIT |
| symbol-tree | 3.2.4 | MIT |
| tinybench | 2.9.0 | MIT |
| tinyexec | 0.3.2 | MIT |
| tinypool | 1.1.1 | MIT |
| tinyrainbow | 1.2.0 | MIT |
| tinyspy | 3.0.2 | MIT |
| tldts | 6.1.86 | MIT |
| tldts-core | 6.1.86 | MIT |
| tough-cookie | 5.1.2 | BSD-3-Clause |
| tr46 | 5.1.1 | MIT |
| typescript | 5.6.3 | Apache-2.0 |
| update-browserslist-db | 1.3.4 | MIT |
| vite | 5.4.11 | MIT |
| vite-node | 2.1.8 | MIT |
| vitest | 2.1.8 | MIT |
| w3c-xmlserializer | 5.0.0 | MIT |
| webidl-conversions | 7.0.0 | BSD-2-Clause |
| whatwg-encoding | 3.1.1 | MIT |
| whatwg-mimetype | 4.0.0 | MIT |
| whatwg-url | 14.2.0 | MIT |
| why-is-node-running | 2.3.0 | MIT |
| ws | 8.22.0 | MIT |
| xml-name-validator | 5.0.0 | Apache-2.0 |
| xmlchars | 2.2.0 | MIT |
| yallist | 3.1.1 | ISC |

## Review notes

- Licenses to review before distribution: none reported as unknown.
- n8n is fair-code (Sustainable Use License): see COMMERCIALIZATION.md before any commercial distribution.
- GPL components (WordPress, MariaDB) appear only in the test overlay and are never shipped.
