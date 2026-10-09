# Playbook: Install Max-grade ears on Apple Silicon (~10 minutes)

Jarvis's default local faster-whisper path is portable and good enough. On Apple Silicon, NVIDIA Parakeet through `parakeet-mlx` can feel near-instant instead of the usual few hundred milliseconds. This is local STT: no extra cloud key, no audio leaving the machine.

## 1. Install Parakeet MLX

Make sure `ffmpeg` is present:

```sh
brew install ffmpeg
```

Install the CLI with `uv`:

```sh
uv tool install parakeet-mlx -U
```

Or use the active project environment:

```sh
uv pip install parakeet-mlx -U
```

`pipx install parakeet-mlx` is also fine if that is how this Mac keeps Python tools.

## 2. Test the command

Run:

```sh
parakeet-mlx --help
```

Then transcribe a small WAV:

```sh
parakeet-mlx /path/to/test.wav --output-format txt --output-dir /tmp
```

The CLI evolves. Current builds use `parakeet-mlx <audio_file> --output-format txt`; older or wrapped builds may expose a `transcribe` subcommand. Trust `parakeet-mlx --help` on the machine in front of you.

## 3. Add the Hermes custom STT block

Open the live `~/.hermes/profiles/jarvis/config.yaml` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\config.yaml`; if you set `HERMES_HOME` yourself, use `<your HERMES_HOME>/profiles/jarvis/config.yaml`) — that is the file Hermes actually reads once the household is installed. (`agents/jarvis/config.yaml` in this repo is only the template that seeds a *fresh* profile install; editing it after setup changes nothing until you reinstall.) Replace the existing `stt:` block with:

```yaml
stt:
  provider: parakeet
  providers:
    parakeet:
      type: command
      command: "parakeet-mlx transcribe {input_path} --output-txt {output_path}"
      format: txt
      timeout: 300
```

If `parakeet-mlx --help` shows a different CLI, adapt only the `command` line. The contract is fixed: Hermes passes `{input_path}` as audio, and the command must write plain text to `{output_path}`.

`voice.silence_duration: 1.2` ships in the profile template and lands in the same live config; that setting is often the bigger perceived-latency lever because it controls how quickly push-to-talk stops listening after you stop speaking.

## 4. Hand the setting to the wizard

There is no secret for this playbook:

| Value | Where it lives |
|---|---|
| `stt.provider` | `~/.hermes/profiles/jarvis/config.yaml` (Windows: `%LOCALAPPDATA%\hermes\profiles\jarvis\config.yaml`; custom `HERMES_HOME`: `<your HERMES_HOME>/profiles/jarvis/config.yaml`) |
| Custom command | The `parakeet-mlx` command verified with `--help` |
| `voice.silence_duration` | Already `1.2` in the live config |

Restart the gateway after changing the config.

## Verify

In the Hermes terminal, run `/voice on` first — voice starts disabled on every launch. Then tap `Ctrl+B`, speak one short sentence, and pause; the recording ends on its own and you can watch the response time.

For a command-line sanity check:

```sh
parakeet-mlx /path/to/test.wav --output-format txt --output-dir /tmp
```

## Rotation & housekeeping

Keep `parakeet-mlx` current with the same install command, `uv tool install parakeet-mlx -U`. If an update changes CLI flags, rerun `parakeet-mlx --help`, update only the Hermes command string, and leave the rest of the STT block alone. On Intel Macs, skip this and stay with faster-whisper.
