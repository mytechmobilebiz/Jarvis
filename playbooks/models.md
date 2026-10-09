# Playbook: Give Jarvis his thinking (~5 minutes)

In v1.4.0 the Model step has one route: **paste an API key** — from Anthropic,
OpenRouter or OpenAI. Any one of the three works; you can change providers later
without rebuilding the household.

**Account sign-in (no API key) is not offered in this release.** The machinery
is built, but it is switched off until it meets the bar this product sets for
credentials: in particular, the ChatGPT/Codex route put the engine's credential
resolution on a path that can copy the Codex app's own credential file
(`~/.codex/auth.json`) into Hermes' store, and JARVIS will not ship a route that
touches a file another vendor's CLI owns. If a profile from an earlier build is
still configured that way, JARVIS clears the route on the next launch and asks
you to set up a provider again.

> **Critical:** do not drive Hermes with a Claude Max or Claude Pro subscription login. Anthropic treats third-party harness use of consumer subscriptions as out of bounds, and accounts do get banned. Use API keys only for Anthropic — the wizard enforces this and will refuse to write such a configuration. Claude Code on the owner's own subscription for Tom's coding lane is fine; that is first-party Claude Code use.

## 1. Choose an API-key provider

1. Anthropic: open [console.anthropic.com](https://console.anthropic.com/) → **API keys** → **Create key**. Copy the key beginning `sk-ant-`.
2. OpenRouter: open [openrouter.ai/keys](https://openrouter.ai/keys) → **Create API key**. One key can route to many models, including free models when OpenRouter lists them as free.
3. OpenAI: open [platform.openai.com/api-keys](https://platform.openai.com/api-keys) → **Create new secret key**. Copy the key beginning `sk-`.

Least privilege is simple here: create one Jarvis-specific key, name it `jarvis`, and do not reuse a personal development key.

## 2. Set the model string

Jarvis reads the key from the environment and the provider and model from Hermes config:

```sh
hermes -p jarvis config set model.provider <provider>
hermes -p jarvis config set model.default <model>
```

Always set both values: `model.provider` uses the engine provider id (`anthropic`, `openrouter`, or `openai-api`), and `model.default` uses the model id as that provider's own catalog names it — `anthropic/<model>` for Anthropic, the vendor-prefixed catalog id for OpenRouter, or the bare id for OpenAI:

```sh
hermes -p jarvis config set model.provider anthropic
hermes -p jarvis config set model.default anthropic/claude-sonnet-4.6
```

Other examples:

```sh
hermes -p jarvis config set model.provider openrouter
hermes -p jarvis config set model.default deepseek/deepseek-chat
hermes -p jarvis config set model.provider openai-api
hermes -p jarvis config set model.default gpt-5.5
```

## 3. Know the cost

A normal office day on mid-tier models should usually land in single-digit dollars. Heavy coding, long research, or image/video work costs more. The spend happens on your own provider account, so your provider's console is where the authoritative numbers (and any hard billing limits you set there) live. Watching your first week's usage there is the honest way to calibrate.

## 4. Hand the value to the wizard

The setup wizard (the first-run screen in the JARVIS app, or `pnpm run wizard` from a source checkout) asks for the provider key and model:

| Value | Where you got it |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Console → API keys |
| `OPENROUTER_API_KEY` | OpenRouter → Keys |
| `OPENAI_API_KEY` | OpenAI Platform → API keys |
| `model.default` | The model id in the provider's own catalog form |

Only one provider key is required.

## Verify

```sh
hermes -p jarvis chat -q "hello"
```

Jarvis should answer once, without asking you to log in to a consumer chatbot.

## Rotation & housekeeping

Rotate by creating a new provider key and running setup again (JARVIS settings → RUN SETUP AGAIN — the Model step takes the new key and restarts the engine with it). Delete the old key in the provider console after the new one verifies. If spend jumps, set a billing limit in your provider's console, or switch to a cheaper model the same way.
