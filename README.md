# CH720N media-display prototype (Windows)

This first proof of concept checks whether Windows exposes the currently playing track and its artwork. It then renders a 200 × 200, 1-bit e-paper-style preview in a small window.

**Bluetooth is not implemented yet.** This verifies the Windows media-session and image-conversion part before we add BLE.

## Requirements

- Windows 10 version 1809 or newer, or Windows 11
- 64-bit CPython 3.11 or newer
- A media player/browser that publishes a Windows media session

## Install

Open PowerShell in this folder and run:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

You can replace `3.12` with another installed Python version (3.11+).

## Run

Start playing something in YouTube Music in your browser, then run:

```powershell
python .\ch720n_media_preview.py
```

The window should show the track title, artist, media-session source, playback state, and a dithered 200 × 200 preview. The generated preview is also saved as `latest_eink_preview.png` beside the script.

## If it says no media session / no artwork

1. Confirm the audio is playing on this Windows laptop.
2. Try pausing and resuming once; some browsers only publish the media session after playback begins.
3. Check whether the browser appears in Windows' media controls (for example, the media flyout opened by a keyboard media key).
4. Try another browser/player to distinguish a YouTube Music/browser metadata issue from a Python issue.

Windows can only provide metadata and artwork that the player/browser publishes. This script cannot recover artwork that the media session does not expose.

## What it does not do

- Does not connect to, pair with, or modify the Sony headphones.
- Does not capture or inspect audio.
- Does not change playback or send media-control commands.
- Does not run in the background as a tray app yet.
