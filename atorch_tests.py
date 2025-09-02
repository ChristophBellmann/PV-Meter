#!/usr/bin/env python3
# atorch_debugger.py — Live-Decode + tastaturechte Button-Steuerung (Atorch DL24x)
# - Live-Parsing der 36-Byte-Statusframes (Typ 0x01)
# - Start/Stop via FF55 (0x32)
# - CC-Bedienung über echte Button-Events (SET/OK/PLUS/MINUS) mit ADU-Fächerung
#   => höherer Erfolg, wenn der interne "Menü-/Seitenindex" (ADU) unbekannt ist
#
# Keys:
#   [UP/+]  : CC "+"
#   [DOWN/-]: CC "-"
#   e       : CC-Edit betreten (SET, dann OK)
#   o       : OK
#   m       : SET
#   p       : "+ N Schritte" (Eingabedialog)
#   n       : "- N Schritte" (Eingabedialog)
#   s       : Start/Stop (FF55 Toggle)
#   q       : Quit
#
# Getestet unter Linux (tty ohne Echo/Canon), pip install bleak

import asyncio
import sys
import termios
import select
import atexit
from bleak import BleakClient

# ==== Anpassungen ====
ADDRESS = "3A:C5:E2:C6:AD:58"  # <- MAC/UUID anpassen
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"  # dl24 write/notify char

# ==== Statuspuffer ====
buffer = bytearray()
last_current_a = None  # gemessener Strom in A (aus Statusframe)

# ---------- Terminal in "cbreak/no-echo" ----------
_fd = sys.stdin.fileno()
_old_attrs = termios.tcgetattr(_fd)

def _setup_keyboard():
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

# ---------- Key-Handling ----------
def read_key_nonblocking():
    dr, _, _ = select.select([sys.stdin], [], [], 0)
    if not dr:
        return None
    ch1 = sys.stdin.read(1)
    if not ch1:
        return None
    if ch1 == "\x1b":  # ESC/CSI
        ch2 = sys.stdin.read(1) or ""
        ch3 = sys.stdin.read(1) or ""
        if ch2 == "[":
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(ch3, None)
        return "ESC"
    if ch1 in ("\r", "\n"):
        return "ENTER"
    if ch1 == "\x7f":
        return "BACKSPACE"
    if ch1 == "+":
        return "PLUS"
    if ch1 == "-":
        return "MINUS"
    if ch1 in ("s", "S"):
        return "STARTSTOP"
    if ch1 in ("e", "E"):
        return "CC_ENTER"
    if ch1 in ("o", "O"):
        return "OK"
    if ch1 in ("m", "M"):
        return "SET"
    if ch1 in ("p", "P"):
        return "PLUS_STEPS"
    if ch1 in ("n", "N"):
        return "MINUS_STEPS"
    if ch1 in ("q", "Q"):
        return "QUIT"
    # Für Eingabedialoge (p/n)
    if ch1.isdigit() or ch1 in ("-","+"):
        return ("CHAR", ch1)
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
            "Energie_Wh":   get32(13) * 1.0,
            "Temperatur_C": get16(24),
            "Laufzeit": {"h": get16(26), "min": data[28], "sec": data[29]},
        }
    except Exception as e:
        print("Parsing-Fehler:", e)
        return None

def print_decoded(p):
    global last_current_a
    if not p:
        print("⚠️ Ungültiges Paket"); return
    last_current_a = p.get('Strom_A', None)
    print(
        f"{p.get('Spannung_V', 0.0):.2f} V   "
        f"{p.get('Strom_A', 0.0):.3f} A   "
        f"{p.get('Kapazität_Ah', 0.0):.2f} Ah   "
        f"{p.get('Energie_Wh', 0.0):.2f} Wh   "
        f"{p.get('Temperatur_C', 0)} °C   "
        f"{p['Laufzeit']['h']}h {p['Laufzeit']['min']}m {p['Laufzeit']['sec']}s"
    )

# ---------- FF55 Start/Stop ----------
def build_atorch_cmd(cmd: int, adu: int = 0x02) -> bytes:
    # FF 55 11 ADU CMD 00 00 00 00 CHK  (CHK = XOR ab byte[2], init 0x44)
    pkt = bytearray([0xFF, 0x55, 0x11, adu, cmd, 0x00, 0x00, 0x00, 0x00])
    chk = 0x44
    for b in pkt[2:]:
        chk ^= b
    pkt.append(chk)
    return bytes(pkt)

async def send_onoff_toggle(client: BleakClient):
    pkt = build_atorch_cmd(0x32, adu=0x02)  # bestätigter ON/OFF-Toggle
    print(f"Sende Start/Stop (0x32): {pkt.hex()}")
    try:
        await client.write_gatt_char(CHAR_UUID, pkt, response=True)
    except Exception as e:
        print("   ↳ write error:", e)

# ---------- Button-Control (dein Block, integriert) ----------
# Button-Codes laut Reverse-Engineering
BTN_SET   = 49  # "SET/Mode"
BTN_OK    = 50  # "OK/Enter/Next digit"
BTN_PLUS  = 51  # "+"
BTN_MINUS = 52  # "-"

def _make_button_packet(button_code: int, adu: int = 0) -> bytes:
    """
    10-Byte Paket:
    [0]=0xFF, [1]=0x55, [2]=0x11, [3]=adu, [4]=button, [5..8]=0, [9]=checksum
    checksum = (sum(bytes[2..8]) XOR 68) & 0xFF
    """
    pkt = bytearray(10)
    pkt[0] = 0xFF
    pkt[1] = 0x55
    pkt[2] = 0x11
    pkt[3] = adu & 0xFF
    pkt[4] = button_code & 0xFF
    # [5..8] bleiben 0
    s = 0
    for i in range(2, 9):
        s += pkt[i]
    pkt[9] = (s ^ 68) & 0xFF
    return bytes(pkt)

async def send_button(client: BleakClient, button_code: int, tries_adu=(0,1,2,3), delay=0.08):
    """
    Sendet einen Button-Event. Falls der genaue ADU (Menü-/Seitenindex) unbekannt ist,
    probieren wir mehrere ADU-Werte – erhöht die Chance, dass das Gerät „trifft“.
    """
    for adu in tries_adu:
        pkt = _make_button_packet(button_code, adu=adu)
        # response=False – wie ein Tastendruck (keine Bestätigung nötig)
        await client.write_gatt_char(CHAR_UUID, pkt, response=False)
        await asyncio.sleep(delay)

async def cc_enter_edit(client: BleakClient):
    """SET, dann OK – öffnet i. d. R. die Bearbeitung des CC-Stromfelds."""
    await send_button(client, BTN_SET)
    await asyncio.sleep(0.2)
    await send_button(client, BTN_OK)

async def cc_nudge(client: BleakClient, steps: int):
    """Ändert den Strom um 'steps' Inkremente (meist 0.01 A pro Schritt)."""
    if steps == 0:
        return
    btn = BTN_PLUS if steps > 0 else BTN_MINUS
    for _ in range(abs(steps)):
        await send_button(client, btn)
        await asyncio.sleep(0.04)

async def cc_ok(client: BleakClient):
    await send_button(client, BTN_OK)

async def cc_set(client: BleakClient):
    await send_button(client, BTN_SET)

# ---------- kleine Eingabedialoge ----------
async def prompt_int(label: str):
    """Einfacher int-Eingabedialog ohne Blockieren des Eventloops."""
    buf = []
    print(f"{label} (Ganzzahl) › ", end="", flush=True)
    while True:
        await asyncio.sleep(0.02)
        k = read_key_nonblocking()
        if not k:
            continue
        if k == "ENTER":
            print("")
            s = "".join(buf).strip()
            try:
                return int(s)
            except Exception:
                print("⚠️ Ungültig."); return None
        if k == "BACKSPACE":
            if buf:
                buf.pop(); sys.stdout.write("\b \b"); sys.stdout.flush()
            continue
        if isinstance(k, tuple) and k[0] == "CHAR":
            ch = k[1]
            if ch in "+-0123456789":
                buf.append(ch); sys.stdout.write(ch); sys.stdout.flush()
        elif k == "ESC":
            print("\n(abgebrochen)"); return None
        # andere Tasten ignorieren

# ---------- Main ----------
async def main():
    global buffer
    print(f"🔌 Verbinde mit {ADDRESS}...")
    async with BleakClient(ADDRESS, timeout=10.0) as client:
        print("✅ Verbunden – Live-Messung gestartet.")
        print("Keys: [UP/+]=CC+, [DOWN/-]=CC-, [e]=Edit (SET→OK), [o]=OK, [m]=SET, [p]=+N, [n]=-N, [s]=Start/Stop, [q]=Quit")
        _setup_keyboard()

        def handle_notify(_, data: bytearray):
            # 8-Byte-Reply (Typ 0x02) vom Button?
            if len(data) == 8 and data[0:2] == b'\xff\x55' and data[2] == 0x02:
                print(f"↩️ Reply(8): {data.hex()}")
                return

            # 36-Byte-Statusframe (Typ 0x01)
            if len(data) >= 2 and data[0:2] == b'\xff\x55':
                global buffer
                buffer += data
                while len(buffer) >= 36:
                    if buffer[0:2] != b'\xff\x55':
                        buffer = buffer[1:]; continue
                    frame = buffer[:36]; buffer = buffer[36:]
                    if frame[2] != 0x01:
                        print(f"\n[RAW?] {frame.hex()}"); continue
                    # Debug-RAW:
                    print(f"\n[RAW] {frame.hex()}")
                    decoded = parse_packet(frame)
                    print_decoded(decoded)
            else:
                # Unbekanntes Kurznachrichtenformat
                print(f"↩️ Misc: {data.hex()}")

        await client.start_notify(CHAR_UUID, handle_notify)

        try:
            while True:
                await asyncio.sleep(0.05)
                key = read_key_nonblocking()
                if not key:
                    continue

                if key in ("PLUS", "UP"):
                    await send_button(client, BTN_PLUS)

                elif key in ("MINUS", "DOWN"):
                    await send_button(client, BTN_MINUS)

                elif key == "CC_ENTER":
                    print("↪️ CC-Edit betreten (SET → OK)")
                    await cc_enter_edit(client)

                elif key == "OK":
                    print("↪️ OK")
                    await cc_ok(client)

                elif key == "SET":
                    print("↪️ SET")
                    await cc_set(client)

                elif key == "PLUS_STEPS":
                    steps = await prompt_int("Anzahl Schritte +")
                    if steps is not None and steps > 0:
                        await cc_nudge(client, steps)

                elif key == "MINUS_STEPS":
                    steps = await prompt_int("Anzahl Schritte -")
                    if steps is not None and steps > 0:
                        await cc_nudge(client, -steps)

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
