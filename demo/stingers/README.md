# Halo Demo Stingers

Render from the `demo/` directory so the output path matches the examples.

| Name | Spoken line | Render command |
| --- | --- | --- |
| `good-morning-sir` | Good morning, sir. | `edge-tts --voice en-GB-RyanNeural --text "Good morning, sir." --write-media stingers/good-morning-sir.mp3` |
| `office-open` | The office is open. | `edge-tts --voice en-GB-RyanNeural --text "The office is open." --write-media stingers/office-open.mp3` |
| `approval-required` | Approval required in support, sir. | `edge-tts --voice en-GB-RyanNeural --text "Approval required in support, sir." --write-media stingers/approval-required.mp3` |
| `bobby-ready` | Bobby has the ad desk ready. | `edge-tts --voice en-GB-RyanNeural --text "Bobby has the ad desk ready." --write-media stingers/bobby-ready.mp3` |
| `sarah-triaged` | Sarah has triaged the inbox. | `edge-tts --voice en-GB-RyanNeural --text "Sarah has triaged the inbox." --write-media stingers/sarah-triaged.mp3` |
| `scout-found-angles` | Scout found three content angles. | `edge-tts --voice en-GB-RyanNeural --text "Scout found three content angles." --write-media stingers/scout-found-angles.mp3` |

`render-all.sh` loops these same lines and writes the MP3 files into this directory. Do not commit rendered audio unless the release packaging explicitly needs it.
