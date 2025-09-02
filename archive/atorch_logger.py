#!/usr/bin/env python3

import asyncio
import csv
import datetime
from bleak import BleakClient
from pathlib import Path
from functools import partial

ADDRESS = "3A:C5:E2:C6:AD:58"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
csv_path = Path("atorch_log.csv")

def parse_frame(data: bytes):
    if len(data) < 36 or data[0:2] != b'\xff\x55':
        return None
    voltage_raw = int.from_bytes(data[4:7], "big")
    current_raw = int.from_bytes(data[7:10], "big")
    voltage = voltage_raw / 10       # V
    current = current_raw / 1000     # A
    return voltage, current

def make_notification_handler(start, state):
    buffer = bytearray()

    def handler(_, data: bytearray):
        nonlocal buffer
        buffer.extend(data)

        while b"\xff\x55" in buffer:
            start_index = buffer.find(b"\xff\x55")
            if len(buffer[start_index:]) < 36:
                break
            frame = buffer[start_index:start_index + 36]
            buffer[:] = buffer[start_index + 36:]

            result = parse_frame(frame)
            if result is None:
                continue

            voltage, current = result
            now = datetime.datetime.now()
            elapsed = (now - start).total_seconds()

            if state["timestamp"] is not None:
                dt_h = (now - state["timestamp"]).total_seconds() / 3600
                state["Ah"] += current * dt_h
                state["Wh"] += voltage * current * dt_h

            state["timestamp"] = now

            if int(elapsed) != state["last_logged_second"]:
                with open(csv_path, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                        f"{voltage:.2f}",
                        f"{current:.3f}",
                        f"{state['Ah']:.4f}",
                        f"{state['Wh']:.4f}",
                    ])
                print(f"📊 {now.strftime('%H:%M:%S')} | {voltage:.2f} V | {current:.3f} A | {state['Ah']:.4f} Ah | {state['Wh']:.4f} Wh")
                state["last_logged_second"] = int(elapsed)

    return handler

async def main():
    state = {
        "timestamp": None,
        "Ah": 0.0,
        "Wh": 0.0,
        "last_logged_second": -1
    }

    async with BleakClient(ADDRESS) as client:
        print("✅ Verbunden – starte Logging...")

        if not csv_path.exists():
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Zeit", "Spannung (V)", "Strom (A)", "Ah", "Wh"])

        start = datetime.datetime.now()
        handler = make_notification_handler(start, state)
        await client.start_notify(CHAR_UUID, handler)

        print("⏳ Warte auf Daten (Strg+C zum Beenden)...")
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n⛔️ Logging beendet.")

if __name__ == "__main__":
    asyncio.run(main())

