"""CH720N media-display proof of concept for Windows.

Reads the current Windows media session via GSMTC, extracts its album art,
converts it to a 200x200 1-bit e-paper-style preview, and displays it.

Each new preview is also sent over Bluetooth LE to the display (epd_ble.py,
firmware in epd_ble/). Run with --no-ble to only show the preview.

Requires Windows 10 version 1809+ (or Windows 11), CPython 3.11+, and the
packages listed in requirements.txt.
"""

from __future__ import annotations

import argparse
import asyncio
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from epaper_image import SCREEN_SIZE, make_epaper_preview, pack_epd_bytes


# ----------------------------- Configuration ----------------------------- #

POLL_INTERVAL_SECONDS = 1.5
ARTWORK_RETRY_SECONDS = 8.0
MAX_ARTWORK_BYTES = 12 * 1024 * 1024
PREVIEW_SCALE = 2
OUTPUT_PREVIEW = Path(__file__).with_name("latest_eink_preview.png")
# Shown when nothing is playing or the player gives no artwork; converted to 1-bit automatically.
FALLBACK_IMAGE = Path(__file__).with_name("fallback.png")


# ------------------------------- WinRT data ------------------------------- #

async def read_thumbnail(thumbnail_ref: Any) -> bytes | None:
    """Read a WinRT thumbnail stream into ordinary Python bytes."""
    if thumbnail_ref is None:
        return None

    stream = await thumbnail_ref.open_read_async()
    reader = None
    try:
        size = int(stream.size)
        if size <= 0:
            return None
        if size > MAX_ARTWORK_BYTES:
            raise ValueError(
                f"Artwork is unusually large ({size:,} bytes); refusing to load it."
            )

        from winrt.windows.storage.streams import DataReader

        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        data = bytearray(size)
        reader.read_bytes(data)
        return bytes(data)
    finally:
        if reader is not None:
            reader.close()
        stream.close()


async def media_monitor(outbox: queue.Queue, stop_event: threading.Event) -> None:
    """Poll the current Windows media session and send changed data to Tk."""
    try:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        )
    except ImportError as exc:
        raise RuntimeError(
            "The Windows media API package is missing. Run the install command "
            "from the instructions (pip install -r requirements.txt)."
        ) from exc

    manager = await MediaManager.request_async()

    previous_ui_state: tuple[Any, ...] | None = None
    previous_media_key: tuple[str, str, str, str] | None = None
    cached_artwork: bytes | None = None
    last_artwork_attempt = 0.0
    last_error: str | None = None

    while not stop_event.is_set():
        try:
            session = manager.get_current_session()

            if session is None:
                payload = {
                    "kind": "update",
                    "has_session": False,
                    "title": "Nothing playing",
                    "artist": "",
                    "album": "",
                    "source": "No active Windows media session",
                    "playback": "—",
                    "artwork": None,
                    "message": "Waiting for a player to publish media information.",
                }
                ui_state = (False,)
                previous_media_key = None
                cached_artwork = None

            else:
                props = await session.try_get_media_properties_async()
                if props is None:
                    raise RuntimeError("Windows returned a session without media properties.")

                source = str(session.source_app_user_model_id or "Unknown application")
                title = str(props.title or "Unknown title")
                artist = str(props.artist or props.album_artist or "Unknown artist")
                album = str(props.album_title or "")

                playback_info = session.get_playback_info()
                playback_status = getattr(
                    getattr(playback_info, "playback_status", None),
                    "name",
                    "Unknown",
                )

                media_key = (source, title, artist, album)
                now = time.monotonic()
                media_changed = media_key != previous_media_key
                should_retry_art = (
                    cached_artwork is None
                    and now - last_artwork_attempt >= ARTWORK_RETRY_SECONDS
                )

                if media_changed:
                    cached_artwork = None
                    last_artwork_attempt = 0.0

                if media_changed or should_retry_art:
                    last_artwork_attempt = now
                    try:
                        cached_artwork = await read_thumbnail(props.thumbnail)
                    except Exception as art_error:
                        # Keep the track metadata useful even if the artwork fails.
                        print(f"Artwork read failed: {art_error}", file=sys.stderr)
                        cached_artwork = None

                message = (
                    "Artwork received and converted for a 200x200 e-paper display."
                    if cached_artwork
                    else "Track detected, but this session has not supplied artwork yet."
                )

                payload = {
                    "kind": "update",
                    "has_session": True,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "source": source,
                    "playback": str(playback_status),
                    "artwork": cached_artwork,
                    "message": message,
                }
                ui_state = (*media_key, str(playback_status), bool(cached_artwork))
                previous_media_key = media_key

            if ui_state != previous_ui_state:
                outbox.put(payload)
                previous_ui_state = ui_state

            last_error = None

        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            if error_text != last_error:
                outbox.put({"kind": "error", "message": error_text})
                print(error_text, file=sys.stderr)
                last_error = error_text

        # Event.wait() lets the GUI stop the worker promptly when its window closes.
        stop_event.wait(POLL_INTERVAL_SECONDS)


# ---------------------------------- GUI ------------------------------------ #

class PreviewApp:
    def __init__(self, root: tk.Tk, use_ble: bool = True) -> None:
        self.root = root
        self.root.title("CH720N Display — Windows Media Preview")
        self.root.minsize(610, 390)
        self.root.configure(padx=14, pady=14)

        self.outbox: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = threading.Thread(
            target=self._worker_entry,
            name="WindowsMediaMonitor",
            daemon=True,
        )
        self.photo: ImageTk.PhotoImage | None = None

        self.status_var = tk.StringVar(value="Starting Windows media-session monitor…")
        self.title_var = tk.StringVar(value="Nothing playing")
        self.artist_var = tk.StringVar(value="")
        self.album_var = tk.StringVar(value="")
        self.source_var = tk.StringVar(value="")
        self.playback_var = tk.StringVar(value="")
        self.ble_var = tk.StringVar(value="Bluetooth: off (--no-ble)")

        self.link = None
        if use_ble:
            from epd_ble import EpdLink
            self.link = EpdLink(on_status=lambda text: self.outbox.put({"kind": "ble", "message": text}))

        heading = ttk.Label(
            root,
            text="CH720N DISPLAY PROTOTYPE",
            font=("Segoe UI", 15, "bold"),
        )
        heading.pack(anchor="w", pady=(0, 10))

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)

        image_column = ttk.Frame(body)
        image_column.pack(side="left", padx=(0, 18), anchor="n")
        ttk.Label(image_column, text="E-paper preview · 200 × 200 · 1-bit").pack(
            anchor="w", pady=(0, 6)
        )

        self.image_label = ttk.Label(image_column)
        self.image_label.pack()
        self._show_image(make_epaper_preview(None, FALLBACK_IMAGE))

        info = ttk.Frame(body)
        info.pack(side="left", fill="both", expand=True, anchor="n")
        ttk.Label(info, text="NOW PLAYING", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ttk.Label(
            info,
            textvariable=self.title_var,
            font=("Segoe UI", 14, "bold"),
            wraplength=320,
        ).pack(anchor="w", pady=(7, 2))
        ttk.Label(info, textvariable=self.artist_var, wraplength=320).pack(anchor="w")
        ttk.Label(info, textvariable=self.album_var, wraplength=320).pack(anchor="w", pady=(2, 12))

        ttk.Separator(info).pack(fill="x", pady=8)
        self._field(info, "Playback", self.playback_var)
        self._field(info, "Windows media source", self.source_var)

        ttk.Separator(info).pack(fill="x", pady=8)
        ttk.Label(info, textvariable=self.status_var, wraplength=320).pack(anchor="w")
        ttk.Label(info, textvariable=self.ble_var, wraplength=320).pack(anchor="w", pady=(6, 0))

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.link is not None:
            self.link.start()
        self.worker.start()
        self.root.after(200, self._drain_queue)

    @staticmethod
    def _field(parent: ttk.Frame, name: str, variable: tk.StringVar) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=f"{name}:", width=20).pack(side="left", anchor="n")
        ttk.Label(row, textvariable=variable, wraplength=260).pack(
            side="left", fill="x", expand=True, anchor="w"
        )

    def _worker_entry(self) -> None:
        try:
            asyncio.run(media_monitor(self.outbox, self.stop_event))
        except Exception as exc:
            self.outbox.put({"kind": "fatal", "message": f"{type(exc).__name__}: {exc}"})
            print(f"Fatal media monitor error: {exc}", file=sys.stderr)

    def _show_image(self, image: Image.Image) -> None:
        enlarged = image.convert("RGB").resize(
            (SCREEN_SIZE[0] * PREVIEW_SCALE, SCREEN_SIZE[1] * PREVIEW_SCALE),
            Image.Resampling.NEAREST,
        )
        self.photo = ImageTk.PhotoImage(enlarged)
        self.image_label.configure(image=self.photo)

    def _handle_update(self, payload: dict[str, Any]) -> None:
        self.title_var.set(payload.get("title", "Nothing playing"))
        self.artist_var.set(payload.get("artist", ""))
        self.album_var.set(payload.get("album", ""))
        self.source_var.set(payload.get("source", ""))
        self.playback_var.set(payload.get("playback", ""))
        self.status_var.set(payload.get("message", ""))

        artwork = payload.get("artwork")
        try:
            preview = make_epaper_preview(artwork, FALLBACK_IMAGE)
            preview.save(OUTPUT_PREVIEW)
        except Exception as exc:
            self.status_var.set(f"Could not decode artwork: {exc}")
            preview = make_epaper_preview(None, FALLBACK_IMAGE)
        self._show_image(preview)
        if self.link is not None:
            # Same frame as the preview; the link skips it if the display already shows it.
            self.link.submit(pack_epd_bytes(preview))

    def _drain_queue(self) -> None:
        try:
            while True:
                payload = self.outbox.get_nowait()
                kind = payload.get("kind")
                if kind == "update":
                    self._handle_update(payload)
                elif kind == "ble":
                    self.ble_var.set(payload.get("message", ""))
                elif kind in {"error", "fatal"}:
                    self.status_var.set(payload.get("message", "Unknown error"))
        except queue.Empty:
            pass

        if not self.stop_event.is_set():
            self.root.after(200, self._drain_queue)

    def close(self) -> None:
        self.stop_event.set()
        if self.link is not None:
            self.link.stop()
        self.root.destroy()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("This prototype uses Windows GSMTC and must be run on Windows 10/11.")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-ble", action="store_true", help="preview only; don't send to the display")
    args = parser.parse_args()

    root = tk.Tk()
    PreviewApp(root, use_ble=not args.no_ble)
    root.mainloop()


if __name__ == "__main__":
    main()
