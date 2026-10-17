# kivi-linux

you talk. kivi types.

A Linux take on [heykivi.ai](https://heykivi.ai), powered by [Sarvam AI](https://sarvam.ai). Hold a key,
speak, let go, and clean text lands in whatever app has focus. It works in Hindi, Hinglish, English and
the other Indian languages `saaras:v4` understands. A small pill sits on your desktop and shows you
what kivi is doing. It also hosts a voice agent that can open things and type for you.

## what it does

| hold | kivi |
|---|---|
| **right ctrl** | **dictate.** filler words go, punctuation gets fixed, "at five, no wait, six" becomes "at six". Then it's pasted. |
| **right ctrl + right shift** | **edit.** select text and say "make this formal" or "turn into bullets". The selection is replaced. With nothing selected, kivi answers your question in a card. |
| **right alt** | **voice agent.** "open youtube", "launch firefox", "type a polite no to this email", "remember the word Sarvam". It answers out loud. |

- **double-tap** right ctrl for hands-free: kivi keeps listening until you tap again.
- **click the pill** to talk to the agent, and click again to send. Right-click it for settings, pause and quit.
- a quick tap, or right ctrl used inside a shortcut like ctrl+c, never starts dictation.
- **memory**: teach kivi names and jargon (`kivi add-word Prasoon`, or the memory page). They're sent to
  speech-to-text as key terms.
- **style**: set a persona, such as "technical and terse" or "casual, all lowercase".
- **history**: every run is saved locally so you can copy it again later.

All of these can be changed in **kivi settings** (app menu → kivi).

## install

```sh
./install.sh
```

This links `kivi` into `~/.local/bin`, adds the app icon and menu entry, and starts the `kivi` systemd user
service. When it finishes it prints the one `sudo` line you still need (if any), usually:

```sh
sudo pacman -S --needed gtk4-layer-shell wl-clipboard && sudo usermod -aG input $USER
```

The `input` group lets kivi see your hotkeys. Log out and back in after joining it, or run
`sudo setfacl -m u:$USER:rw /dev/input/event*` to get access now (it lasts until reboot). Without
it, kivi still works through the pill or `kivi toggle`, which you can bind to any shortcut in system settings.
`gtk4-layer-shell` is optional and gives a cleaner always-on-top pill. `wl-clipboard` is optional too.

Put your Sarvam API key in **kivi settings → api key**. It's stored in `~/.config/kivi/api_key` (chmod 600).
You can also set `$SARVAM_API_KEY`.

Requirements: KDE Plasma 6 on Wayland is the main target. X11 and GNOME work with a plainer pill.
You also need pipewire, python-evdev, gtk4 and libadwaita.

## command line

```
kivi                      run the daemon (the service does this for you)
kivi settings             open the settings window
kivi toggle [dictate|agent|command]
kivi add-word WORD        teach kivi a word
kivi words                list learned words
kivi test FILE.wav        speech-to-text + cleanup on a file, without typing anything
kivi status               is it running, and is everything set up
```

Logs: `journalctl --user -u kivi -f`.

## config

`~/.config/kivi/config.toml`. Settings writes it for you; every key is listed in
[`config.example.toml`](config.example.toml).

## how it works

evdev hotkeys (works on any compositor) → `pw-record` at 16 kHz → Sarvam `speech-to-text` (saaras:v4) →
`sarvam-105b-conversations` cleanup with reasoning off → kivi puts the text on the clipboard, a virtual
uinput keyboard presses ctrl+v, and your old clipboard comes back. Expect about a second from letting
go to seeing text. The agent uses Sarvam chat with tools, and speaks through `bulbul:v3`.

## limits

- Sarvam's synchronous speech-to-text takes clips up to about 30 s, so kivi stops listening at 29 s.
- Terminals want `paste_keys = "ctrl+shift+v"`. Per-app paste keys aren't supported yet.
- Clipboard managers will see each dictation.
