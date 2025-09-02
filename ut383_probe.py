#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import argparse
import sys
import time
from datetime import datetime
from typing import Optional, Tuple, List

from bleak import BleakScanner, BleakClient
from bleak.exc import BleakError

# -------- Einstellungen --------
TARGET_NAMES = ("UT383BT", "UT383")
# bevorzugter Decoder: u16 @ offset 2, /100 → passt zu deinen 5–20 lx
PREFERRED_LOCK: Tuple[str, int, str] = ("u16", 2, "/100")
# Trigger-UUID (write-without-response) + Notify-UUID
UUID_FF01 = "0000ff01-0000-1000-8000-00805f9b34fb"  # meist WNR (Trigger)
UUID_FF02 = "0000ff02-0000-1000-8000-00805f9b34fb"  # NOTIFY (Lux steckt in langem Frame)
# Fallback-Suchservice (manche Stacks listen mehrere Services, wir filtern nicht hart)


def hx(b: bytes, n: int = 32) -> str:
    s = " ".join(f"{x:02X}" for x in b[:n])
    return s + (" …" if len(b) > n else "")


def now_ms() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class LuxDetector:
    """
    Sehr simple, robuste Logik:
    - Wenn ein langes Paket (>=6 Bytes) kommt: u16 little @2 /100 → sofort locken
    - 2-Byte-Pakete ignorieren (sind Status/Keepalive, nicht Lux)
    - Sobald gelockt, nur noch genau dort dekodieren
    """
    def __init__(self):
        self.lock: Optional[Tuple[str, int, str]] = None
        self.lock_reason: str = ""

    def feed(self, pkt: bytes) -> Tuple[Optional[float], bool]:
        # lock schon vorhanden → nur dort dekodieren
        if self.lock:
            kind, off, tag = self.lock
            try:
                if kind == "u16":
                    raw = int.from_bytes(pkt[off:off + 2], "little", signed=False)
                else:
                    raw = int.from_bytes(pkt[off:off + 4], "little", signed=False)
            except Exception:
                return (None, True)
            scale = 1.0 if tag == "/1" else (10.0 if tag == "/10" else 100.0)
            return (raw / scale, True)

        # noch kein Lock: lange Frames priorisieren
        if len(pkt) >= 6:
            raw = int.from_bytes(pkt[2:4], "little", signed=False)
            v = raw / 100.0
            if 0.0 <= v <= 200_000.0:
                self.lock = PREFERRED_LOCK
                self.lock_reason = "erstes langes Paket (u16@2 /100) schlüssig"
                return (v, True)

        # 2-Byte-Pakete ignorieren (helfen nicht beim Locken)
        return (None, False)


async def scan_for_device(scan_time: float = 6.0):
    print("🔎 Scanne nach UT383BT … (BT-Symbol am Gerät muss blinken)")
    devs = await BleakScanner.discover(timeout=scan_time)
    for d in devs:
        if d.name and any(n in d.name for n in TARGET_NAMES):
            print(f"✅ Gefunden: {d.name} [{d.address}]")
            return d
    print("❌ Kein UT383BT im Scan gefunden.")
    return None


async def find_chars(client: BleakClient):
    """
    Sucht die zu nutzenden Characteristics.
    """
    # bleak-Versionen unterscheiden sich: get_services evtl. awaitbar
    svcs = None
    if hasattr(client, "get_services"):
        try:
            svcs = await client.get_services()
        except TypeError:
            svcs = client.get_services()
    if svcs is None:
        svcs = getattr(client, "services", None)

    if not svcs:
        raise RuntimeError("Konnte Services/Characteristics nicht laden (bleak zu alt?).")

    wnr_candidates: List = []
    notify_char = None

    for s in svcs:
        for c in getattr(s, "characteristics", []):
            props = set(getattr(c, "properties", []))
            cu = c.uuid.lower()
            if cu == UUID_FF02 and "notify" in props:
                notify_char = c
            if cu == UUID_FF01 and ("write" in props or "write-without-response" in props):
                wnr_candidates.append(c)

    # WNR: nimm die erste passende FF01
    wnr_char = wnr_candidates[0] if wnr_candidates else None

    if not notify_char:
        raise RuntimeError("FF02 (notify) nicht gefunden.")
    if not wnr_char:
        # Manche Geräte erwarten ggf. andere Trigger; wir versuchen trotzdem ohne.
        print("⚠️ FF01 (Trigger) nicht gefunden – ich versuche es trotzdem nur mit Subscribe.")
    return wnr_char, notify_char


async def run(addr: Optional[str], scan_time: float = 6.0):
    # Adresse bestimmen
    if not addr:
        dev = await scan_for_device(scan_time)
        if not dev:
            return
        addr = dev.address
    else:
        print(f"➡️  Nutze Adresse: {addr}")

    detector = LuxDetector()
    last_notify = 0.0
    got_any_notify = False

    async with BleakClient(addr, address_type="random", timeout=20.0) as client:
        if not client.is_connected:
            print("❌ Verbindung fehlgeschlagen.")
            return
        print("🔗 Verbunden.")

        # Chars suchen
        try:
            wnr_char, notify_char = await find_chars(client)
        except Exception as e:
            print(f"❌ Charakteristiken-Fehler: {e}")
            return

        # Callback
        def _on_notify(_uuid, data: bytearray):
            nonlocal last_notify, got_any_notify
            b = bytes(data)
            got_any_notify = True
            last_notify = time.time()

            # kurze Frames ignorieren (zeigen wir einmal an, solange nicht gelockt)
            if len(b) <= 2 and not detector.lock:
                print(f"\n[{now_ms()}] notify len={len(b)}  {hx(b)} … (kurz, kein Lux)")
                print("   …suche Kandidaten (hell/dunkel wechseln!)")
                return
            if len(b) <= 2 and detector.lock:
                # im Lock-Betrieb überspringen
                return

            print(f"\n[{now_ms()}] notify len={len(b)}  {hx(b)}")
            lux, locked = detector.feed(b)
            if detector.lock and lux is not None:
                kind, off, tag = detector.lock
                print(f"[{now_ms()}] Lux: {lux:.2f} lx   ({kind}{tag}@{off})")
            elif not detector.lock:
                print("   …suche Kandidaten (hell/dunkel wechseln!)")

        # Subscribe
        await client.start_notify(notify_char, _on_notify)
        print("📩 Notifications auf FF02 abonniert.")

        # kleiner Trigger-Helfer
        async def nudge():
            if not wnr_char:
                return
            seq = [b"\x01", b"\x00"]  # simpel an/aus toggeln
            for p in seq:
                try:
                    await client.write_gatt_char(wnr_char, p, response=False)
                    print(f"→ write FF01 <- {hx(p)}")
                    await asyncio.sleep(0.05)
                except Exception as e:
                    print(f"⚠️ write FF01 fehlgeschlagen: {e}")

        # initial anstupsen
        await nudge()

        print("🎧 Live-Betrieb … (Strg+C beendet; Sensor hell/dunkel bewegen)")

        try:
            while True:
                # wenn längere Zeit nichts kommt, erneut triggern
                await asyncio.sleep(1.0)
                if time.time() - last_notify > 3.0:
                    print("⏲️  Keine frischen Notifications → Trigger-Zyklus …")
                    await nudge()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                await client.stop_notify(notify_char)
            except Exception:
                pass
            print("👋 Ende.")

def parse_args():
    ap = argparse.ArgumentParser(description="UNI-T UT383BT – Live-Lux (BLE)")
    ap.add_argument("--addr", help="MAC-Adresse (optional, sonst Autoscan)")
    ap.add_argument("--scan", type=float, default=6.0, help="Scan-Dauer in s (bei Autoscan)")
    return ap.parse_args()


def main():
    args = parse_args()
    try:
        asyncio.run(run(args.addr, args.scan))
    except BleakError as e:
        print("BLE-Fehler:", e)
        print("Tipps: iENV-App schließen, Gerät aus/an (BT blinkt), näher an den Adapter.")
    except Exception as e:
        print("Fehler:", e)


if __name__ == "__main__":
    main()
