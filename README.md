# NUBB-E

NUBB-E ("noobee") is a personal AI that runs on your own laptop: a local brain, an animated face, text and voice chat, saved chats, and a memory about you. It works fully offline, and goes online only to look things up when a question needs it.

The full setup guide comes later. For now, this covers the web, files and voice.

## Web

NUBB-E's brain always runs on this laptop. When there's internet, it can also search the web (DuckDuckGo, no account) and read pages, but only when the answer needs it: news, prices, weather, latest versions, a link you give it, or a fact it isn't sure about. "Explain recursion" or "fix my code" never goes online.

- **Sources:** answers that used the web end with links. Click one to open it in your browser.
- **You're in charge:** say "search for..." or "look it up" to make it search, or tell it not to. The **Web** button in the app turns it off. `web_enabled = false` in `config.toml` turns it off completely.
- **No internet?** NUBB-E answers from what it knows and tells you it couldn't check online. It never hangs waiting for the network.
- **Privacy:** only the search words and the pages it opens go online. Your chats, memories and files never do. Each search and page is logged in `~/.nubbe/web.log`.

Works in the app, the quick bar and voice calls.

## Files

NUBB-E can look at the files on your laptop and answer from them: "what's in my notes about the robot arm?", "what does my config file in ~/Documents/project say?".

- **Reading is free:** listing, reading and searching any folder happen right away, no questions asked.
- **Changing needs your Yes:** before NUBB-E writes a file or runs a command, a card shows the exact file and text, or the exact command. Nothing happens until you press **Yes**. No, closing the window, or 5 minutes with no answer all mean no.
- **Private stuff is off limits:** SSH keys, saved browser passwords and cookies, Windows credentials, password vaults (`.kdbx`), `.env` files and NUBB-E's own data are never opened, even with Yes.
- **Cloud files stay in the cloud:** OneDrive "online-only" files aren't downloaded. NUBB-E only matches their names.
- **Search is fastest in one folder,** like `~/Documents`. A search stops after 15 seconds and says so.
- **Everything is logged** in `~/.nubbe/tools.log` (file names and commands, not your file contents).

This is `workspace_dir = "*"` in `config.toml`. To keep NUBB-E to one folder instead, set something like `workspace_dir = "~/Documents/Notes"`. `""` turns the file tools off. They work in the app and the quick bar. Voice calls skip them to stay fast.

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
