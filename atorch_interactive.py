#!/usr/bin/env python3
# atorch_interactive.py — Live-Decode + Button-Steuerung (Atorch DL24x)
# Tasten:
#   [+]/[↑]   : Iset +1 Schritt
#   [-]/[↓]   : Iset -1 Schritt
#   [e]       : M (kurz) – Cursor/Spalte weiter
#   [m]       : MPPT-Sweep (coarse + fine, event-getrieben), schreibt CSV-Ergebnis
#   [p]/[n]   : +N / -N Schritte
#   [s]       : Start/Stop (FF55 0x32)
#   [q]       : Quit
#
# Hinweise:
# - Frames (~1 Hz) kommen vom Gerät; der MPPT wartet nach jedem Stellschritt auf den
#   nächsten Mess-Frame und bewertet erst dann (event-getrieben). Zusätzlich sendet er
#   mehrere '+' zwischen Frames (Burst), bewertet aber nur bei neuem Frame.
# - CSV: mppt_results.csv mit Spalten: timestamp,spannung_v,strom_a,leistung_w

import asyncio
import sys
import time
import termios
import select
import atexit
import os
import csv
from bleak import BleakClient

# ==== Anpassungen ====
ADDRESS = "3A:C5:E2:C6:AD:58"     # <- MAC/UUID anpassen
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Tunables
DEBOUNCE_MS = 120
NUDGE_STEP_DELAY_MS = 40                # Zusatzwartezeit nach jedem Schritt (ms)
WAIT_DELTA_A = 0.0005
WAIT_DELTA_TIMEOUT_MS = 800

# --- MPPT Tuning ---
MPPT_MAX_STEPS = 500          # Sicherheitslimit coarse
MPPT_CONSECUTIVE_DOWNS = 3    # so viele schlechtere Frames in Folge => Peak in coarse
BURST_PER_FRAME = 3           # max. + Taps zwischen zwei Frames (coarse)

# Fine-Dither rund um das Maximum:
MPPT_FINE_CYCLES = 10         # so viele Dither-Zyklen versuchen
MPPT_FINE_TOL_W = 0.05        # minimale Leistungsverbesserung (W), damit „besser“ gilt
MPPT_FINE_NO_IMPROVE_MAX = 4  # so viele Zyklen ohne Verbesserung -> abbrechen
MPPT_RETURN_TO_BEST = True    # am Ende auf bestes Digit zurückspringen

# CSV Datei
CSV_PATH = "mppt_results.csv"

# ==== Statuspuffer ====
buffer = bytearray()
last_current_a = None
last_voltage_v = None
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
    if ch1 == "\x1b":  # ESC/CSI (Arrow keys)
        ch2 = sys.stdin.read(1) or ""
        ch3 = sys.stdin.read(1) or ""
        if ch2 == "[":
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(ch3, None)
        return "ESC"
    if ch1 in ("\r", "\n"):  return "ENTER"
    if ch1 == "\x7f":        return "BACKSPACE"
    if ch1 == "+":           return "PLUS"
    if ch1 == "-":           return "MINUS"
    if ch1 in ("s","S"):     return "STARTSTOP"
    if ch1 in ("e","E"):     return "M_TAP"        # M (kurz)
    if ch1 in ("m","M"):     return "MPPT"         # MPPT-Sweep
    if ch1 in ("p","P"):     return "PLUS_STEPS"
    if ch1 in ("n","N"):     return "MINUS_STEPS"
    if ch1 in ("q","Q"):     return "QUIT"
    # Für Eingabedialoge (p/n)
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
    """
    Ausgabe ohne Temperatur & Laufzeit – alles andere bleibt:
    U (V), I (A), Ah, Wh
    """
    global last_current_a, last_voltage_v, frame_counter
    if not p:
        return
    last_current_a = p.get("Strom_A", None)
    last_voltage_v = p.get("Spannung_V", None)
    frame_counter += 1
    if not status_ready.is_set() and last_current_a is not None:
        status_ready.set()
    print(
        f"{p['Spannung_V']:.2f} V   "
        f"{p['Strom_A']:.3f} A   "
        f"{p['Kapazität_Ah']:.2f} Ah   "
        f"{p['Energie_Wh']:.2f} Wh"
    )

# ---------- FF55 Start/Stop ----------
def build_atorch_cmd(cmd: int, adu: int = 0x02) -> bytes:
    # FF 55 11 ADU CMD 00 00 00 00 CHK  (CHK = XOR ab byte[2], init 0x44)
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
BTN_SET, BTN_PLUS, BTN_MINUS = 49, 51, 52  # M/Plus/Minus
def _make_button_packet(button_code:int, adu:int)->bytes:
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

# ---------- Aktionen ----------
async def m_tap(client: BleakClient):
    """M (kurz): bewegt den Cursor / wechselt Spalte."""
    adu = _detected_adu if _detected_adu is not None else 0
    await send_button_exact(client, BTN_SET, adu, delay_ms=120)
    print(f"↪️ M (kurz) gesendet (ADU={adu}).")

async def _wait_status_delta(min_delta=WAIT_DELTA_A, timeout=WAIT_DELTA_TIMEOUT_MS):
    start=asyncio.get_event_loop().time()
    base=last_current_a
    while (asyncio.get_event_loop().time()-start) < timeout/1000:
        await asyncio.sleep(0.02)
        if base is None: continue
        if last_current_a is not None and abs(last_current_a-base) >= min_delta:
            return True
    return False

def _current_power():
    if last_voltage_v is None or last_current_a is None:
        return None
    return last_voltage_v * last_current_a

async def wait_for_new_frame(prev_fc: int, timeout_s: float = 3.0) -> bool:
    """Wartet bis frame_counter > prev_fc (neuer Messframe) oder Timeout."""
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_s:
        if frame_counter > prev_fc:
            return True
        await asyncio.sleep(0.01)
    return False

async def cc_nudge(client, steps:int):
    """Wert ändern: +/−; wartet kurz auf Status."""
    if steps == 0: return
    btn = BTN_PLUS if steps > 0 else BTN_MINUS
    for _ in range(abs(steps)):
        await send_button(client, btn)
        _ = await _wait_status_delta()
        await asyncio.sleep(NUDGE_STEP_DELAY_MS/1000.0)

def _csv_append_result(path: str, ts_iso: str, u: float, i: float, p: float):
    """Schreibt (timestamp,spannung_v,strom_a,leistung_w) an CSV-Datei (mit Header, falls neu)."""
    file_exists = os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["timestamp", "spannung_v", "strom_a", "leistung_w"])
        w.writerow([ts_iso, f"{u:.3f}", f"{i:.3f}", f"{p:.3f}"])

async def mppt_sweep(client: BleakClient,
                     max_steps: int = MPPT_MAX_STEPS,
                     consec_downs: int = MPPT_CONSECUTIVE_DOWNS):
    """
    MPPT-Sweep:
      1) Coarse: Burst-weise '+' zwischen Frames, bewerte nur bei neuem Frame.
      2) Fine:   Um das Maximum herum mit +/- dither'n und verfeinern.
    Stop/Start der Last machst du manuell (Taste 's').
    Am Ende: CSV-Zeile mit timestamp, U, I, P.
    """
    if _detected_adu is None:
        print("⚠️ MPPT abgebrochen: ADU unbekannt. Drücke erst '+'/'-' zum Lernen.")
        return

    # Sicherstellen, dass wir frische Messung haben
    prev_fc = frame_counter
    await wait_for_new_frame(prev_fc, timeout_s=3.0)

    # Startwerte (aktueller Messpunkt)
    base_p = _current_power()
    if base_p is None:
        print("⚠️ MPPT: keine Messwerte verfügbar."); return
    best_p = base_p
    best_u = last_voltage_v or 0.0
    best_i = last_current_a or 0.0
    best_pos_steps = 0         # wie viele '+'-Schritte seit Start bis zum Maximum
    downs = 0
    total_steps = 0

    print(f"⚡ MPPT (coarse): bis {BURST_PER_FRAME} Schritte pro Frame, max {max_steps} Schritte …")

    # -------- Coarse Sweep: nach oben, bis es nicht mehr besser wird --------
    while total_steps < max_steps:
        this_prev = frame_counter

        # (1) innerhalb eines Frame-Intervalls bis zu BURST_PER_FRAME Schritte
        burst = min(BURST_PER_FRAME, max_steps - total_steps)
        for _ in range(burst):
            await send_button(client, BTN_PLUS)
            total_steps += 1
            await asyncio.sleep(0.02)  # sehr kurz

        # (2) auf neuen Frame warten
        got = await wait_for_new_frame(this_prev, timeout_s=5.0)
        if not got:
            print("⚠️ Kein neuer Messframe – Abbruch coarse.")
            break

        p = _current_power()
        if p is None:
            continue

        if p > best_p + 1e-9:
            best_p = p
            best_u = last_voltage_v or 0.0
            best_i = last_current_a or 0.0
            best_pos_steps = total_steps
            downs = 0
        else:
            downs += 1
            if downs >= consec_downs:
                # Peak offenbar überschritten
                break

    print(f"🔎 Coarse-Peak:  P={best_p:.2f} W   I={best_i:.3f} A   U={best_u:.2f} V  "
          f"(+{best_pos_steps} Schritte)")

    # -------- Fine Dither: um best_pos_steps herum mit +/- verfeinern --------
    print(f"🪄 MPPT (fine): Dithern ±1 um Peak, bis keine Verbesserung (> {MPPT_FINE_TOL_W:.2f} W) …")
    no_improve = 0
    pos = best_pos_steps  # aktuelle Position relativ zum Start (nur für Rückkehr)
    for cycle in range(1, MPPT_FINE_CYCLES + 1):
        improved = False

        # Versuche +1
        prev_fc = frame_counter
        await send_button(client, BTN_PLUS)
        pos += 1
        if not await wait_for_new_frame(prev_fc, timeout_s=3.0):
            break
        p_plus = _current_power()
        if p_plus is not None and (p_plus > best_p + MPPT_FINE_TOL_W):
            best_p = p_plus
            best_u = last_voltage_v or 0.0
            best_i = last_current_a or 0.0
            best_pos_steps = pos
            improved = True
        else:
            # wieder zurück, wenn nicht besser
            prev_fc = frame_counter
            await send_button(client, BTN_MINUS)
            pos -= 1
            await wait_for_new_frame(prev_fc, timeout_s=3.0)

        # Versuche -1
        prev_fc = frame_counter
        await send_button(client, BTN_MINUS)
        pos -= 1
        if not await wait_for_new_frame(prev_fc, timeout_s=3.0):
            break
        p_minus = _current_power()
        if p_minus is not None and (p_minus > best_p + MPPT_FINE_TOL_W):
            best_p = p_minus
            best_u = last_voltage_v or 0.0
            best_i = last_current_a or 0.0
            best_pos_steps = pos
            improved = True
        else:
            # wieder zurück, wenn nicht besser
            prev_fc = frame_counter
            await send_button(client, BTN_PLUS)
            pos += 1
            await wait_for_new_frame(prev_fc, timeout_s=3.0)

        if improved:
            no_improve = 0
        else:
            no_improve += 1
            if no_improve >= MPPT_FINE_NO_IMPROVE_MAX:
                print("ℹ️ Fine: keine Verbesserung mehr – beende Dithern.")
                break

    # -------- optional: zurück auf bestes Digit --------
    if MPPT_RETURN_TO_BEST and pos != best_pos_steps:
        delta = best_pos_steps - pos
        key = BTN_PLUS if delta > 0 else BTN_MINUS
        for _ in range(abs(delta)):
            await send_button(client, key)
            await asyncio.sleep(0.02)
        # einen letzten Frame abwarten, nur fürs Logging
        await wait_for_new_frame(frame_counter, timeout_s=2.0)

    # Ergebnis melden + CSV schreiben
    print(f"✅ MPP:  P={best_p:.2f} W   I={best_i:.3f} A   U={best_u:.2f} V")
    ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        _csv_append_result(CSV_PATH, ts_iso, best_u, best_i, best_p)
        print(f"📝 CSV: {CSV_PATH} (+1 Zeile)")
    except Exception as e:
        print(f"⚠️ CSV-Fehler: {e}")
    print("ℹ️ Stoppen/Starten der Last weiter manuell mit 's'.")

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
    async with BleakClient(ADDRESS, timeout=10.0) as client:
        print("✅ Verbunden.")
        print("Keys: [↑/+]=+, [↓/-]=- , [e]=M(kurz), [m]=MPPT, [p]/[n]=±N, [s]=Start/Stop, [q]=Quit")
        _setup_keyboard()

        def handle_notify(_, data: bytearray):
            global buffer, misc_counter
            if not data: return
            if len(data) == 8 and data[0:2] == b'\xff\x55' and data[2] == 0x02:
                # kurze Replies unterdrücken
                return
            if len(data) >= 2 and data[0:2] == b'\xff\x55':
                buffer += data
                while len(buffer) >= 36:
                    if buffer[0:2] != b'\xff\x55':
                        buffer = buffer[1:]; continue
                    frame, buffer[:] = buffer[:36], buffer[36:]
                    if frame[2] != 0x01:
                        continue
                    decoded = parse_packet(frame)
                    if decoded: print_decoded(decoded)
            else:
                misc_counter += 1

        await client.start_notify(CHAR_UUID, handle_notify)

        try:
            await asyncio.wait_for(status_ready.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            print("⚠️ Kein Status empfangen")

        await learn_adu(client)

        try:
            while True:
                await asyncio.sleep(0.05)
                key = read_key_nonblocking()
                if not key: continue
                if _debounced(): continue

                if key in ("PLUS","UP"):
                    await cc_nudge(client, +1)

                elif key in ("MINUS","DOWN"):
                    await cc_nudge(client, -1)

                elif key == "M_TAP":
                    await m_tap(client)

                elif key == "MPPT":
                    await mppt_sweep(client)

                elif key == "PLUS_STEPS":
                    steps = await prompt_int("+Schritte")
                    if steps: await cc_nudge(client, steps)

                elif key == "MINUS_STEPS":
                    steps = await prompt_int("-Schritte")
                    if steps: await cc_nudge(client, -steps)

                elif key == "STARTSTOP":
                    await send_onoff_toggle(client)

                elif key == "ENTER":
                    print("(ENTER ignoriert)")

                elif key == "QUIT":
                    print("🚪 Quit"); break

        finally:
            await client.stop_notify(CHAR_UUID)
            _restore_keyboard()

if __name__=="__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _restore_keyboard()
        print("\n⛔️ Manuell beendet.")
