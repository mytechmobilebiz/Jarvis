#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
demo_dir="$(cd "$script_dir/.." && pwd)"

cd "$demo_dir"

while IFS='|' read -r name text; do
  edge-tts --voice en-GB-RyanNeural --text "$text" --write-media "stingers/${name}.mp3"
done <<'EOF'
good-morning-sir|Good morning, sir.
office-open|The office is open.
approval-required|Approval required in support, sir.
bobby-ready|Bobby has the ad desk ready.
sarah-triaged|Sarah has triaged the inbox.
scout-found-angles|Scout found three content angles.
EOF
