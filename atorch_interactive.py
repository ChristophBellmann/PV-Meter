#!/usr/bin/env python3
# atorch_interactive.py — Live-Decode + Button-Steuerung (Atorch DL24x)
# - Live-Parsing der 36-Byte-Statusframes (Typ 0x01)
# - Start/Stop via FF55 (0x32)
# - CC-Bedienung über echte Button-Events (SET/OK/PLUS/MINUS)
# - ADU wird beim Start automatisch gelernt (via PLUS), danach nur diese ADU genutzt
# - Safe-Edit: e = SET, SET, OK (alle ADU=0) → keine versehentlichen Starts

import asyncio
import sys
import termios
import select
import atexit
from bleak import BleakClient

# ==== Anpassungen ====
ADDRESS = "3A:C5:E2:C6:AD:58"  # <- MAC/UUID anpassen
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Tunables
DEBOUNCE_MS = 120
NUDGE_STEP_DELAY_MS = 40
WAIT_DELTA_A = 0.0005
WAIT_DELTA_TIMEOUT_MS = 800

# ==== Statuspuffer ====
buffer = bytearray()
last_current_a = None
frame_counter = 0
misc_counter = 0
status_ready = asyncio.Event()
_detected_adu = None

# ---------- Terminal ----------
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
_last_key_ts = 0.0
def _debounced():
    import time
    global _last_key_ts
    now = time.monotonic()
    if (now - _last_key_ts) * 1000.0 < DEBOUNCE_MS:
        return True
    _last_key_ts = now
    return False

def read_key_nonblocking():
    dr, _, _ = select.select([sys.stdin], [], [], 0)
    if not dr:
        return None
    ch1 = sys.stdin.read(1)
    if not ch1:
        return None
    if ch1 == "\x1b":
        ch2 = sys.stdin.read(1) or ""
        ch3 = sys.stdin.read(1) or ""
        if ch2 == "[":
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(ch3, None)
        return "ESC"
    if ch1 in ("\r", "\n"):
        return "ENTER"
    if ch1 == "\x7f":
        return "BACKSPACE"
    if ch1 == "+": return "PLUS"
    if ch1 == "-": return "MINUS"
    if ch1 in ("s","S"): return "STARTSTOP"
    if ch1 in ("e","E"): return "CC_ENTER"
    if ch1 in ("o","O"): return "OK"
    if ch1 in ("m","M"): return "SET"
    if ch1 in ("p","P"): return "PLUS_STEPS"
    if ch1 in ("n","N"): return "MINUS_STEPS"
    if ch1 in ("q","Q"): return "QUIT"
    if ch1.isdigit() or ch1 in ("-","+"):
        return ("CHAR", ch1)
    return None

# ---------- Parser ----------
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
    except:
        return None

def print_decoded(p):
    global last_current_a, frame_counter
    if not p: return
    last_current_a = p.get("Strom_A", None)
    frame_counter += 1
    if not status_ready.is_set() and last_current_a is not None:
        status_ready.set()
    print(
        f"{p['Spannung_V']:.2f} V   {p['Strom_A']:.3f} A   "
        f"{p['Kapazität_Ah']:.2f} Ah   {p['Energie_Wh']:.2f} Wh   "
        f"{p['Temperatur_C']} °C   "
        f"{p['Laufzeit']['h']}h {p['Laufzeit']['min']}m {p['Laufzeit']['sec']}s"
    )

# ---------- FF55 Start/Stop ----------
def build_atorch_cmd(cmd: int, adu: int = 0x02) -> bytes:
    pkt = bytearray([0xFF,0x55,0x11,adu,cmd,0,0,0,0])
    chk=0x44
    for b in pkt[2:]: chk^=b
    pkt.append(chk)
    return bytes(pkt)

async def send_onoff_toggle(client: BleakClient):
    pkt = build_atorch_cmd(0x32, adu=0x02)
    print(f"Sende Start/Stop (0x32): {pkt.hex()}")
    await client.write_gatt_char(CHAR_UUID, pkt, response=True)

# ---------- Buttons ----------
BTN_SET, BTN_OK, BTN_PLUS, BTN_MINUS = 49,50,51,52

def _make_button_packet(button_code:int, adu:int=0)->bytes:
    pkt=bytearray(10)
    pkt[0:5]=[0xFF,0x55,0x11,adu&0xFF,button_code&0xFF]
    s=sum(pkt[2:9])
    pkt[9]=(s^68)&0xFF
    return bytes(pkt)

async def _write_button(client, code, adu, delay_ms=40):
    pkt=_make_button_packet(code,adu)
    await client.write_gatt_char(CHAR_UUID,pkt,response=False)
    await asyncio.sleep(delay_ms/1000.0)

async def send_button(client, code, tries_adu=(0,1,2,3)):
    if _detected_adu is not None:
        await _write_button(client, code, _detected_adu)
    else:
        for adu in tries_adu:
            await _write_button(client, code, adu)

async def send_button_exact(client, code, adu, delay_ms=80):
    pkt=_make_button_packet(code,adu)
    await client.write_gatt_char(CHAR_UUID,pkt,response=False)
    await asyncio.sleep(delay_ms/1000.0)

# ---------- ADU lernen ----------
async def learn_adu(client: BleakClient):
    global _detected_adu
    print("🧪 Lerne ADU (via PLUS)…")
    best_adu=None; best_score=-1
    for adu in (0,1,2,3):
        f0,m0,si=frame_counter,misc_counter,last_current_a
        await send_button_exact(client,BTN_PLUS,adu,delay_ms=100)
        await asyncio.sleep(0.25)
        f1,m1,ei=frame_counter,misc_counter,last_current_a
        di=0 if si is None or ei is None else abs(ei-si)
        score=(f1-f0)+(m1-m0)+(1 if di>=0.0005 else 0)
        print(f"  ADU {adu}: Δf={f1-f0}, Δm={m1-m0}, ΔI={di:.4f} → {score}")
        await send_button_exact(client,BTN_MINUS,adu,delay_ms=100)
        await asyncio.sleep(0.15)
        if score>best_score: best_score, best_adu=score,adu
    if best_score<=0:
        print("⚠️ Keine eindeutige ADU → Fächerung")
        _detected_adu=None
    else:
        _detected_adu=best_adu
        print(f"✅ ADU gelernt: {best_adu}")

# ---------- CC helpers ----------
async def cc_enter_edit(client: BleakClient):
    # sichere Sequenz: SET, SET, OK alle ADU=0
    await send_button_exact(client,BTN_SET,0,delay_ms=120)
    await asyncio.sleep(0.2)
    await send_button_exact(client,BTN_SET,0,delay_ms=120)
    await asyncio.sleep(0.2)
    await send_button_exact(client,BTN_OK,0,delay_ms=120)

async def _wait_status_delta(min_delta=WAIT_DELTA_A,timeout=WAIT_DELTA_TIMEOUT_MS):
    start=asyncio.get_event_loop().time()
    base=last_current_a
    while (asyncio.get_event_loop().time()-start)<timeout/1000:
        await asyncio.sleep(0.02)
        if base is None: continue
        if last_current_a is not None and abs(last_current_a-base)>=min_delta:
            return True
    return False

async def cc_nudge(client,steps:int):
    if steps==0: return
    btn=BTN_PLUS if steps>0 else BTN_MINUS
    for _ in range(abs(steps)):
        await send_button(client,btn)
        _=await _wait_status_delta()
        await asyncio.sleep(NUDGE_STEP_DELAY_MS/1000.0)

async def cc_ok(client): await send_button(client,BTN_OK)
async def cc_set(client): await send_button(client,BTN_SET)

# ---------- Input prompt ----------
async def prompt_int(label):
    buf=[]
    print(f"{label} › ",end="",flush=True)
    while True:
        await asyncio.sleep(0.02)
        k=read_key_nonblocking()
        if not k: continue
        if k=="ENTER":
            print(""); s="".join(buf).strip()
            try: return int(s)
            except: print("⚠️ Ungültig"); return None
        if k=="BACKSPACE":
            if buf: buf.pop(); sys.stdout.write("\b \b"); sys.stdout.flush()
            continue
        if isinstance(k,tuple) and k[0]=="CHAR":
            ch=k[1]
            if ch in "+-0123456789":
                buf.append(ch); sys.stdout.write(ch); sys.stdout.flush()
        elif k=="ESC":
            print("\n(abgebrochen)"); return None

# ---------- Main ----------
async def main():
    global buffer,misc_counter
    print(f"🔌 Verbinde mit {ADDRESS}…")
    async with BleakClient(ADDRESS,timeout=10.0) as client:
        print("✅ Verbunden.")
        print("Keys: [↑/+]=+, [↓/-]=- , [e]=Edit, [o]=OK, [m]=SET, [p]=+N, [n]=-N, [s]=Start/Stop, [q]=Quit")
        _setup_keyboard()

        def handle_notify(_,data:bytearray):
            global buffer,misc_counter
            if not data: return
            if len(data)==8 and data[0:2]==b'\xff\x55' and data[2]==0x02:
                print(f"↩️ Reply(8): {data.hex()}"); return
            if len(data)>=2 and data[0:2]==b'\xff\x55':
                buffer+=data
                while len(buffer)>=36:
                    if buffer[0:2]!=b'\xff\x55': buffer=buffer[1:]; continue
                    frame,buffer=buffer[:36],buffer[36:]
                    if frame[2]!=0x01: print(f"[RAW?]{frame.hex()}"); continue
                    print(f"\n[RAW]{frame.hex()}")
                    decoded=parse_packet(frame)
                    if decoded: print_decoded(decoded)
            else:
                misc_counter+=1
                print(f"↩️ Misc: {data.hex()}")

        await client.start_notify(CHAR_UUID,handle_notify)

        try: await asyncio.wait_for(status_ready.wait(),timeout=2.0)
        except asyncio.TimeoutError: print("⚠️ Kein Status empfangen")

        await learn_adu(client)

        try:
            while True:
                await asyncio.sleep(0.05)
                key=read_key_nonblocking()
                if not key: continue
                if _debounced(): continue
                if key in ("PLUS","UP"): await cc_nudge(client,+1)
                elif key in ("MINUS","DOWN"): await cc_nudge(client,-1)
                elif key=="CC_ENTER": print("↪️ Edit"); await cc_enter_edit(client)
                elif key=="OK": print("↪️ OK"); await cc_ok(client)
                elif key=="SET": print("↪️ SET"); await cc_set(client)
                elif key=="PLUS_STEPS":
                    steps=await prompt_int("+Schritte")
                    if steps: await cc_nudge(client,steps)
                elif key=="MINUS_STEPS":
                    steps=await prompt_int("-Schritte")
                    if steps: await cc_nudge(client,-steps)
                elif key=="STARTSTOP": await send_onoff_toggle(client)
                elif key=="ENTER": print("(ENTER ignoriert)")
                elif key=="QUIT": print("🚪 Quit"); break
        finally:
            await client.stop_notify(CHAR_UUID); _restore_keyboard()

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        _restore_keyboard(); print("\n⛔️ Manuell beendet.")
