# Playbook: Connect Postiz for Ava's scheduler (free self-hosted or hosted, ~10 minutes)

Postiz holds the publishing permissions. Ava schedules approved posts through Postiz; the social profile logins stay inside Postiz.

## Why Postiz

Postiz is open source under the AGPL and can be self-hosted for free. It also has a native MCP server, which gives Hermes a tidy, first-class connection instead of asking the household to improvise around a posting API. That makes it the natural default for a self-hosted product.

## 1. Choose where Postiz lives

### Option A — Self-host it for free

The Postiz Docker Compose setup has been tested on Ubuntu 24.04 with 2 GB RAM and 2 vCPUs:

```sh
git clone https://github.com/gitroomhq/postiz-docker-compose
cd postiz-docker-compose
docker compose up
```

The frontend defaults to [http://localhost:4007](http://localhost:4007).

Always pull the Compose file from the Postiz repository with the command above. Do not copy or snapshot it into Jarvis; Postiz remains the authority for its own deployment recipe.

### Option B — Use Postiz hosted

Create an account at [postiz.com](https://postiz.com/). Hosted plans start at $23/month for the annual Standard plan.

## 2. Connect your social channels

Open the Postiz UI and connect every social channel Ava may schedule to. Confirm the intended accounts appear in Postiz before giving Ava the key; the integration schedules through those existing connections and does not replace their authorization.

## 3. Create the API key

In Postiz, open **Settings → Developers → Public API** and create or copy the API key. The same key works with Postiz's MCP server and its normal public REST API; MCP is the preferred route for Hermes.

> Never paste the API key into chat. Put it into the local profile files yourself and refer to it in conversation only as `POSTIZ_API_KEY`.

## 4. Add the profile environment values

Open `~/.hermes/profiles/jarvis/.env` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\.env`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/.env`) and add:

```dotenv
POSTIZ_API_KEY=REPLACE_WITH_PUBLIC_API_KEY
```

For a self-hosted instance, also add its backend base URL:

```dotenv
POSTIZ_URL=https://YOUR_POSTIZ_BACKEND_URL
```

`POSTIZ_URL` is for self-hosted installations only. It must be the instance's backend URL, which may not be the same address as the frontend on port 4007.

## 5. Register the MCP server

Edit the installed `~/.hermes/profiles/jarvis/config.yaml` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\config.yaml`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/config.yaml`). Paste the complete block below and replace `YOUR_API_KEY` locally. If `mcp_servers` already exists, add only the `postiz` lines under it.

```yaml
mcp_servers:
  hud:
    url: http://127.0.0.1:4870/mcp
  postiz:
    url: https://api.postiz.com/mcp/YOUR_API_KEY
```

The API key is the final path segment; it is not a query parameter.

For self-hosted Postiz, the MCP server is served by the instance's own backend with the same path shape:

```yaml
mcp_servers:
  hud:
    url: http://127.0.0.1:4870/mcp
  postiz:
    url: https://YOUR_POSTIZ_BACKEND_URL/mcp/YOUR_API_KEY
```

Confirm the exact backend URL in your Postiz instance's settings or documentation before saving it. That address varies by Postiz version, and Jarvis will not pretend otherwise.

## Verify

Confirm Hermes sees the server:

```sh
hermes -p jarvis mcp list
```

Then ask Ava for a harmless read-only check:

```sh
hermes -p jarvis chat -q "Ava, list the posts currently scheduled in Postiz. Do not create or publish anything."
```

The `postiz` server should appear in the first command, and Ava should use it to list scheduled posts in the second.

## Troubleshooting

- **`postiz` is absent from `mcp list`** — check that you added it under `mcp_servers` in `~/.hermes/profiles/jarvis/config.yaml` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\config.yaml`; custom `HERMES_HOME`: `<your HERMES_HOME>/profiles/jarvis/config.yaml`), that the YAML matches the shape above, and that the API key occupies the final path segment.
- **Hosted MCP rejects the request** — recreate or recopy the key from **Settings → Developers → Public API**, then replace `YOUR_API_KEY` in the hosted MCP URL.
- **Self-hosted MCP returns a missing-route error** — confirm the exact backend URL in that instance's settings or version-matched documentation. The frontend URL alone is not proof of the MCP backend address.
- **A social channel is missing** — connect it in the Postiz UI first, then repeat the read-only Ava check.
- **MCP is unavailable but the key works** — Ava may use Postiz's public REST API as a fallback. The scheduling skill follows the current Postiz API documentation and stops rather than inventing an endpoint or payload.
