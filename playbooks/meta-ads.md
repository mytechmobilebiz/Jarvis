# Playbook: Connect Meta Ads for Bobby (read-only, ~15 minutes)

Bobby needs yesterday's spend and campaign performance, not the keys to the kingdom. The least-risk path is a Meta developer app, Marketing API, and a System User token with `ads_read`. Dev mode is enough for your own ad account; App Review is only for serving other businesses' ad accounts or building a public SaaS.

## 1. Create the developer app

1. Open [developers.facebook.com/apps](https://developers.facebook.com/apps) → **Create App**.
2. Choose **Business** as the app type → **Next**.
3. Name it `Jarvis Ads Reader`, attach the correct Business portfolio if Meta asks, then **Create app**.
4. On the app dashboard, find **Add products to your app** → **Marketing API** → **Set up**.

Leave the app in development mode. Your own business assets and test users work there; Standard Access and App Review are for broader access.

## 2. Create a read-only System User token

1. Open [business.facebook.com/settings](https://business.facebook.com/settings) for the business that owns the ad account.
2. Go to **Users → System users** (currently under **Business Settings**).
3. **Add** a system user named `jarvis-readonly`; choose the employee/read-only style role, not admin.
4. Select the system user → **Add assets** → **Ad accounts** → choose the ad account → grant read/reporting access only → **Save changes**.
5. Select the system user → **Generate new token** → choose the Jarvis app → add `ads_read` → generate and copy the token.

Do not add `ads_management` unless Bobby's future action mode is enabled and guarded by approvals.

## 3. Find the ad account ID

Open Meta Ads Manager → **Account overview** or **Ad account settings**. Copy the ID and write it as `act_1234567890`, including the `act_` prefix.

## 4. Vet and add a Meta Ads MCP server

Meta does not provide a pinned MCP package in this kit. A community package such as `@meta/ads-mcp` may be an option, but the buyer must inspect its source, permissions, maintenance state, and current install command before giving it an access token. If it passes that review, add the vetted server to the Jarvis profile using the command published by that project.

Jarvis expects the resulting tools to be available under the `meta-ads` MCP name. Keep the server read-only unless the owner has separately enabled Bobby's guarded action mode.

## 5. Add the values to Jarvis

Open `~/.hermes/profiles/jarvis/.env` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\.env`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/.env`) and add these exact lines:

```dotenv
META_ACCESS_TOKEN=REPLACE_WITH_SYSTEM_USER_TOKEN
META_AD_ACCOUNT_ID=act_REPLACE_ME
```

Jarvis uses these variables through Bobby's skills and the buyer-vetted MCP tools; the setup wizard does not collect them.

Report-only mode and demo mode always work, even if Meta wants extra business verification before broader access.

## Verify

In Slack, ask Bobby in `#ads`: `Bobby, what was yesterday's Meta spend?`

Or run:

```sh
hermes -p jarvis chat -q "Bobby, report yesterday's Meta spend."
```

He should return spend from the configured `act_…` account, not demo numbers.

## Rotation & housekeeping

Rotate from **Business Settings → Users → System users**: generate a new token for the same system user, replace `META_ACCESS_TOKEN` in the profile `.env`, restart the gateway, then revoke the old token. Keep `ads_read` as the default. Business verification states vary by account; if Meta blocks live API access, leave Bobby in report-only demo mode until the business account is cleared.
