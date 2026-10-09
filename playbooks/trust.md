# Is this safe? (read this first, or paste it into ChatGPT or Claude)

If you asked an AI assistant whether JARVIS is a "red flag" because it is a
"third-party platform" using your ChatGPT or Claude subscription, here is the
precise picture so it can judge the real thing instead of a guess.

## What actually runs

- **Everything runs on your own computer.** There is no JARVIS server in the
  loop. The only thing that ever leaves your machine for us is anonymous setup
  telemetry (which steps worked, on what kind of computer), and you can switch
  that off (see `README.md`, "Setup telemetry").
- **The brain is the official app, under your own login.** Setup installs the
  official Claude Code or Codex app from Anthropic's or OpenAI's own releases
  and starts their sign-in. JARVIS then drives that app locally, the same way
  you would from a terminal. Your sign-in lives inside that official app.
- **JARVIS never touches your credentials.** The engine is patched so it never
  reads the official app's credential files, never copies or replays a token,
  and never forwards a request through anyone's server on your plan. The patch
  series is in `hud/vendor/engine-patches/` (see its README, patches 0001 and
  0002) and is pinned by tests.
- **You can stop it any time** by signing out of that official app. Nothing is
  stored that survives that.

## What the assistants are warning about

A "third-party platform" in the sense of the providers' terms is a service that
offers a Claude or ChatGPT login on its own website, or that sends your prompts
through its own servers using your plan. JARVIS does neither, by design: no
login form of ours, no proxy, no shared account.

## If you would rather not use your plan at all

Pick **Advanced: use an API key instead** on the first setup screen (Anthropic,
OpenAI or OpenRouter). That key is scoped to JARVIS, revocable in one click at
your provider, and billed pay-as-you-go. `playbooks/models.md` walks through it.

## Two honest caveats

- The providers write their own terms and can change them. JARVIS was built to
  the line they have published (their own app, your own login, no credential
  handling), and the API-key route exists so you are never stranded if that
  line moves.
- The Windows installer is not code-signed yet, so Windows shows a SmartScreen
  warning once. The Mac app is signed and notarized.

## Verify it yourself

You are holding the full source. Search it for what it does with credentials,
or hand this folder to the assistant of your choice and ask it the precise
question. Terms and privacy: https://jarvis-agent.tech/terms and
https://jarvis-agent.tech/privacy. Questions: jarvis.agent.tech@pm.me.
