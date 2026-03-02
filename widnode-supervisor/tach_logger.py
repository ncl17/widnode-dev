#!/usr/bin/env python3
import os, time, threading, sys, math
import gpiod
from collections import deque
from datetime import datetime
from pathlib import Path

# ===============================
# CONFIG
# ===============================
CHIP = os.getenv("TACH_CHIP", "/dev/gpiochip0")
LINE = int(os.getenv("TACH_LINE", "1"))
PPR = float(os.getenv("TACH_PPR", "1"))  # Pulsos por revolución
AVG_WINDOW = float(os.getenv("TACH_AVG_WINDOW_SEC", "2"))
LOG_PERIOD = float(os.getenv("TACH_LOG_PERIOD_SEC", "10"))
ALPHA = float(os.getenv("TACH_FILTER_ALPHA", "1.0"))  # 0..1 filtro exponencial
MIN_PERIOD_MS = float(os.getenv("TACH_MIN_PERIOD_MS", "1.0"))  # antirrebote/sw
DATA_DIR = Path(os.getenv("TACH_DIR", "/data/tacho"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
# === NUEVO: retención diaria estilo tach_logger ===
KEEP_DAYS = int(os.getenv("TACH_KEEP_DAYS", "14"))
CSV_BASENAME = os.getenv("TACH_BASENAME", "tacho")  # ej: tacho-YYYY-MM-DD.csv


# ===============================
# ESTADO
# ===============================
pulse_times = deque()  # timestamps en segundos (monotónicos)
lock = threading.Lock()
rpm_filt = 0.0
_current_date = None
_log_file = None
# === NUEVO: parámetros de logging al estilo tach_logger ===
def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def _purge_old_files():
    """Mantiene solo los últimos KEEP_DAYS archivos diarios."""
    files = sorted(DATA_DIR.glob(f"{CSV_BASENAME}-*.csv"))
    if len(files) <= KEEP_DAYS:
        return
    for f in files[:-KEEP_DAYS]:
        try:
            f.unlink()
            print(f"[TACH] purgado {f}")
        except Exception:
            pass

def _open_log_for_today_two_cols():
    """
    Abre (y rota) archivo diario, mantiene symlink CSV_BASENAME.csv -> CSV_BASENAME-YYYY-MM-DD.csv
    Cabecera: 'timestamp,rpm'
    """
    global _current_date, _log_file
    today = datetime.now().date().isoformat()
    if _current_date == today and _log_file and not _log_file.closed:
        return _log_file

    # cerrar anterior si había
    if _log_file and not _log_file.closed:
        _log_file.flush()
        _log_file.close()

    _current_date = today
    fname = DATA_DIR / f"{CSV_BASENAME}-{today}.csv"

    # actualizar symlink (relativo dentro de DATA_DIR)
    try:
        link = DATA_DIR / f"{CSV_BASENAME}.csv"
        tmp  = DATA_DIR / f".{CSV_BASENAME}.csv.tmp"
        if tmp.exists():
            tmp.unlink()
        tmp.symlink_to(fname.name)
        tmp.replace(link)
    except Exception:
        pass

    new_file = not fname.exists()
    f = open(fname, "a", buffering=1)
    if new_file:
        f.write("timestamp,rpm\n")

    _purge_old_files()
    print(f"[TACH] logging en {fname}")
    _log_file = f
    return f

def _csv_path_for_today() -> Path:
    d = datetime.now()
    return DATA_DIR / f"{CSV_BASENAME}-{d:%Y-%m-%d}.csv"

def _ensure_symlink_to_today(csv_path: Path):
    link = DATA_DIR / f"{CSV_BASENAME}.csv"
    try:
        if link.is_symlink() or link.exists():
            try:
                link.unlink()
            except FileNotFoundError:
                pass
        link.symlink_to(csv_path.name)  # symlink relativo dentro de DATA_DIR
    except Exception as e:
        print(f"[TACH][WARN] No pude crear symlink {link}: {e}", file=sys.stderr)

def _ensure_csv_header_two_cols(csv_path: Path):
    if not csv_path.exists():
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", buffering=1) as f:
            f.write("timestamp,rpm\n")

def _rotate_daily_if_needed(current_csv: Path) -> Path:
    today = _csv_path_for_today()
    if current_csv is None or (current_csv.exists() and not current_csv.samefile(today)) or (not current_csv.exists() and today.exists()) or (current_csv != today):
        _ensure_csv_header_two_cols(today)
        _ensure_symlink_to_today(today)
        return today
    return current_csv

def _append_csv_row(csv_path: Path, row: str):
    with csv_path.open("a", buffering=1) as f:
        f.write(row)

def _enforce_circular(csv_path: Path, max_rows: int):
    if max_rows <= 0:
        return
    try:
        with csv_path.open("r") as f:
            lines = f.readlines()
        if len(lines) <= max_rows + 1:  # +1 por el header
            return
        header, data = lines[0], lines[1:]
        data = data[-max_rows:]
        with csv_path.open("w") as f:
            f.write(header)
            f.writelines(data)
    except Exception as e:
        print(f"[TACH][WARN] Circular CSV falló: {e}", file=sys.stderr)

# ===============================
# UTIL
# ===============================
def event_ts_seconds(evt):
    """Devuelve timestamp del evento en segundos (float), según versión de libgpiod."""
    ns = getattr(evt, "timestamp_ns", None)
    if isinstance(ns, int) and ns > 0:
        return ns / 1e9
    sec = getattr(evt, "sec", None)
    nsec = getattr(evt, "nsec", None)
    if isinstance(sec, int) and isinstance(nsec, int):
        return sec + (nsec / 1e9)
    ts = getattr(evt, "timestamp", None)
    if isinstance(ts, int) and ts > 0:
        return ts / 1e9
    return None

def safe_event_wait(line, timeout_s: float) -> bool:
    """
    Espera eventos de forma compatible:
      - Primero intenta con milisegundos (int) -> algunas bindings v1 lo requieren.
      - Si falla por TypeError, reintenta con segundos (float).
    """
    ms = max(1, int(math.ceil(timeout_s * 1000.0)))
    try:
        return bool(line.event_wait(ms))
    except TypeError:
        return bool(line.event_wait(timeout_s))

# def compute_rpm(now):
#     limit = now - AVG_WINDOW
#     with lock:
#         while pulse_times and pulse_times[0] < limit:
#             pulse_times.popleft()
#         pulses = len(pulse_times)
#         # # 🔍 DEBUG: imprimir los timestamps actuales
#         # print("[DEBUG] pulse_times:", list(pulse_times), flush=True)
#     if pulses == 0:
#         return 0.0, 0
#     rpm = (pulses / PPR) * (60.0 / AVG_WINDOW)
#     return rpm, pulses
def compute_rpm(now):
    """
    Estima RPM usando los pulsos dentro de [now-AVG_WINDOW, now], pero
    midiendo tiempo real entre el primer y el último pulso de esa ventana.
    Fórmula:
       rpm = ((N-1)/PPR) * 60 / (t_last - t_first)
    donde N es la cantidad de pulsos en la ventana (N>=2), y (N-1) son los
    intervalos completos observados entre pulsos.
    """
    window_start = now - AVG_WINDOW

    with lock:
        # 1) purgar fuera de la ventana
        while pulse_times and pulse_times[0] < window_start:
            pulse_times.popleft()

        # 2) snapshot para calcular sin sostener el lock
        pulses_list = list(pulse_times)

    N = len(pulses_list)
    if N < 2:
        return 0.0, N

    t_first = pulses_list[0]
    t_last  = pulses_list[-1]
    T = t_last - t_first
    if T <= 0:
        return 0.0, N

    # (N-1)/T -> pulsos/seg; dividir por PPR -> rev/seg; *60 -> RPM
    rpm_edge_locked = ((N - 1) / T) * (60.0 / PPR)
    return rpm_edge_locked, N

# ===============================
# HILOS
# ===============================
def gpio_listener():
    try:
        chip = gpiod.Chip(CHIP)
    except Exception as e:
        print(f"[ERR] No puedo abrir {CHIP}: {e}", file=sys.stderr, flush=True)
        os._exit(2)

    line_obj = chip.get_line(LINE)
    try:
        line_obj.request(consumer="tachometer", type=gpiod.LINE_REQ_EV_RISING_EDGE)
    except Exception as e:
        print(f"[ERR] No puedo solicitar línea {LINE} en {CHIP}: {e}", file=sys.stderr, flush=True)
        os._exit(3)

    last_ts = None
    min_period_s = max(0.0, MIN_PERIOD_MS / 1000.0)

    while True:
        if safe_event_wait(line_obj, max(1.0, AVG_WINDOW)):
            try:
                evt = line_obj.event_read()
            except Exception as e:
                print(f"[WARN] event_read() falló: {e}", file=sys.stderr, flush=True)
                continue

            ts = event_ts_seconds(evt)
            if ts is None:
                ts = time.monotonic()
                if last_ts is None:
                    print("[WARN] libgpiod sin timestamp explícito; usando time.monotonic()", flush=True)

            # Antirrebote / antirruido por periodo mínimo
            if last_ts is not None and (ts - last_ts) < min_period_s:
                continue
            last_ts = ts

            with lock:
                pulse_times.append(ts)
        # else: timeout, el writer se ocupa de purgar

def log_writer():
    global rpm_filt
    _ensure_dir()
    f = _open_log_for_today_two_cols()  # abre/rota y deja header

    next_save = time.monotonic() + LOG_PERIOD
    while True:
        now = time.monotonic()
        rpm, _pulses = compute_rpm(now)
        rpm_filt = ALPHA * rpm + (1 - ALPHA) * rpm_filt

        if now >= next_save:
            # por si cambió el día, reabrimos/rotamos
            f = _open_log_for_today_two_cols()
            ts = datetime.now().astimezone().isoformat(timespec="seconds")
            f.write(f"{ts},{rpm_filt:.2f}\n")
            f.flush()
            # print(f"[{ts}] RPM: {rpm_filt:.2f}", flush=True)
            next_save += LOG_PERIOD

        time.sleep(0.01)



# ===============================
# MAIN
# ===============================
if __name__ == "__main__":
    t = threading.Thread(target=gpio_listener, daemon=True)
    t.start()
    log_writer()
