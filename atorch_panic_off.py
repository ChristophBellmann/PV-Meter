#!/usr/bin/env python3
# atorch_panic_off.py
import asyncio, signal
from bleak import BleakClient

ADDRESS   = "3A:C5:E2:C6:AD:58"  # deine MAC-Adresse
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

def frame(cmd, adu=2):
    # FF 55 11 <ADU> <CMD> 00 00 00 00 <CHK(xor^44)>
    p = bytearray([0xFF, 0x55, 0x11, adu, cmd, 0, 0, 0, 0])
    chk = 0
    for b in p[2:]:
        chk ^= b
    chk ^= 0x44
    p.append(chk)
    return bytes(p)

TOGGLE, SET, OK = 0x32, 0x49, 0x50

async def main():
    print("🔌 Verbinden …")
    async with BleakClient(ADDRESS) as c:   # verbindet automatisch
        print("✅ Verbunden – sende OFF-Sequenzen …")

        sequences = [
            ("TOGGLE", [frame(TOGGLE)]),
            ("SET→OK", [frame(SET), frame(OK)]),
            ("TOGGLEx2", [frame(TOGGLE), frame(TOGGLE)]),
        ]

        for attempt in range(3):            # 3 Wiederholungen
            for name, pkts in sequences:
                for p in pkts:
                    try:
                        print(f"📤 {name} → {p.hex()}")
                        await c.write_gatt_char(CHAR_UUID, p, response=False)
                        await asyncio.sleep(0.25)
                    except Exception as e:
                        print("⚠️ write fail:", e)
            await asyncio.sleep(0.8)

        print("🆗 fertig – OFF-Sequenzen gesendet.")

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *a: None)
    asyncio.run(main())
