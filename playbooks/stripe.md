# Playbook: Connect Stripe revenue to the HUD (read-only, ~5 minutes)

Stripe is for revenue totals in the HUD and brief. Jarvis does not need refund, payout, customer-write, or subscription-write access. A restricted key keeps the blast radius boring.

## 1. Create the restricted key

1. Open [dashboard.stripe.com](https://dashboard.stripe.com/) and switch to the correct account.
2. Use **Live mode** if you want real revenue, or sandbox if you are testing.
3. Go to **Developers → API keys**.
4. Under **Restricted keys**, click **Create restricted key**.
5. Name it `jarvis-revenue-readonly`.
6. Set **Charges** to **Read**.
7. Set **Balance** to **Read**.
8. Set everything else to **None**.
9. Create the key and copy the value beginning `rk_live_` or `rk_test_`.

Even if leaked, this key can only read charge and balance totals. It cannot refund, charge, transfer, or change customers.

## 2. Add the key to Jarvis

Open `~/.hermes/profiles/jarvis/.env` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\.env`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/.env`) and add this exact line with the restricted key:

```dotenv
STRIPE_RESTRICTED_KEY=rk_live_REPLACE_ME
```

Use an `rk_live_…` key for the real HUD. Use `rk_test_…` only for a demo. Jarvis uses the key through its revenue skills; the setup wizard does not collect it.

## Verify

```sh
curl -sS -u "$STRIPE_RESTRICTED_KEY:" \
  "https://api.stripe.com/v1/balance_transactions?limit=1"
```

A good response contains `"object": "list"`.

## Rotation & housekeeping

Rotate from **Developers → API keys**: create a new restricted key with the same two read permissions, replace `STRIPE_RESTRICTED_KEY` in the profile `.env`, restart the gateway, verify, then expire the old key. Keep the key restricted; never paste an unrestricted `sk_live_…` key into Jarvis.
