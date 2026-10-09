# JARVIS Zero-Key Demo Data

Set `JARVIS_DEMO=1` to give a buyer the full walk-in moment without requiring Stripe, RevenueCat, ad-platform, email, or YouTube credentials. The demo company is Halo, an invented solo-founder sleep-tracking iOS app at $18,200 MRR with a $30,000 target.

## Demo Surfaces

- Morning brief: reads `demo/revenue-series.json`, `demo/ads-insights.json`, `demo/inbox.json`, and `demo/comments.json`, then speaks in Jarvis's register. `demo/brief-2026-07-07.md` is the canonical sample brief.
- Bobby: reads `demo/ads-insights.json` for yesterday's eight creative rows, the $309.90 spend total, the 2.2 blended ROAS, the winning "Morning Routine v2" creative, and the trailing "Testimonial Static" creative.
- Sarah: triages `demo/inbox.json` into FAQ answers, refund decisions, one crash escalation, duplicate Android requests, and spam.
- Scout: reads `demo/comments.json` across four YouTube-style videos and extracts the three buried content angles: naps, couples or two-person tracking, and comparisons to SleepCycle.
- HUD: animates `demo/revenue-series.json`, including weekend dips, the June 18 "featured by Apple" spike, and the current $18,200 MRR line against the $30,000 target.
- Stingers: `demo/stingers/README.md` lists the spoken lines, and `demo/stingers/render-all.sh` can render them with `edge-tts` when audio assets are needed.

To switch the demo off, unset `JARVIS_DEMO` or set it to `0` before starting JARVIS. With demo mode disabled, agents should return to their configured live connectors and should treat missing API keys as setup gaps instead of silently reading from `demo/`.
