"""Bluetooth LE link to the CH720N display (XIAO nRF52840 running epd_ble/epd_ble.ino).

Used by ch720n_media_preview.py, or run on its own to send one picture:

    python epd_ble.py                 # send fallback.png
    python epd_ble.py cover.jpg       # convert and send any image
    python epd_ble.py --scan          # list nearby BLE devices

Protocol (must match epd_ble.ino):
    control  0x01                      begin transfer
    image    [offset u16 LE][bytes]    sequential chunks, 5000 bytes total
    control  0x02 [crc32 u32 LE]       commit -> status ACCEPTED, then DONE after the refresh
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
import zlib
from pathlib import Path
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

DEVICE_NAME = "CH720N-EPD"
SERVICE_UUID = "c7201000-8f3e-4b8a-9c4d-1a2b3c4d5e6f"
IMAGE_UUID = "c7201001-8f3e-4b8a-9c4d-1a2b3c4d5e6f"
CONTROL_UUID = "c7201002-8f3e-4b8a-9c4d-1a2b3c4d5e6f"
STATUS_UUID = "c7201003-8f3e-4b8a-9c4d-1a2b3c4d5e6f"

FRAME_BYTES = 5000
MAX_CHUNK = 244 - 2            # firmware accepts 244-byte writes; 2 bytes are the offset
SCAN_TIMEOUT = 10.0
ACCEPT_TIMEOUT = 5.0
REFRESH_TIMEOUT = 20.0

ST_ACCEPTED, ST_DONE, ST_RECEIVING = 0x01, 0x02, 0x03
ST_ERRORS = {
    0xE1: "chunk out of order",
    0xE2: "image incomplete",
    0xE3: "CRC mismatch",
    0xE4: "display busy",
    0xE5: "panel did not respond (BUSY timeout)",
}


class TransferError(Exception):
    pass


def _matches(device, adv) -> bool:
    return (device.name == DEVICE_NAME or adv.local_name == DEVICE_NAME
            or SERVICE_UUID in (adv.service_uuids or []))


async def find_display(timeout: float = SCAN_TIMEOUT):
    return await BleakScanner.find_device_by_filter(_matches, timeout=timeout)


class Session:
    """One connection: sends frames and waits for the display's status notifications."""

    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.status: asyncio.Queue[int] = asyncio.Queue()

    async def start(self) -> None:
        await self.client.start_notify(STATUS_UUID, lambda _c, data: self.status.put_nowait(data[0]))
        mtu = self.client.mtu_size
        if mtu <= 23:                       # Windows sometimes reports the MTU exchange late
            await asyncio.sleep(1.0)
            mtu = self.client.mtu_size
        self.chunk = max(18, min(mtu - 3 - 2, MAX_CHUNK))

    async def _expect(self, wanted: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransferError(f"no status 0x{wanted:02X} from display within {timeout:.0f} s")
            code = await asyncio.wait_for(self.status.get(), remaining)
            if code == wanted:
                return
            if code in ST_ERRORS:
                raise TransferError(ST_ERRORS[code])

    async def send(self, frame: bytes) -> float:
        """Send one 5000-byte frame and wait until the panel has refreshed. Returns seconds taken."""
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"frame must be {FRAME_BYTES} bytes, got {len(frame)}")
        t0 = time.monotonic()
        write = self.client.write_gatt_char
        await write(CONTROL_UUID, b"\x01", response=True)
        for off in range(0, FRAME_BYTES, self.chunk):
            await write(IMAGE_UUID, off.to_bytes(2, "little") + frame[off:off + self.chunk], response=True)
        while not self.status.empty():      # drop RECEIVING etc. from before the commit
            code = self.status.get_nowait()
            if code in ST_ERRORS:
                raise TransferError(ST_ERRORS[code])
        await write(CONTROL_UUID, b"\x02" + zlib.crc32(frame).to_bytes(4, "little"), response=True)
        await self._expect(ST_ACCEPTED, ACCEPT_TIMEOUT)
        await self._expect(ST_DONE, REFRESH_TIMEOUT)
        return time.monotonic() - t0


class EpdLink:
    """Background thread that keeps a connection to the display and pushes the latest frame.

    submit() can be called from any thread; only the newest frame is kept, so fast track
    changes skip intermediate covers instead of queueing refreshes.
    on_status(text) is called from the BLE thread.
    """

    def __init__(self, on_status: Callable[[str], None] = print) -> None:
        self.on_status = on_status
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._thread_main, name="EpdBle", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._poke()

    def submit(self, frame: bytes) -> None:
        with self._lock:
            self._latest = bytes(frame)
        self._poke()

    def _poke(self) -> None:
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            self.on_status(f"Bluetooth stopped: {type(exc).__name__}: {exc}")

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        while not self._stop.is_set():
            self.on_status(f"Bluetooth: searching for {DEVICE_NAME}…")
            try:
                device = await find_display()
            except (BleakError, OSError) as exc:
                self.on_status(f"Bluetooth unavailable: {exc}")
                await self._sleep(5)
                continue
            if device is None:
                continue
            try:
                await self._connected(device)
            except (BleakError, OSError, asyncio.TimeoutError, TransferError) as exc:
                self.on_status(f"Bluetooth: {type(exc).__name__}: {exc}")
                await self._sleep(2)

    async def _sleep(self, seconds: float) -> None:
        """Wait, but wake early on stop() or a new frame."""
        self._wake.clear()
        if self._stop.is_set():
            return
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _connected(self, device) -> None:
        gone = asyncio.Event()
        loop = asyncio.get_running_loop()
        async with BleakClient(device, lambda _c: loop.call_soon_threadsafe(gone.set)) as client:
            session = Session(client)
            await session.start()
            self.on_status(f"Bluetooth: connected to {device.name or device.address} "
                           f"(MTU {client.mtu_size}, {session.chunk}-byte chunks)")
            sent: bytes | None = None       # resend after every reconnect: the display may have rebooted
            failures = 0
            while not self._stop.is_set() and not gone.is_set():
                with self._lock:
                    frame = self._latest
                if frame is None or frame == sent:
                    self._wake.clear()
                    waiter = asyncio.ensure_future(self._wake.wait())
                    lost = asyncio.ensure_future(gone.wait())
                    await asyncio.wait({waiter, lost}, return_when=asyncio.FIRST_COMPLETED)
                    waiter.cancel(); lost.cancel()
                    continue
                self.on_status("Bluetooth: sending image…")
                try:
                    seconds = await session.send(frame)
                except TransferError as exc:
                    failures += 1
                    self.on_status(f"Bluetooth: display reported {exc} (attempt {failures})")
                    if failures >= 3:
                        raise
                    await asyncio.sleep(2)
                    continue
                failures = 0
                sent = frame
                self.on_status(f"Bluetooth: image shown ({seconds:.1f} s)")
            if gone.is_set():
                self.on_status("Bluetooth: display disconnected")


# ---------------------------------- CLI ----------------------------------- #

async def _send_once(path: Path) -> None:
    from PIL import Image
    from epaper_image import to_epd_bytes

    with Image.open(path) as opened:
        frame = to_epd_bytes(opened)
    print(f"Searching for {DEVICE_NAME}…")
    device = await find_display()
    if device is None:
        raise SystemExit(f"{DEVICE_NAME} not found. Is the board powered and advertising?")
    async with BleakClient(device) as client:
        session = Session(client)
        await session.start()
        print(f"Connected to {device.address}, MTU {client.mtu_size}, {session.chunk}-byte chunks")
        seconds = await session.send(frame)
    print(f"Shown {path.name} in {seconds:.1f} s")


async def _scan() -> None:
    found = await BleakScanner.discover(timeout=SCAN_TIMEOUT, return_adv=True)
    for device, adv in sorted(found.values(), key=lambda x: -x[1].rssi):
        mark = "  <- display" if _matches(device, adv) else ""
        print(f"{adv.rssi:4d} dBm  {device.address}  {device.name or adv.local_name or '?'}{mark}")


def main() -> None:
    from epaper_image import FALLBACK_IMAGE

    parser = argparse.ArgumentParser(description="Send an image to the CH720N display over BLE.")
    parser.add_argument("image", nargs="?", type=Path, default=FALLBACK_IMAGE)
    parser.add_argument("--scan", action="store_true", help="list nearby BLE devices and exit")
    args = parser.parse_args()
    try:
        asyncio.run(_scan() if args.scan else _send_once(args.image))
    except (BleakError, TransferError, OSError) as exc:
        raise SystemExit(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
