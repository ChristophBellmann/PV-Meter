#!/usr/bin/env python3

import asyncio
from bleak import BleakClient
from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings

ADDRESS = "3A:C5:E2:C6:AD:58"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

buffer = bytearray()

def parse_packet(data: bytes):
    if len(data) < 36 or data[0:2] != b'\xff\x55':
        return None

    def get24(offset):
        return int.from_bytes(data[offset:offset+3], byteorder='big')

    def get32(offset):
        return int.from_bytes(data[offset:offset+4], byteorder='big')

    def get16(offset):
        return int.from_bytes(data[offset:offset+2], byteorder='big')

    try:
        return {
            "Spannung_V": get24(4) / 10,
            "Strom_A": get24(7) / 1000,
            "Kapazität_Ah": get24(10) / 100,
            "Energie_Wh": get32(13) * 1.0,
            "Temperatur_C": get16(24),
            "Laufzeit": {
                "h": get16(26),
                "min": data[28],
                "sec": data[29],
            }
        }
    except Exception as e:
        print("⚠️ Parsing-Fehler:", e)
        return None

def print_decoded(packet: dict):
    if not packet:
        print("⚠️ Ungültiges Paket")
        return
    print(
        f"�� {packet.get('Spannung_V', 0.0):.2f} V   "
        f"⚡ {packet.get('Strom_A', 0.0):.3f} A   "
        f"🔋 {packet.get('Kapazität_Ah', 0.0):.2f} Ah   "
        f"🔄 {packet.get('Energie_Wh', 0.0):.2f} Wh   "
        f"🌡 {packet.get('Temperatur_C', 0)} °C   "
        f"⏱ {packet['Laufzeit']['h']}h {packet['Laufzeit']['min']}m {packet['Laufzeit']['sec']}s"
    )

def build_keypress_packet(key_code: int, adu: int = 0x01) -> bytearray:
    packet = bytearray([0xFF, 0x55, 0x11, adu, key_code, 0x00, 0x00, 0x00, 0x00])
    checksum = 0x44
    for b in packet[2:]:
        checksum ^= b
    packet.append(checksum)
    return packet

async def main():
    global buffer
    print(f"🔌 Verbinde mit {ADDRESS}...")
    async with BleakClient(ADDRESS) as client:
        print("✅ Verbunden – starte Dekodierung der Messdaten...")
        print("⬆️  = Strom +     ⬇️  = Strom -     ⏎ = OK     s = Set")
        print("⏳ Warte auf Benachrichtigungen (ESC oder Strg+C zum Beenden)...")

        async def send_key(keycode: int):
            packet = build_keypress_packet(keycode)
            await client.write_gatt_char(CHAR_UUID, packet)

        def handle_notify(_, data: bytearray):
            global buffer
            buffer += data
            while len(buffer) >= 36:
                if buffer[0:2] != b'\xff\x55':
                    buffer = buffer[1:]
                    continue
                packet = buffer[0:36]
                buffer = buffer[36:]
                decoded = parse_packet(packet)
                if decoded:
                    print(f"\n[RAW] {packet.hex()}")
                    print_decoded(decoded)

        await client.start_notify(CHAR_UUID, handle_notify)

        kb = KeyBindings()

        @kb.add('up')
        def _(event): asyncio.create_task(send_key(0x51))  # +
        @kb.add('down')
        def _(event): asyncio.create_task(send_key(0x52))  # -
        @kb.add('enter')
        def _(event): asyncio.create_task(send_key(0x50))  # OK
        @kb.add('s')
        def _(event): asyncio.create_task(send_key(0x49))  # SET
        @kb.add('escape')
        def _(event): event.app.exit()

        app = Application(key_bindings=kb, full_screen=False)
        try:
            await app.run_async()
        finally:
            await client.stop_notify(CHAR_UUID)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔️ Manuell beendet.")
