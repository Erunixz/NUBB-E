# NUBB-E

NUBB-E ("noobee") is a personal AI that runs on your own laptop: a local brain, an animated face, text and voice chat, saved chats, and a memory about you. It works fully offline.

The full setup guide comes later. For now, this covers files and voice.

## Files

NUBB-E can read and search the files in one folder: `workspace_dir` in `config.toml` (by default the `workspace` folder here, which stays off GitHub). Point it at your notes, for example `workspace_dir = "~/Documents/Notes"`, and ask things like "what's in my notes about the robot arm?".

- **Looking is free:** listing, reading and searching happen right away.
- **Changing needs your Yes:** before NUBB-E writes a file or runs a command, a card shows the exact file and text, or the exact command. Nothing happens until you press **Yes**. No, closing the window, or 5 minutes with no answer all mean no.
- **One folder only:** paths outside it are refused, including `..` tricks and links that point outside.
- **Everything is logged** in `~/.nubbe/tools.log` (file names and commands, not your file contents).

File tools work in the app and the quick bar. Voice calls skip them to stay fast. Set `workspace_dir = ""` to turn them off.

## Voice

Press **Call** in the app and just talk, like a phone call. NUBB-E answers out loud when you pause, then listens again. Talk over it to interrupt. Press **End call** to hang up. Each call is saved as one "Call:" chat.

Headphones work best. With laptop speakers, NUBB-E filters out its own voice so it doesn't interrupt itself, but loud speakers can still confuse it, so keep the volume moderate.

Speech runs on the CPU, so the GPU stays free for the brain:

- **Ears:** Whisper `base.en` (faster-whisper)
- **Voice:** Kokoro-82M, voice `am_puck` (change `kokoro_voice` in `config.toml`)
- **Backup voice:** Piper `en_US-amy-medium` in `voices/`, used only if Kokoro can't load

### One-time setup

1. Install espeak-ng (Kokoro uses it for unusual words): `winget install eSpeak-NG.eSpeak-NG`
2. Install the Python parts: `.venv\Scripts\python -m pip install -r requirements.txt`
3. With internet on, start voice once (press Call, or run `.venv\Scripts\python nubbe_voice.py`). It downloads the speech models (about 800 MB) to `~/.cache/huggingface`.

### Offline

After that first run, voice works with no internet. NUBB-E loads the speech models from the local cache with `HF_HUB_OFFLINE=1` set automatically, so it never waits on the network. It only goes online if the models are missing.

To check it yourself: turn Wi-Fi off, start the app, and press Call.

### Mic too sensitive, or not hearing you?

Run the mic test. It listens to your room for 3 seconds, then to you for 4 seconds, suggests a `mic_threshold`, and asks before saving it:

```
.venv\Scripts\python nubbe_voice.py --mic-test
```

### Speed

On this laptop, NUBB-E starts speaking about 3 to 4.5 seconds after you stop talking: 0.6 s to be sure you're done, about 0.8 s to understand you, 1 to 2 s for the brain to start its answer, and about 0.6 to 1.4 s to say its first words.
