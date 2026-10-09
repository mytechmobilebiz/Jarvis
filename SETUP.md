# JARVIS Setup

## Never used a terminal?

You do not need one. The easiest path on Mac and Windows is the installer from your download page — it opens the setup screen directly. From this folder instead: on Windows double-click `1-START-JARVIS-WINDOWS.bat`; on Mac or Linux open a terminal here and run `bash start.sh`. The launcher downloads what JARVIS needs, then opens the setup screen and walks you through the rest.

This is the manual mirror of first-run setup. The same steps run two ways — visually in the JARVIS HUD, your desktop command center (`pnpm run hud` on a machine with no profile yet), or as a terminal wizard (`pnpm run wizard`, the right choice on a headless VPS). Eleven of the twelve below run on a fresh machine; step 2, Earlier Install, appears only when JARVIS finds a previous install to tidy up. Use this document when you want to inspect each step, or when setup reached a snag and you would rather finish the household by hand.

**What you need**
- Node 22 or newer.
- On macOS/Linux, `git` and `curl`; on Windows, PowerShell (the Hermes installer provisions PortableGit itself).
- A Mac, Linux, native Windows machine, or a small VPS that can stay on. Two-way voice ships for Windows and macOS.
- A way for Jarvis to think: sign in with a ChatGPT or Claude subscription you already pay for (nothing to find or paste), or an API key if you prefer one (Anthropic, OpenRouter, or OpenAI).
- Optional Slack admin access. Voice needs nothing extra installed: the speech engines and their models ship inside the JARVIS application.

Fine print: the wizard reports anonymous setup telemetry (platform, step
outcomes, chosen provider — never keys, names, or content, never linked to your
purchase). Opt out with `JARVIS_NO_TELEMETRY=1` in your environment or profile
`.env`.

**Quick path**

Run this from the unzipped repository root (skip this whole section if you used a double-click launcher — it does the same thing). If `pnpm` is missing, install the pinned package manager first:

```sh
npm install --global pnpm
pnpm install --frozen-lockfile
pnpm run hud      # visual first-run setup, becomes the HUD when done
```

Or, for the terminal wizard (identical steps, no display needed):

```sh
pnpm install --frozen-lockfile
pnpm -r build
pnpm run wizard
```

The `@jarvis/setup` build script intentionally invokes `tsc` directly: package scripts resolve `.cmd` shims through the shell on Windows, while the old Node `execFileSync("tsc")` wrapper could not.

If you prefer npm only:

```sh
npm install
npm run build --workspaces
npm run wizard
```

The wizard asks you to press Enter to run each step, or `S` to skip. In order, it checks the machine, clears up an earlier JARVIS install if it finds one, installs the bundled Hermes engine, installs the `jarvis` profile, composes Sarah, Tom, Bobby, Scout, and Ava into that profile, asks how Jarvis should think, offers Slack, starts the engine and proves it answers, verifies the bundled voice engines, builds and installs JARVIS HUD as a desktop app, posts the greeting, and prints the summary.

Plan on 10-30 minutes. Signing in with no Slack is the short path; Slack takes longer. There is no service to install and no key to copy anywhere: the app starts the engine itself and hands it its credentials.

Nothing is downloaded during setup on either platform — the engine, its Python
and its libraries all ship inside the app. On macOS they ship already installed,
so the engine step is a folder copy and takes seconds; on Windows the step
builds the engine's environment from the bundled library files, which takes a
minute or two on a slow disk. Neither path can reach the network: an install
that fails is a disk or download problem with the app itself, never your
connection.

**Manual path**
Run the manual path from the repository root. Keep this terminal open so the exported paths stay available. These commands use the concrete POSIX default Hermes root, `~/.hermes` (Windows: `%LOCALAPPDATA%\hermes`; if you set `HERMES_HOME` yourself, use that root, or the root above `/profiles/<name>` when it names a profile).

```sh
export REPO_ROOT="$(pwd)"
export HERMES_ROOT="$HOME/.hermes"
export PROFILE_DIR="$HERMES_ROOT/profiles/jarvis"
export HERMES_AGENT_DIR="$HERMES_ROOT/hermes-agent"
export HERMES_UV="$HERMES_ROOT/bin/uv"
test -d "$REPO_ROOT/setup" && test -d "$REPO_ROOT/agents/jarvis" && printf 'repo root ready\n'
```

Verify: the last line prints `repo root ready`.

1. Preflight

Check the same requirements the wizard checks: OS, Node, `git`, `curl`, and optional Homebrew. Native Windows proceeds through setup; Windows users should follow the wizard or double-click launcher because the manual command blocks below are written for POSIX shells.

```sh
node --version
git --version
curl --version
if [ "$(uname -s)" = "Darwin" ]; then
  command -v brew >/dev/null 2>&1 && brew --version || printf 'Homebrew not found; nothing JARVIS needs depends on it\n'
fi
```

Verify: Node prints `v22` or newer, and `git` and `curl` print versions.

2. Earlier Install

Only for a machine that already ran JARVIS v1.3.x. v1.4.0 does not leave a
gateway running behind your back, so the old install's background pieces have
to go before the new one starts: the scheduled task or Startup shortcut that
launched a gateway at sign-in, the gateway that task started, and the saved
`API_SERVER_*` settings in the profile — those override the credentials this
app supplies and are the usual cause of chat answering `401`.

The wizard's Earlier Install step does this for you, asking before each change,
and it keeps everything that matters: your provider keys and `.env`, the
profile config, knowledge, workspace and history. Declining is allowed; the
step then says plainly that the engine will probably refuse to start while the
old one holds the profile.

By hand, the same thing:

```sh
# Windows (PowerShell, as your own user): remove both registration styles.
schtasks /Query /FO LIST | Select-String -Pattern 'Hermes_Gateway'   # look first
schtasks /Delete /TN "Hermes_Gateway-jarvis" /F                      # if present
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Hermes_Gateway*.vbs" -ErrorAction SilentlyContinue
```

```sh
# macOS / Linux: stop whatever is serving this profile today.
hermes -p jarvis gateway stop || true
```

Then remove the superseded lines from the profile `.env` — `API_SERVER_KEY`,
`API_SERVER_PORT`, `API_SERVER_HOST`, `API_SERVER_ENABLED` — and leave every
other line alone.

Verify: no `Hermes_Gateway*` task or Startup shortcut remains, and
`grep -E '^API_SERVER' "$PROFILE_DIR/.env"` prints nothing.

3. Hermes

Install Hermes if needed, pin the Hermes Agent checkout to the distribution version, then sync its virtual environment only when the wizard would.

```sh
if [ -d "$HERMES_AGENT_DIR" ] || command -v hermes >/dev/null 2>&1; then
  printf 'Hermes is already installed\n'
else
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
fi

git -C "$HERMES_AGENT_DIR" fetch --tags --depth 1 origin tag v2026.7.1 >/dev/null 2>&1 || true
if git -C "$HERMES_AGENT_DIR" checkout v2026.7.1; then
  printf 'Hermes pinned to v2026.7.1\n'
else
  printf 'WARNING: Hermes pin to v2026.7.1 failed; the installed version is still active and is not the supported pin\n' >&2
fi

if [ -x "$HERMES_UV" ] && [ -f "$HERMES_AGENT_DIR/pyproject.toml" ]; then
  if [ -d "$HERMES_AGENT_DIR/venv" ]; then
    printf 'Hermes virtual environment sync was not needed\n'
  else
    cd "$HERMES_AGENT_DIR"
    VIRTUAL_ENV="$HERMES_AGENT_DIR/venv" "$HERMES_UV" sync
    cd "$REPO_ROOT"
  fi
fi
```

Verify: `hermes --help` prints usage, and `git -C "$HERMES_AGENT_DIR" describe --tags --always` shows `v2026.7.1`.

4. Profile

Install the `agents/jarvis` Hermes profile, copy Jarvis knowledge, seed `operations.md` only if it is missing, and create the workspace folders.

```sh
hermes profile install "$REPO_ROOT/agents/jarvis" --name jarvis --alias -y
mkdir -p "$PROFILE_DIR/knowledge" "$PROFILE_DIR/workspace/briefs" "$PROFILE_DIR/workspace/reports" "$PROFILE_DIR/skills" "$PROFILE_DIR/cron"
cp -Rp "$REPO_ROOT/agents/jarvis/knowledge/." "$PROFILE_DIR/knowledge/"
if [ -f "$PROFILE_DIR/operations.md" ]; then
  printf 'operations.md already present\n'
else
  cp "$REPO_ROOT/agents/jarvis/operations.md" "$PROFILE_DIR/operations.md"
fi
```

Verify: `test -f "$PROFILE_DIR/operations.md" && test -d "$PROFILE_DIR/workspace/briefs" && printf 'profile ready\n'` prints `profile ready`.

5. Compose Roles

Copy role skills and knowledge, merge role personas into `operations.md`, and write the setup cron marker.

```sh
node <<'NODE'
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const repoRoot = process.cwd();
const profileDir = path.join(os.homedir(), ".hermes", "profiles", "jarvis");
const roles = [
  { id: "sarah", title: "Sarah" },
  { id: "tom", title: "Tom" },
  { id: "bobby", title: "Bobby" },
  { id: "scout", title: "Scout" },
  { id: "ava", title: "Ava" },
];

function exists(filePath) {
  try {
    fs.accessSync(filePath);
    return true;
  } catch {
    return false;
  }
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function copyDirectoryContents(sourceDir, targetDir) {
  if (exists(sourceDir) === false) return 0;
  ensureDir(targetDir);
  const entries = fs.readdirSync(sourceDir, { withFileTypes: true });
  for (const entry of entries) {
    fs.cpSync(path.join(sourceDir, entry.name), path.join(targetDir, entry.name), {
      force: true,
      preserveTimestamps: true,
      recursive: true,
    });
  }
  return entries.length;
}

function extractPersonaSection(soul) {
  const withoutTitle = soul.replace(/^# .*(?:\r?\n)+/, "");
  return withoutTitle.split(/\r?\n##\s+/)[0].trim();
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function mergeRoleIntoOperations(existing, roleId, roleTitle, persona) {
  const start = `<!-- jarvis-setup:role:${roleId}:start -->`;
  const end = `<!-- jarvis-setup:role:${roleId}:end -->`;
  const block = `${start}\n### ${roleTitle}\n\n${persona.trim()}\n${end}`;
  const markerPattern = new RegExp(`\\n*${escapeRegExp(start)}[\\s\\S]*?${escapeRegExp(end)}\\n*`, "g");
  const withoutExistingBlock = existing.trimEnd().replace(markerPattern, "\n\n").replace(/\n{3,}/g, "\n\n").trimEnd();
  const rolesHeader = /^## Roles\s*$/m.exec(withoutExistingBlock);

  if (rolesHeader === null) {
    return `${withoutExistingBlock}\n\n## Roles\n\n${block}\n`;
  }

  const headerEnd = rolesHeader.index + rolesHeader[0].length;
  const afterHeader = withoutExistingBlock.slice(headerEnd);
  const nextSection = /\n##\s+/.exec(afterHeader);
  const insertAt = nextSection === null ? withoutExistingBlock.length : headerEnd + nextSection.index;
  const before = withoutExistingBlock.slice(0, insertAt).trimEnd();
  const after = withoutExistingBlock.slice(insertAt).trimStart();

  return `${before}\n\n${block}${after === "" ? "" : `\n\n${after}`}\n`;
}

ensureDir(path.join(profileDir, "skills"));
ensureDir(path.join(profileDir, "knowledge"));
ensureDir(path.join(profileDir, "cron"));

const demoTarget = path.join(profileDir, "demo");
if (exists(demoTarget) === false) {
  fs.cpSync(path.join(repoRoot, "demo"), demoTarget, {
    preserveTimestamps: true,
    recursive: true,
  });
  console.log("Copied demo fixtures into the profile.");
}

const operationsPath = path.join(profileDir, "operations.md");
let operations = exists(operationsPath) ? fs.readFileSync(operationsPath, "utf8") : "# Operations manual\n";

for (const role of roles) {
  const roleDir = path.join(repoRoot, "agents", role.id);
  const skillsCopied = copyDirectoryContents(path.join(roleDir, "skills"), path.join(profileDir, "skills"));
  const knowledgeCopied = copyDirectoryContents(path.join(roleDir, "knowledge"), path.join(profileDir, "knowledge", role.id));
  if (role.id === "tom") {
    const reposPath = path.join(profileDir, "knowledge", "tom", "repos.md");
    const repos = fs.readFileSync(reposPath, "utf8").replaceAll("{{JARVIS_REPO_ROOT}}", repoRoot);
    fs.writeFileSync(reposPath, repos, "utf8");
  }
  const soul = fs.readFileSync(path.join(roleDir, "SOUL.md"), "utf8");
  operations = mergeRoleIntoOperations(operations, role.id, role.title, extractPersonaSection(soul));
  console.log(`${role.title}: copied ${skillsCopied} skill items and ${knowledgeCopied} knowledge items.`);
}

fs.writeFileSync(operationsPath, operations, "utf8");

const markerPath = path.join(profileDir, "cron", "jarvis-setup-registered.json");
let markerJobs = [];
if (exists(markerPath)) {
  const parsed = JSON.parse(fs.readFileSync(markerPath, "utf8"));
  markerJobs = Array.isArray(parsed.jobs) ? parsed.jobs.slice().sort() : [];
}
fs.writeFileSync(markerPath, JSON.stringify({ jobs: markerJobs }, null, 2) + "\n", "utf8");
NODE
```

The wizard asks before each role schedule and defaults to no. To answer yes for a schedule, run that role's exact cron command.

```sh
hermes -p jarvis cron create '0 * * * *' "Run Sarah's triage-inbox skill against the configured support inbox. Budget: keep this run under 15k tokens; stop after the current message and report partial status if the cap would be exceeded. Hermes ships cron disabled on install, so this job runs only after the owner enables cron." --name triage-inbox --skill triage-inbox --deliver slack
hermes -p jarvis cron create '0 6 * * *' "Run Bobby's roas-report skill for yesterday's Meta Ads performance. Budget: keep this run under 20k tokens; if data gathering stalls, write the partial report, state the gap, and stop." --name roas-report --skill roas-report --deliver slack
hermes -p jarvis cron create '0 7 * * 1' "Run the weekly-sweep skill. Budget: keep this run under 25k tokens; if collection or synthesis would exceed it, stop, write the partial report to workspace/reports/scout-YYYY-WW.md, and report the cap." --name weekly-sweep --skill weekly-sweep --deliver slack
hermes -p jarvis cron create '0 10 * * *' "Write today's content hooks per the write-hooks skill. Use the newest Scout angles if no owner brief is present. Budget: keep this run under 15k tokens; stop and report when the cap is hit." --name write-hooks --skill write-hooks --deliver slack
```

The packs ship disabled. After personalising a job's knowledge, credentials, limits, and Slack delivery, list the registered jobs and enable only the one you are ready to trust:

```sh
hermes -p jarvis cron list
hermes -p jarvis cron enable JOB_ID_OR_NAME
```

Verify: `test -f "$PROFILE_DIR/skills/triage-inbox/SKILL.md" && test -f "$PROFILE_DIR/knowledge/sarah/faq.md" && printf 'roles composed\n'` prints `roles composed`.

6. Model

Choose one provider from [playbooks/models.md](playbooks/models.md), store its key in the profile `.env`, set the exact wizard model string, and run the Hermes verification chat.

```sh
printf 'Model provider (anthropic, openrouter, openai): '
IFS= read -r MODEL_PROVIDER
MODEL_PROVIDER="${MODEL_PROVIDER:-anthropic}"

printf 'Provider API key: '
restore_tty_echo() {
  if test -t 0; then stty echo; fi
}
trap restore_tty_echo EXIT HUP INT TERM
if test -t 0; then stty -echo; fi
IFS= read -r PROVIDER_API_KEY
restore_tty_echo
trap - EXIT HUP INT TERM
printf '\n'

if [ -z "$PROVIDER_API_KEY" ]; then
  printf 'No provider key entered; Jarvis stays offline until one is added. Mint a key and rerun this block\n'
  exit 1
fi

case "$MODEL_PROVIDER" in
  anthropic)
    ENV_KEY='ANTHROPIC_API_KEY'
    MODEL_CONFIG_PROVIDER='anthropic'
    MODEL_DEFAULT='anthropic/claude-sonnet-4.6'
    ;;
  openrouter)
    ENV_KEY='OPENROUTER_API_KEY'
    MODEL_CONFIG_PROVIDER='openrouter'
    MODEL_DEFAULT='anthropic/claude-sonnet-4.6'
    ;;
  openai)
    ENV_KEY='OPENAI_API_KEY'
    MODEL_CONFIG_PROVIDER='openai-api'
    MODEL_DEFAULT='gpt-5.5'
    ;;
  *)
    printf 'Unknown provider. Use anthropic, openrouter, or openai\n'
    exit 1
    ;;
esac

export ENV_KEY PROVIDER_API_KEY
node <<'NODE'
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const envPath = path.join(os.homedir(), ".hermes", "profiles", "jarvis", ".env");
const updates = {
  [process.env.ENV_KEY]: process.env.PROVIDER_API_KEY,
  JARVIS_DEMO: "0",
};

function quoteEnvValue(value) {
  return /^[A-Za-z0-9_./:@%+=,-]*$/.test(value) ? value : JSON.stringify(value);
}

function upsertEnvText(existing, values) {
  const lines = existing.split(/\r?\n/);
  const seen = new Set();
  const next = lines.map((line) => {
    const equals = line.indexOf("=");
    if (equals === -1 || line.trim().startsWith("#")) return line;
    const key = line.slice(0, equals).trim();
    if (Object.prototype.hasOwnProperty.call(values, key) === false) return line;
    seen.add(key);
    return `${key}=${quoteEnvValue(values[key] ?? "")}`;
  });

  for (const [key, value] of Object.entries(values)) {
    if (seen.has(key) === false) next.push(`${key}=${quoteEnvValue(value ?? "")}`);
  }

  while (next.length > 0 && next[next.length - 1] === "") next.pop();
  return `${next.join("\n")}\n`;
}

fs.mkdirSync(path.dirname(envPath), { recursive: true });
const existing = fs.existsSync(envPath) ? fs.readFileSync(envPath, "utf8") : "";
fs.writeFileSync(envPath, upsertEnvText(existing, updates), "utf8");
NODE

hermes -p jarvis config set model.provider "$MODEL_CONFIG_PROVIDER"
hermes -p jarvis config set model.default "$MODEL_DEFAULT"
# Pin the assistant's working folder to the profile — without this, a gateway
# launched from the engine checkout injects upstream's contributor guide
# (AGENTS.md) into every prompt. The shipped seed carries the default-root
# spelling; this pins YOUR actual profile dir, custom HERMES_HOME included.
hermes -p jarvis config set terminal.cwd "$PROFILE_DIR"
hermes -p jarvis chat -q "Reply: ok"
```

The wizard never enables demo mode — Jarvis needs a provider key to run. To preview the HUD with clearly-badged sample data, flip DEMO MODE in the HUD settings (gear icon) instead.

Verify: the provider path prints a reply to `Reply: ok`.

7. Slack

Complete [playbooks/slack.md](playbooks/slack.md), then store the three values, validate auth, create the office channels, fill `knowledge/org.md`, and set `SLACK_HOME_CHANNEL`.

```sh
case "$(uname -s)" in
  Darwin) open https://api.slack.com/apps ;;
  Linux) xdg-open https://api.slack.com/apps ;;
  *) printf 'Open https://api.slack.com/apps\n' ;;
esac

printf 'Slack bot token xoxb-: '
restore_tty_echo() {
  if test -t 0; then stty echo; fi
}
trap restore_tty_echo EXIT HUP INT TERM
if test -t 0; then stty -echo; fi
IFS= read -r SLACK_BOT_TOKEN
restore_tty_echo
trap - EXIT HUP INT TERM
printf '\n'

printf 'Slack app token xapp-: '
trap restore_tty_echo EXIT HUP INT TERM
if test -t 0; then stty -echo; fi
IFS= read -r SLACK_APP_TOKEN
restore_tty_echo
trap - EXIT HUP INT TERM
printf '\n'

printf 'Your Slack member ID: '
IFS= read -r SLACK_ALLOWED_USERS
SLACK_OWNER_MEMBER_ID="$SLACK_ALLOWED_USERS"
export SLACK_BOT_TOKEN SLACK_APP_TOKEN SLACK_ALLOWED_USERS SLACK_OWNER_MEMBER_ID

node <<'NODE'
const fs = require("node:fs");
const https = require("node:https");
const os = require("node:os");
const path = require("node:path");

const profileDir = path.join(os.homedir(), ".hermes", "profiles", "jarvis");
const envPath = path.join(profileDir, ".env");
const orgPath = path.join(profileDir, "knowledge", "org.md");
const repoOrgPath = path.join(process.cwd(), "agents", "jarvis", "knowledge", "org.md");
const channels = ["jarvis-office", "support", "dev", "ads", "research", "content", "approvals"];
const roleChannels = {
  Jarvis: "jarvis-office",
  Sarah: "support",
  Tom: "dev",
  Bobby: "ads",
  Scout: "research",
  Ava: "content",
};

function quoteEnvValue(value) {
  return /^[A-Za-z0-9_./:@%+=,-]*$/.test(value) ? value : JSON.stringify(value);
}

function upsertEnvText(existing, updates) {
  const lines = existing.split(/\r?\n/);
  const seen = new Set();
  const next = lines.map((line) => {
    const equals = line.indexOf("=");
    if (equals === -1 || line.trim().startsWith("#")) return line;
    const key = line.slice(0, equals).trim();
    if (Object.prototype.hasOwnProperty.call(updates, key) === false) return line;
    seen.add(key);
    return `${key}=${quoteEnvValue(updates[key] ?? "")}`;
  });
  for (const [key, value] of Object.entries(updates)) {
    if (seen.has(key) === false) next.push(`${key}=${quoteEnvValue(value ?? "")}`);
  }
  while (next.length > 0 && next[next.length - 1] === "") next.pop();
  return `${next.join("\n")}\n`;
}

function upsertEnv(updates) {
  fs.mkdirSync(path.dirname(envPath), { recursive: true });
  const existing = fs.existsSync(envPath) ? fs.readFileSync(envPath, "utf8") : "";
  fs.writeFileSync(envPath, upsertEnvText(existing, updates), "utf8");
}

function slackApi(method, token, params = {}) {
  const body = new URLSearchParams(params).toString();
  return new Promise((resolve) => {
    const request = https.request(
      {
        hostname: "slack.com",
        path: `/api/${method}`,
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/x-www-form-urlencoded",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (response) => {
        let data = "";
        response.on("data", (chunk) => {
          data += chunk.toString("utf8");
        });
        response.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch {
            resolve({ ok: false, error: `http_${response.statusCode ?? "unknown"}` });
          }
        });
      },
    );
    request.on("error", (error) => resolve({ ok: false, error: error.message }));
    request.write(body);
    request.end();
  });
}

async function listSlackChannels(token) {
  const result = {};
  let cursor = "";
  do {
    const response = await slackApi("conversations.list", token, {
      exclude_archived: "true",
      limit: "1000",
      types: "public_channel",
      ...(cursor === "" ? {} : { cursor }),
    });
    if (response.ok === false) return result;
    for (const channel of response.channels ?? []) {
      result[channel.name] = channel.id;
    }
    cursor = response.response_metadata?.next_cursor ?? "";
  } while (cursor.length > 0);
  return result;
}

function updateOrgChannels(orgText, channelIds) {
  const lines = orgText.split(/\r?\n/).map((line) => {
    if (line.startsWith("|") === false) return line;
    const cells = line.split("|").map((cell) => cell.trim());
    const role = cells[1];
    if (typeof role === "undefined" || typeof roleChannels[role] === "undefined") return line;
    const channelId = channelIds[roleChannels[role]];
    if (typeof channelId === "undefined") return line;
    cells[3] = channelId;
    return `| ${cells[1]} | ${cells[2]} | ${cells[3]} | ${cells[4]} | ${cells[5]} |`;
  });

  const approvalsId = channelIds.approvals;
  let text = lines.join("\n").trimEnd();
  if (typeof approvalsId === "string") {
    text = text.replace(
      /Approvals mirror: #approvals(?: \([^)]+\))?\./,
      `Approvals mirror: #approvals (${approvalsId}).`,
    );
  }
  return `${text}\n`;
}

async function main() {
  const botToken = process.env.SLACK_BOT_TOKEN ?? "";
  const appToken = process.env.SLACK_APP_TOKEN ?? "";
  const memberId = process.env.SLACK_ALLOWED_USERS ?? "";

  if (botToken === "" || appToken === "" || memberId === "") {
    console.log("Slack values were incomplete; Slack stays skipped and nothing else changes. No phone brief or remote approvals until playbooks/slack.md is completed.");
    return;
  }

  upsertEnv({
    SLACK_APP_TOKEN: appToken,
    SLACK_ALLOWED_USERS: memberId,
    SLACK_BOT_TOKEN: botToken,
    SLACK_OWNER_MEMBER_ID: memberId,
  });

  const auth = await slackApi("auth.test", botToken);
  console.log(auth.ok === true ? "Slack bot token validated with auth.test." : `Slack auth.test returned ${auth.error ?? "unknown_error"}.`);

  const createdIds = {};
  let printedScopeHint = false;
  for (const channel of channels) {
    const response = await slackApi("conversations.create", botToken, {
      name: channel,
      is_private: "false",
    });
    if (response.ok === true && typeof response.channel?.id === "string") {
      createdIds[channel] = response.channel.id;
      console.log(`#${channel} created.`);
      continue;
    }
    if (response.error === "name_taken") {
      console.log(`#${channel} already exists.`);
      continue;
    }
    if (response.error === "missing_scope") {
      if (printedScopeHint === false) {
        console.log(`Slack is missing ${response.needed === undefined ? "a scope" : `scope ${response.needed}`}; add the scopes from playbooks/slack.md, reinstall the app, and rerun this step.`);
        printedScopeHint = true;
      }
      continue;
    }
    console.log(`#${channel} was not created: ${response.error ?? "unknown_error"}.`);
  }

  const existingIds = await listSlackChannels(botToken);
  const channelIds = { ...existingIds, ...createdIds };
  const orgText = fs.existsSync(orgPath)
    ? fs.readFileSync(orgPath, "utf8")
    : fs.existsSync(repoOrgPath)
      ? fs.readFileSync(repoOrgPath, "utf8")
      : "";
  fs.mkdirSync(path.dirname(orgPath), { recursive: true });
  fs.writeFileSync(orgPath, updateOrgChannels(orgText, channelIds), "utf8");

  if (typeof channelIds["jarvis-office"] === "string") {
    upsertEnv({ SLACK_HOME_CHANNEL: channelIds["jarvis-office"] });
    console.log("SLACK_HOME_CHANNEL set to the #jarvis-office channel ID.");
  } else {
    console.log("SLACK_HOME_CHANNEL was not set because #jarvis-office could not be resolved.");
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
NODE
```

Skipping Slack changes nothing else — the configured model stays live; you lose only the phone brief and remote approvals until `playbooks/slack.md` is completed.

Verify: DM the bot, then in a channel run `/invite @Jarvis` and mention him. The channel IDs should appear in `$PROFILE_DIR/knowledge/org.md`.

8. Start Engine

Start the engine and prove it answers. In v1.4.0 the JARVIS app owns this
process: it starts the engine as its own child, generates the credential and
port at startup and passes them in the child's environment, and stops it when
you quit. There is no service to install, no login task, no fixed port, and
nothing to paste into HUD Settings — the pairing key you may remember from
v1.3.x no longer exists as a thing you handle.

Open JARVIS and the engine starts. The wizard's Start Engine step does the same
thing through the same supervisor and then asks the engine a real question
through its own port, so a green step means chat works, not that a file was
written.

For a terminal-only session — no app running — start one yourself in a window
you keep open:

```sh
hermes -p jarvis gateway run
```

Verify: in the app, HUD Settings shows a live engine port and chat answers. In
a terminal session, `hermes -p jarvis chat -q "Reply: ok"` answers `ok`.

9. Voice

Voice is bundled, not installed. The application ships local speech recognition (whisper.cpp with a `base.en` model) and local speech generation (Kokoro-82M v1.0, voice `bm_george`) as separate signed executables with their model files, on Windows and macOS alike. There is nothing to download and nothing to add to the Hermes virtual environment.

The wizard's Voice step therefore only *verifies*: it checks every bundled file against the SHA-256 manifest that ships beside them, speaks one test sentence, and runs a recognition self-test. It never fails the install — if voice cannot run on a machine, everything else still works and the step says so plainly.

Using it: in the HUD chat bar, hold the microphone button (or hold Space, or Ctrl+Space while typing) and speak. Release, and your words appear as an ordinary chat message; Jarvis then answers aloud. Pressing to talk again stops him mid-sentence. "Speak replies aloud" is a toggle in HUD Settings.

Honest scope: **speech recognition and speech generation run locally on your computer and no audio leaves it. Your request itself still goes to whichever AI provider you configured.** JARVIS is not an offline product.

Verify: HUD Settings shows `VOICE BM_GEORGE` and "Local speech recognition and speech generation are ready." Developers working from a checkout stage the payload first with `node hud/scripts/fetch-voice-assets.mjs`, then `node hud/scripts/fetch-voice-assets.mjs --check`.

10. Install App

Build JARVIS HUD on this machine and install it, so the HUD opens like any other app instead of from a terminal. A locally built bundle carries no quarantine flag, so macOS opens it without a Gatekeeper warning.

On macOS:

```sh
rm -rf "$REPO_ROOT"/hud/dist-app/mac*
pnpm --dir "$REPO_ROOT/hud" exec electron-builder --mac --dir
BUILT_APP="$(ls -d "$REPO_ROOT"/hud/dist-app/mac*/'JARVIS HUD.app' | head -1)"
APPS_DIR=/Applications
[ -w "$APPS_DIR" ] || { APPS_DIR="$HOME/Applications"; mkdir -p "$APPS_DIR"; }
rm -rf "$APPS_DIR/JARVIS HUD.app"
ditto "$BUILT_APP" "$APPS_DIR/JARVIS HUD.app"
```

On Linux. This installs an unpacked application directory rather than an AppImage, because AppImage needs `libfuse.so.2`, which Ubuntu 22.04 and newer no longer ship:

```sh
rm -rf "$REPO_ROOT"/hud/dist-app/linux*unpacked
pnpm --dir "$REPO_ROOT/hud" exec electron-builder --linux dir
BUILT_DIR="$(ls -d "$REPO_ROOT"/hud/dist-app/linux*unpacked | head -1)"
mkdir -p "$HOME/.local/opt" "$HOME/.local/share/icons" "$HOME/.local/share/applications"
rm -rf "$HOME/.local/opt/jarvis-hud"
cp -R "$BUILT_DIR" "$HOME/.local/opt/jarvis-hud"
chmod +x "$HOME/.local/opt/jarvis-hud/jarvis-hud"
cp "$REPO_ROOT/hud/build/icon.png" "$HOME/.local/share/icons/jarvis-hud.png"
cat > "$HOME/.local/share/applications/jarvis-hud.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=JARVIS HUD
Comment=Your self-hosted AI chief of staff
Exec=$HOME/.local/opt/jarvis-hud/jarvis-hud
Icon=$HOME/.local/share/icons/jarvis-hud.png
Terminal=false
Categories=Office;Utility;
DESKTOP
```

On Windows, `pnpm run install-app` builds an NSIS installer under `hud/dist-app` and launches it. If the build or installer fails, the wizard reports that the desktop shortcut was not created but still completes setup; JARVIS continues to run from the launcher or checkout. Run `pnpm run install-app` again at any time to retry.

Verify: on macOS, `test -d "/Applications/JARVIS HUD.app" || test -d "$HOME/Applications/JARVIS HUD.app" && printf 'app installed\n'` prints `app installed`, and JARVIS HUD appears in Spotlight. On Linux, `test -x "$HOME/.local/opt/jarvis-hud/jarvis-hud" && printf 'app installed\n'` prints `app installed`, and JARVIS HUD appears in your applications menu.

11. Hello

Post the office greeting to `#jarvis-office` when Slack is configured; otherwise print it in the terminal.

```sh
node <<'NODE'
const fs = require("node:fs");
const https = require("node:https");
const os = require("node:os");
const path = require("node:path");

const envPath = path.join(os.homedir(), ".hermes", "profiles", "jarvis", ".env");

function readEnv(text) {
  const values = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line === "" || line.startsWith("#")) continue;
    const equals = line.indexOf("=");
    if (equals === -1) continue;
    const key = line.slice(0, equals).trim();
    const value = line.slice(equals + 1).trim();
    values[key] = value.replace(/^["']|["']$/g, "");
  }
  return values;
}

function timeGreeting(date = new Date()) {
  const hour = date.getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

function officeGreeting(date = new Date()) {
  return `${timeGreeting(date)}, sir. The office is open — your staff and I are at your service. My morning brief arrives at 08:00.`;
}

function slackApi(method, token, params = {}) {
  const body = new URLSearchParams(params).toString();
  return new Promise((resolve) => {
    const request = https.request(
      {
        hostname: "slack.com",
        path: `/api/${method}`,
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/x-www-form-urlencoded",
          "Content-Length": Buffer.byteLength(body),
        },
      },
      (response) => {
        let data = "";
        response.on("data", (chunk) => {
          data += chunk.toString("utf8");
        });
        response.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch {
            resolve({ ok: false, error: `http_${response.statusCode ?? "unknown"}` });
          }
        });
      },
    );
    request.on("error", (error) => resolve({ ok: false, error: error.message }));
    request.write(body);
    request.end();
  });
}

async function main() {
  const env = fs.existsSync(envPath) ? readEnv(fs.readFileSync(envPath, "utf8")) : {};
  const greeting = officeGreeting();
  if (typeof env.SLACK_BOT_TOKEN === "string" && typeof env.SLACK_HOME_CHANNEL === "string") {
    const response = await slackApi("chat.postMessage", env.SLACK_BOT_TOKEN, {
      channel: env.SLACK_HOME_CHANNEL,
      text: greeting,
    });
    if (response.ok === true) {
      console.log("Greeting posted to #jarvis-office.");
    } else {
      console.log(`Slack greeting was not posted: ${response.error ?? "unknown_error"}.`);
      console.log(greeting);
    }
  } else {
    console.log(greeting);
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
NODE
```

Verify: the greeting appears in `#jarvis-office`, or the same greeting prints in your terminal.

12. Summary

Print the same next actions the wizard gives at the end.

```sh
printf '%s\n' \
  'Open the HUD from the repository root: pnpm run hud' \
  'Use terminal chat any time: hermes -p jarvis chat -q "hello"' \
  'HUD pairing is automatic — JARVIS starts the engine and supplies its key, nothing to paste.' \
  'DM the Slack bot, then watch #jarvis-office for the office greeting and morning brief.' \
  'Connect Slack when ready: playbooks/slack.md' \
  'Mint an AI provider key when needed: playbooks/models.md' \
  'For revenue, ads, content, and support keys, use playbooks/stripe.md, playbooks/revenuecat.md, playbooks/meta-ads.md, playbooks/postiz.md, and playbooks/email.md.'
```

Verify: run `pnpm run hud` from the repository root for the HUD, or `hermes -p jarvis chat -q "hello"` for terminal chat.

**Launch the HUD**

After the Install App step, open **JARVIS HUD** from Spotlight, the Dock, or your applications menu; the terminal is no longer needed. If you skipped that step, run the HUD from the unzipped repository root instead, and install the app whenever you like:

```sh
pnpm run hud          # run from source
pnpm run install-app  # build and install JARVIS HUD as a desktop app
```

**Local API access (advanced)**

JARVIS runs its engine as a child process and gives it a fresh port and key at
every launch — that is what removed the port-squatting and stale-key failures
of v1.3.x. It also means there is nothing fixed to write into an integration.

So JARVIS publishes where it is listening, in the profile directory:

```
~/.hermes/profiles/jarvis/local-api.json
```

(Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\local-api.json`; with a
custom `HERMES_HOME`, under that root.)

```json
{
  "base_url": "http://127.0.0.1:51234/v1",
  "port": 51234,
  "key": "…",
  "pid": 4321,
  "app_version": "1.4.0",
  "written_at": "2026-08-13T09:41:02.114Z"
}
```

Use it like this: read the file, take `base_url` and `key`, and send an
OpenAI-shaped request with `Authorization: Bearer <key>`.

```sh
BASE=$(python3 -c 'import json,pathlib,os;print(json.loads(pathlib.Path(os.path.expanduser("~/.hermes/profiles/jarvis/local-api.json")).read_text())["base_url"])')
KEY=$(python3 -c 'import json,pathlib,os;print(json.loads(pathlib.Path(os.path.expanduser("~/.hermes/profiles/jarvis/local-api.json")).read_text())["key"])')
curl -s "$BASE/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"jarvis","messages":[{"role":"user","content":"Reply: ok"}]}'
```

What the file promises, exactly:

- **It is the only supported way to find the local API.** The port and the key
  change at every launch, by design. Anything that hard-codes 8642, or reads a
  key out of the profile `.env`, is reading a v1.3.x recipe that no longer
  describes this product.
- **It exists only while a verified engine is running.** JARVIS writes it after
  the engine has answered an authenticated request, and removes it when the
  engine stops or is being replaced. A brief absence during a restart is the
  correct answer to "where is it?" — re-read it, do not cache it.
- **Re-read it for every session.** Caching the port or key across launches
  puts you back in the drift this file exists to remove.
- **`pid` is the JARVIS process supervising the engine.** If that process is
  gone, so is the engine: after a crash the file can survive for a moment, and
  checking the pid is how you tell.
- **The key is a credential.** The file is owner-only (0600) on macOS and
  Linux. On Windows it inherits the profile directory's permissions, exactly as
  the profile `.env` beside it always has.


**Your own MCP servers on the ChatGPT brain (advanced)**

On the ChatGPT brain, an MCP server you list under `mcp_servers` in the
profile's `config.yaml` stays off until you opt it in, with one line per
server:

```
~/.hermes/profiles/jarvis/config.yaml
```

(Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\config.yaml`; with a
custom `HERMES_HOME`, under that root.)

```yaml
mcp_servers:
  shopify-ava:
    command: 'C:\Program Files\nodejs\node.exe'
    args: ['C:\mcp\shopify-ava\index.js']
    env:
      SHOP_DOMAIN: example.myshopify.com
    jarvis_chatgpt: true
  crm:
    url: https://mcp.example.com/mcp
    headers:
      Authorization: 'Bearer ${CRM_TOKEN}'
    jarvis_chatgpt: true
```

Save the file, then quit and reopen JARVIS. Servers without the line stay off,
and the connectors in your ChatGPT account stay off either way.

- What reaches Codex: `command`, `args` and `env` for a server JARVIS starts
  on this computer; `url` and `headers` for a hosted one. Hosted servers must
  speak Streamable HTTP, not SSE. Other keys are ignored on this brain.
- Every value must be text: write `'8080'`, not `8080`. A server with a number
  or true/false in `command`, `args`, `env` or `headers` is left off, with a
  warning in the engine log.
- `${NAME}` placeholders are filled from the profile `.env`. Better still, let
  the server fetch its own credentials when it starts (Windows Credential
  Manager, macOS Keychain), so no secret sits in either file.
- On Windows a full path to the program, as above, is the safest choice;
  plain `node` or `npx` work only if they are on the PATH JARVIS started with.
- Opting a server in approves its tools: JARVIS calls them without asking you
  each time. If the server itself asks for a confirmation, JARVIS declines it,
  so keep sign-off steps that need a human outside JARVIS. Opt in only servers
  you wrote or trust.
- The assistant never edits this file. RUN SETUP AGAIN keeps your entries,
  though not your comments.

**Troubleshooting**
| Symptom | Likely Cause | Fix |
|---|---|---|
| Engine will not start | An earlier JARVIS is still holding the profile, a damaged install, or saved `API_SERVER_*` lines that reject this app's credentials | Open HUD Settings and read the engine line, then COPY DIAGNOSTICS. Run setup again and let the Earlier Install and Start Engine steps run — together they remove the old registrations and say exactly what the engine reported. If Hermes itself is broken, rerun the Hermes and Profile steps above. |
| Slack is silent in channels | The bot was not invited, or the app is missing scopes | In the channel, run `/invite @Jarvis`. If it is still silent, add the scopes from [playbooks/slack.md](playbooks/slack.md), reinstall the Slack app, and rerun the Slack step. |
| Cron did not fire | JARVIS was closed (the engine runs while the app does), or the model was never pinned | Leave JARVIS running — or open it — and check `hermes -p jarvis cron status`. Confirm a model is set from [playbooks/models.md](playbooks/models.md). A sleeping laptop does not run scheduled jobs; that limit is real and not a fault. |
| Voice is silent | The bundled speech files are damaged, or microphone access is blocked | Open HUD Settings and read the VOICE line, then COPY DIAGNOSTICS. "unavailable (assets-corrupt)" or "(assets-missing)" means antivirus quarantined a file — reinstall JARVIS and allow-list its folder. If it says ready but nothing is heard, allow the microphone: Windows `Settings > Privacy & security > Microphone` (including "Let desktop apps access your microphone"), macOS `System Settings > Privacy & Security > Microphone`. |
| HUD chat is offline | The engine has not started, or leftover `API_SERVER_*` lines from v1.3.x override the credentials this app supplies | Never copy a key into Settings — that field is read-only now and the app supplies its own. Run setup again and let the Earlier Install step clear those lines, then the Start Engine step. |
| HUD says no profile but the wizard finished | The saved Hermes root pointer is stale | Delete `~/.config/jarvis/hermes-root` (Windows: `%APPDATA%\jarvis\hermes-root`) and relaunch the HUD. |

**Updating**

Extract the new bundle over your JARVIS folder (or into a fresh one), then run `pnpm install` and `pnpm run wizard` once — the wizard rebuilds itself from the new source before running. Press Enter through the steps — completed steps are retained, not redone — and the wizard also repairs profile settings that older versions seeded incorrectly (it does this even when every step is retained). Finish with the Install App step if you want the desktop app rebuilt.

**Uninstall**

The default is a profile-only removal. It removes the composed `jarvis` profile while leaving Hermes and the extracted product files alone:

```sh
# Quit JARVIS first — the engine stops with it; there is no service to remove.
hermes profile delete jarvis
```

**Warning — full removal:** the command below removes the shared Hermes installation and can affect other profiles. Run it only if this machine has no other Hermes household you mean to keep.

```sh
hermes uninstall
```

Deleting the extracted JARVIS folder also removes the shipped examples and local product files. Confirm its exact path, leave that folder, and delete it explicitly with your file manager; there is deliberately no broad `rm -rf` command here.
