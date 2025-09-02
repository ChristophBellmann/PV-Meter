#!/usr/bin/env python3
# atorch_debugger.py — Live-Decode + sofortige Start/Stop-Steuerung, tastaturecht (kein Enter nötig)

import asyncio
import sys
import termios
import tty
import select
import atexit
from bleak import BleakClient

ADDRESS = "3A:C5:E2:C6:AD:58"            # <- MAC anpassen
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

buffer = bytearray()

# ---------- Terminal global in "cbreak/no-echo" setzen ----------
_fd = sys.stdin.fileno()
_old_attrs = termios.tcgetattr(_fd)

def _setup_keyboard():
    # Basierend auf TTY-Settings: ICANON & ECHO aus, VMIN=0/VTIME=0 => non-blocking, kein Echo
    new_attrs = termios.tcgetattr(_fd)
    lflag = new_attrs[3]
    lflag &= ~termios.ICANON
    lflag &= ~termios.ECHO
    new_attrs[3] = lflag
    new_attrs[6][termios.VMIN] = 0
    new_attrs[6][termios.VTIME] = 0
    termios.tcsetattr(_fd, termios.TCSANOW, new_attrs)

def _restore_keyboard():
    try:
        termios.tcsetattr(_fd, termios.TCSANOW, _old_attrs)
    except Exception:
        pass

atexit.register(_restore_keyboard)

def read_key_nonblocking():
    # Jetzt liefert das TTY Zeichen sofort (ohne Enter). select nur zur Schonung.
    dr, _, _ = select.select([sys.stdin], [], [], 0)
    if not dr:
        return None
    ch1 = sys.stdin.read(1)
    if not ch1:
        return None
    if ch1 == "\x1b":  # ESC-Sequenzen (Pfeile)
        ch2 = sys.stdin.read(1) or ""
        ch3 = sys.stdin.read(1) or ""
        if ch2 == "[":
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(ch3, None)
        return "ESC"
    if ch1 in ("\r", "\n"):
        return "ENTER"
    if ch1 == "+":
        return "PLUS"
    if ch1 == "-":
        return "MINUS"
    if ch1 in ("s", "S"):
        return "STARTSTOP"
    if ch1 in ("q", "Q"):
        return "QUIT"
    return None

# ---------- Parser (36-Byte-Statusframe, Typ 0x01) ----------
def parse_packet(data: bytes):
    if len(data) < 36 or data[0:2] != b'\xff\x55':
        return None
    def get24(o): return int.from_bytes(data[o:o+3], 'big')
    def get32(o): return int.from_bytes(data[o:o+4], 'big')
    def get16(o): return int.from_bytes(data[o:o+2], 'big')
    try:
        return {
            "Spannung_V":   get24(4) / 10,
            "Strom_A":      get24(7) / 1000,
            "Kapazität_Ah": get24(10) / 100,
            "Energie_Wh":   get32(13) * 1.0,   # konservativ: Rohwert als Wh
            "Temperatur_C": get16(24),
            "Laufzeit": {"h": get16(26), "min": data[28], "sec": data[29]},
        }
    except Exception as e:
        print("⚠️ Parsing-Fehler:", e)
        return None

def print_decoded(p):
    if not p:
        print("⚠️ Ungültiges Paket"); return
    print(
        f"🔋 {p.get('Spannung_V', 0.0):.2f} V   "
        f"⚡ {p.get('Strom_A', 0.0):.3f} A   "
        f"🔋 {p.get('Kapazität_Ah', 0.0):.2f} Ah   "
        f"🔄 {p.get('Energie_Wh', 0.0):.2f} Wh   "
        f"🌡 {p.get('Temperatur_C', 0)} °C   "
        f"⏱ {p['Laufzeit']['h']}h {p['Laufzeit']['min']}m {p['Laufzeit']['sec']}s"
    )

# ---------- Atorch-Befehl: FF 55 11 ADU CMD 00 00 00 00 CHK  (CHK = XOR ab byte[2], init 0x44) ----------
def build_atorch_cmd(cmd: int, adu: int = 0x02) -> bytes:
    pkt = bytearray([0xFF, 0x55, 0x11, adu, cmd, 0x00, 0x00, 0x00, 0x00])
    chk = 0x44
    for b in pkt[2:]:
        chk ^= b
    pkt.append(chk)
    return bytes(pkt)

async def send_onoff_toggle(client: BleakClient):
    pkt = build_atorch_cmd(0x32, adu=0x02)  # bestätigter ON/OFF-Toggle
    print(f"📤 ON/OFF (0x32) → {pkt.hex()}")
    try:
        await client.write_gatt_char(CHAR_UUID, pkt, response=True)
    except Exception as e:
        print("   ↳ write error:", e)

async def send_plus(client: BleakClient):
    pkt = build_atorch_cmd(0x51, adu=0x02)  # optional: + (falls FW unterstützt)
    print(f"📤 PLUS (0x51) → {pkt.hex()}")
    try:
        await client.write_gatt_char(CHAR_UUID, pkt, response=True)
    except Exception as e:
        print("   ↳ write error:", e)

async def send_minus(client: BleakClient):
    pkt = build_atorch_cmd(0x52, adu=0x02)  # optional: - (falls FW unterstützt)
    print(f"📤 MINUS (0x52) → {pkt.hex()}")
    try:
        await client.write_gatt_char(CHAR_UUID, pkt, response=True)
    except Exception as e:
        print("   ↳ write error:", e)

async def main():
    global buffer
    print(f"🔌 Verbinde mit {ADDRESS}...")
    async with BleakClient(ADDRESS) as client:
        print("✅ Verbunden – Live-Messung gestartet.")
        print("Keys: [UP/+]=I+, [DOWN/-]=I-, [s]=Start/Stop (sofort), [q]=Quit")
        print("ℹ️  ENTER wird ignoriert (macht nichts).")
        _setup_keyboard()   # <<< WICHTIG: Keyboard sofort in cbreak/no-echo

        def handle_notify(_, data: bytearray):
            # 8-Byte-Reply (Typ 0x02) vom Button?
            if len(data) == 8 and data[0:2] == b'\xff\x55' and data[2] == 0x02:
                print(f"↩️ Reply(8): {data.hex()}")
                return

            # 36-Byte-Statusframe (Typ 0x01)
            global buffer
            buffer += data
            while len(buffer) >= 36:
                if buffer[0:2] != b'\xff\x55':
                    buffer = buffer[1:]; continue
                frame = buffer[:36]; buffer = buffer[36:]
                if frame[2] != 0x01:
                    print(f"\n[RAW?] {frame.hex()}"); continue
                print(f"\n[RAW] {frame.hex()}")
                decoded = parse_packet(frame)
                print_decoded(decoded)

        await client.start_notify(CHAR_UUID, handle_notify)

        try:
            while True:
                await asyncio.sleep(0.05)
                key = read_key_nonblocking()
                if not key:
                    continue
                # Debug: zeig erkannte Taste
                # print(f"(Key={key})")
                if key in ("PLUS", "UP"):
                    await send_plus(client)
                elif key in ("MINUS", "DOWN"):
                    await send_minus(client)
                elif key == "STARTSTOP":
                    await send_onoff_toggle(client)
                elif key == "ENTER":
                    print("(ENTER ignoriert)")
                elif key == "QUIT":
                    print("🚪 Beende Programm..."); break
        finally:
            await client.stop_notify(CHAR_UUID)
            _restore_keyboard()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _restore_keyboard()
        print("\n⛔️ Manuell beendet.")
