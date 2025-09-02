#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import argparse
from datetime import datetime, timedelta
from bleak import BleakScanner, BleakClient

UUID_FF02_NOTIFY = "0000ff02-0000-1000-8000-00805f9b34fb"  # notify
UUID_FF01_WRITE  = "0000ff01-0000-1000-8000-00805f9b34fb"  # write w/o response
NAME_HINTS = ("UT383BT", "UT383")

# Trigger-/Watchdog
NUDGE_PERIOD_S = 0.5
STALE_AFTER_S  = 2.0
STARTUP_BURST  = 6

# Plausible Lux-Bereich (real ~5..120 lx bei dir). Breit lassen, aber nicht zu breit.
LUX_MIN = 0.0
LUX_MAX = 1000.0  # breit, weil lange Frames ~ x10 kommen

def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]

def short_hex(b: bytes, maxlen=32):
    h = " ".join(f"{x:02X}" for x in b[:maxlen])
    return h + (" …" if len(b) > maxlen else "")

def decode_short(payload: bytes):
    """2-Byte-Frame → u16 LE / 1000"""
    if len(payload) != 2:
        return None
    raw = int.from_bytes(payload, "little", signed=False)
    return raw / 1000.0

def decode_long(payload: bytes):
    """≥4-Byte-Frame → u16 LE @2 / 100; 0-Frames ignorieren"""
    if len(payload) < 4:
        return None
    raw = int.from_bytes(payload[2:4], "little", signed=False)
    if raw == 0:
        return 0.0
    return raw / 100.0

class UT383Reader:
    def __init__(self, addr: str | None):
        self.addr = addr
        self.client: BleakClient | None = None
        self.last_rx = datetime.min
        self._nudge_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        # De-Dupe: merke letzten Kurz-Frame
        self.last_short_ts: datetime | None = None
        self.last_short_val: float | None = None

    async def connect(self):
        if not self.addr:
            self.addr = await self._discover()
            if not self.addr:
                return False
        print(f"➡️  Nutze Adresse: {self.addr}")
        self.client = BleakClient(self.addr, address_type="random", timeout=20.0)
        await self.client.connect()
        print("🔗 Verbunden.")
        await self.client.start_notify(UUID_FF02_NOTIFY, self._on_notify)
        print("📩 Notifications auf FF02 abonniert.")

        # Startup-Burst
        for _ in range(STARTUP_BURST):
            await self._nudge()
            await asyncio.sleep(0.08)

        self._nudge_task = asyncio.create_task(self._nudger())
        self._watch_task = asyncio.create_task(self._watchdog())
        return True

    async def close(self):
        try:
            if self._nudge_task: self._nudge_task.cancel()
            if self._watch_task: self._watch_task.cancel()
            if self.client and self.client.is_connected:
                await self.client.stop_notify(UUID_FF02_NOTIFY)
                await self.client.disconnect()
        except Exception:
            pass

    async def _discover(self, timeout=8.0):
        print("🔎 Scan … (UT383BT einschalten, BT-Symbol blinken lassen)")
        devs = await BleakScanner.discover(timeout=timeout)
        for d in devs:
            name = (d.name or "").upper()
            if any(h in name for h in NAME_HINTS):
                print(f"✅ Gefunden: {d.name} [{d.address}]")
                return d.address
        print("❌ Kein UT383BT gefunden.")
        return None

    async def _nudge(self):
        if not self.client or not self.client.is_connected:
            return
        try:
            await self.client.write_gatt_char(UUID_FF01_WRITE, b"\x01", response=False)
            await asyncio.sleep(0.02)
            await self.client.write_gatt_char(UUID_FF01_WRITE, b"\x00", response=False)
        except Exception:
            pass

    async def _nudger(self):
        while True:
            await asyncio.sleep(NUDGE_PERIOD_S)
            await self._nudge()

    async def _watchdog(self):
        while True:
            await asyncio.sleep(0.25)
            if datetime.now() - self.last_rx > timedelta(seconds=STALE_AFTER_S):
                await self._nudge()

    def _emit(self, kind: str, n: int, data: bytes, note: str = ""):
        h = short_hex(data)
        if note:
            print(f"[{ts()}] {kind} len={n:>3}  {h}\n   {note}")
        else:
            print(f"[{ts()}] {kind} len={n:>3}  {h}")

    def _on_notify(self, _handle, data: bytes):
        self.last_rx = datetime.now()
        n = len(data)

        # 1) Kurz-Frames bevorzugen
        if n == 2:
            lux = decode_short(data)
            if lux is not None and LUX_MIN <= lux <= LUX_MAX:
                self.last_short_ts = datetime.now()
                self.last_short_val = lux
                self._emit("notify", n, data)
                print(f"[{ts()}] Lux: {lux:.2f} lx   (u16/1000@0)")
            else:
                self._emit("notify", n, data, f"↪︎ (kein plausibler Lux: short {lux})")
            return

        # 2) Lange Frames: duplizieren oft kurz ×10 → ggf. leise überspringen
        if n >= 4:
            lux_long = decode_long(data)
            if lux_long == 0.0:
                # typische Null-Frames: still ignorieren
                return

            # De-Dupe: wenn kürzlich Kurz-Frame kam und ~×10 passt, überspringen
            if self.last_short_ts and (datetime.now() - self.last_short_ts).total_seconds() < 0.8:
                s = self.last_short_val or 0.0
                if 0 < s and abs(lux_long - 10.0*s) <= max(0.5, 0.02*lux_long):
                    # Optional: zum Debuggen ausgeben -> auskommentiert
                    # self._emit("notify", n, data, f"(duplikat: long≈10×short → {lux_long:.2f} vs {s:.2f})")
                    return

            # Kein Duplikat → als Info zeigen, aber kennzeichnen
            self._emit("notify", n, data, f"↪︎ (long u16/100@2 = {lux_long:.2f} lx; vermutlich 10× der Kurzwerte)")

        else:
            # ganz kurze Frames
            self._emit("notify", n, data, "↪︎ (zu kurz)")

async def run(addr: str | None):
    reader = UT383Reader(addr)
    if not await reader.connect():
        return
    print("🎧 Live-Betrieb … (Strg+C beendet; Sensor hell/dunkel bewegen)")
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        await reader.close()
        print("👋 Ende.")

def main():
    ap = argparse.ArgumentParser(description="UNI-T UT383BT Konsolen-Reader (Kurz-Frames bevorzugt, Duplikate unterdrückt)")
    ap.add_argument("--addr", help="BT-Adresse, z. B. E8:26:CF:F1:16:B1")
    args = ap.parse_args()
    asyncio.run(run(args.addr))

if __name__ == "__main__":
    main()
