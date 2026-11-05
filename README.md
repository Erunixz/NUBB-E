# NUBB-E

NUBB-E ("noobee") is a personal AI that runs on your own laptop: a local brain, an animated face, text and voice chat, saved chats, and a memory about you. It works fully offline, and goes online only to look things up when a question needs it.

The full setup guide comes later. For now, this covers the web, files, hands and voice.

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

## Hands (filling in forms)

Open a form or doc, then ask NUBB-E from the quick bar or a call: "fill this out for me". It looks at the screen, works out what goes in each field, and shows you the plan on a card first, including which window it will type into. Nothing is typed until you press **Yes**.

- **It only uses what you told it:** your memories, your request and the chat. If something's missing (like your email), it leaves that field and asks you.
- **Never:** passwords, payment, bank or ID fields, and it never presses Submit or Send. That part is always yours.
- **You can stop it anytime:** press **Esc** or move the mouse, and it stops right away.
- **It checks its work:** after each field it looks again, and stops if the text didn't land where it should.
- **Safe places only:** it won't type into terminals or code editors (typing there could run commands), and it stops if another window moves in front of the form.
- **Not yet:** dropdowns, checkboxes and radio buttons. Only the main screen.

Every step is logged in `~/.nubbe/tools.log` (field names, not what was typed). Set `hands = false` in `config.toml` to turn it off.

## Everyday actions

Just say or type it, in the app, the quick bar or a call:

- **Apps, folders, sites:** "open Calculator", "launch VS Code", "open my downloads folder", "open YouTube and search for lo-fi", "google best pizza near me", "go to github.com"
- **Sound and music:** "volume 30", "turn it down", "mute", "pause the music", "next song", "what's playing?", "play Billie Jean on Spotify"
- **Timers and reminders:** "timer 10 minutes", "10 minute timer for the pasta", "remind me at 6 to call mom", "remind me tomorrow at 9am to submit the report", "what are my reminders", "cancel the timer". When one is due, NUBB-E says it out loud, the face reacts and a Windows notification shows, even with no NUBB-E window open.
- **To-do list and notes:** "add milk and eggs to my list", "what's on my list", "cross milk off my list", "take a note: ...", "search my notes for wifi". The **Lists** button in the app shows them all.
- **Routines:** "make a routine called start my day: open Spotify and open Gmail", then just "start my day".
- **Good morning:** the date, the weather (when Web is on), today's reminders and your list.
- **Your PC:** "how's my laptop doing" (CPU, memory, battery, disk, GPU), "find my resume" (by name, shown in File Explorer, never opened or run).

Simple commands like these are understood without the brain, so they happen in about a tenth of a second, even while the brain is asleep. Anything else goes to the brain, which can use the same actions. Opening things, sound, music, timers and notes just happen. Clearing the list or deleting a note or routine shows a Yes/No card first. "Play X on Spotify" looks the song up online (when Web is on) to start the exact track; offline it opens the search in Spotify.

## Voice

Press **Call** in the app and just talk, like a phone call. NUBB-E answers out loud when you pause, then listens again. Talk over it to interrupt. Press **End call** to hang up. Each call is saved as one "Call:" chat.

Headphones work best. With laptop speakers, NUBB-E filters out its own voice so it doesn't interrupt itself, but loud speakers can still confuse it, so keep the volume moderate.

Speech runs on the CPU, so the GPU stays free for the brain:

- **Ears:** NVIDIA Parakeet TDT 0.6B v2 (int8, run with onnx-asr), about 0.1 s per sentence. Whisper `base.en` (faster-whisper) is the backup, or set `ears = "whisper"` in `config.toml`
- **Hearing you talk:** Silero VAD (the copy inside faster-whisper), so fans, clicks and music don't start a turn
- **Voice:** Kokoro-82M, voice `am_puck` (change `kokoro_voice` in `config.toml`). Short lines like "On it." are made once and saved in `~/.nubbe/cues/`, so they play at once
- **Backup voice:** Piper `en_US-amy-medium` in `voices/`, used only if Kokoro can't load

### "Hey NUBB-E"

Set `wake_phrase = true` in `config.toml` and NUBB-E listens all the time: say "Hey NUBB-E, what's the time?" and it answers. For a few seconds after it answers, follow-ups need no name. A session never lasts more than 2 minutes, so a TV can't keep it open. Anything that doesn't start with its name is dropped right away, never saved or logged. Windows shows the mic as in use while it listens. It's off by default.

### Credits

- Parakeet TDT 0.6B v2 by NVIDIA, CC-BY-4.0, ONNX export by Ilya Stupakov (istupakov/parakeet-tdt-0.6b-v2-onnx)
- onnx-asr by Ilya Stupakov, MIT
- Silero VAD by Silero Team, MIT
- Kokoro-82M by hexgrad, Apache 2.0
- faster-whisper by SYSTRAN, MIT

### One-time setup

1. Install espeak-ng (Kokoro uses it for unusual words): `winget install eSpeak-NG.eSpeak-NG`
2. Install the Python parts: `.venv\Scripts\python -m pip install -r requirements.txt`
3. With internet on, start voice once (press Call, or run `.venv\Scripts\python nubbe_voice.py`). It downloads the speech models (about 1.5 GB with Parakeet) to `~/.cache/huggingface`.

### Offline

After that first run, voice works with no internet. NUBB-E loads the speech models from the local cache with `HF_HUB_OFFLINE=1` set automatically, so it never waits on the network. It only goes online if the models are missing.

To check it yourself: turn Wi-Fi off, start the app, and press Call.

### Mic too sensitive, or not hearing you?

Run the mic test. It listens to your room for 3 seconds, then to you for 4 seconds, suggests a `mic_threshold`, and asks before saving it:

```
.venv\Scripts\python nubbe_voice.py --mic-test
```

### Speed

On this laptop, NUBB-E starts speaking about 2.5 to 4 seconds after you stop talking: 0.6 s to be sure you're done, about 0.1 s to understand you (Parakeet; Whisper took about 0.5 to 0.8 s), 1 to 2 s for the brain to start its answer, and about 0.6 to 1.4 s to say its first words. If the brain was asleep, it starts loading the moment you press Call, open the quick bar or say "Hey NUBB-E".
