# FRIDAY — always-on deployment

Run FRIDAY as a background **systemd user service** that starts on login,
auto-restarts on crash, and logs to journald.

## Install

```sh
pip install -e .            # if not already (provides the `friday` command)
./deploy/install-service.sh
```

This writes `~/.config/systemd/user/friday.service` (from
`friday.service.in`, with your repo path + `friday` binary path filled in),
then enables and starts it.

## Manage

```sh
systemctl --user status friday        # is it running?
systemctl --user restart friday       # after a code change / .env edit
systemctl --user stop friday          # pause it
journalctl --user -u friday -f        # live logs
```

## Remove

```sh
systemctl --user disable --now friday
rm ~/.config/systemd/user/friday.service
systemctl --user daemon-reload
```

## How it activates (wake word)

In always-on mode FRIDAY ignores ambient speech and only responds when you
address it by its wake word:

> **"Friday, what's the weather?"**

Configure in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `WAKE_WORD` | `friday` | Word that must prefix a command |
| `WAKE_WORD_ENABLED` | `1` | `0` = respond to every utterance |
| `TTS_ENGINE` | `gemini` | `kokoro` for instant local (robotic) voice |
| `TTS_GEMINI_VOICE` | `Kore` | Puck, Charon, Aoede, Fenrir, Leda, … |
| `FRIDAY_NO_HUD` | unset | `1` = run headless (no Electron HUD) |

## Notes

- **Display/audio:** the unit sets `WAYLAND_DISPLAY=wayland-0` and `DISPLAY=:0`
  and imports the session environment so the HUD, `ydotool` and `sounddevice`
  reach your graphical session. If your compositor uses a different
  `WAYLAND_DISPLAY`, edit the unit.
- **Secrets:** `GEMINI_API_KEY` is read from the repo `.env` by the app — it is
  never written into the systemd unit.
- This is a *user* service (no `sudo`). It runs only while you are logged in,
  which is what an interactive voice+HUD assistant wants.
