# Playbook: Connect RevenueCat for app revenue (read-only, ~5 minutes)

Use this only if the business sells mobile subscriptions or in-app purchases through RevenueCat. Jarvis needs overview metrics for the brief and HUD, not customer mutation rights.

## 1. Create a read-only v2 secret key

1. Open [app.revenuecat.com](https://app.revenuecat.com/) and choose the project.
2. Go to **Project settings → API keys**.
3. Click **New** or **Create secret key**.
4. Choose **V2** as the key version.
5. Grant only the Charts & Metrics overview read permission, currently named `charts_metrics:overview:read`.
6. Generate the key and copy it once.

Do not use a public SDK key. Do not grant customer, entitlement, refund, product, or webhook write permissions for the HUD.

## 2. Find the project ID

In **Project settings**, copy the Project ID, currently shown as a value like `proj1ab2c3d4`. If the UI has moved, the same ID appears in RevenueCat API v2 URLs as `/v2/projects/{project_id}`.

## 3. Add the values to Jarvis

Open `~/.hermes/profiles/jarvis/.env` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\.env`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/.env`) and add these exact lines with the project values:

```dotenv
REVENUECAT_API_KEY=REPLACE_WITH_V2_SECRET_KEY
REVENUECAT_PROJECT_ID=proj_REPLACE_ME
```

Jarvis uses these values through its revenue skills and REST calls when needed; the setup wizard does not collect them.

## Verify

```sh
curl -sS \
  -H "Authorization: Bearer $REVENUECAT_API_KEY" \
  "https://api.revenuecat.com/v2/projects/$REVENUECAT_PROJECT_ID/metrics/overview?currency=USD"
```

A good response contains `overview_metrics`.

## Rotation & housekeeping

Rotate in **Project settings → API keys**: create a new v2 read-only key, replace `REVENUECAT_API_KEY` in the profile `.env`, restart the gateway, then revoke the old key. Skip this whole playbook if you do not sell mobile subscriptions; Stripe or manual revenue notes are cleaner.
