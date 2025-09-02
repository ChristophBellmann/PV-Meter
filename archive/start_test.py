#!/usr/bin/env python3
import asyncio
from bleak import BleakClient

ADDRESS = "3A:C5:E2:C6:AD:58"  # <- dein DL24M
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# -------- Paket-Bauer für verschiedene Varianten --------
def pkt_v1():
    # Atorch-Variante, Typ=0x11, ADU=0x02, Cmd=0x32, XOR-Checksumme
    pkt = bytearray([0xFF,0x55,0x11,0x02,0x32,0x00,0x00,0x00,0x00])
    chk = 0x44
    for b in pkt[2:]:
        chk ^= b
    pkt.append(chk)
    return pkt

def pkt_v2():
    # Gleiches, aber Checksum init=0x00
    pkt = bytearray([0xFF,0x55,0x11,0x02,0x32,0x00,0x00,0x00,0x00])
    chk = 0x00
    for b in pkt[2:]:
        chk ^= b
    pkt.append(chk)
    return pkt

def pkt_v3():
    # ADU=0x01, Checksum init=0x44
    pkt = bytearray([0xFF,0x55,0x11,0x01,0x32,0x00,0x00,0x00,0x00])
    chk = 0x44
    for b in pkt[2:]:
        chk ^= b
    pkt.append(chk)
    return pkt

def pkt_v4():
    # PX100-artiger Frame, Beispiel: ON
    return bytearray([0xB1,0xB2,0x01,0x01,0x00,0xB6])

VARIANTS = [
    ("Atorch ADU=0x02 chk^0x44", pkt_v1),
    ("Atorch ADU=0x02 chk^0x00", pkt_v2),
    ("Atorch ADU=0x01 chk^0x44", pkt_v3),
    ("PX100 style", pkt_v4),
]

# ---------------------------------------------------------
async def main():
    async with BleakClient(ADDRESS) as client:
        print("✅ Verbunden mit DL24M")
        for label, builder in VARIANTS:
            pkt = builder()
            print(f"\n▶️ Teste Variante: {label}")
            print(f"📤 Sende: {pkt.hex()}")
            try:
                await client.write_gatt_char(CHAR_UUID, pkt, response=True)
            except Exception as e:
                print("   ⚠️ Fehler:", e)
            print("👉 Falls das Gerät JETZT startet: Drücke ENTER. Sonst beliebige Taste + ENTER zum Weitermachen.")
            input()
        print("🚪 Sequenz fertig.")

if __name__ == "__main__":
    asyncio.run(main())
