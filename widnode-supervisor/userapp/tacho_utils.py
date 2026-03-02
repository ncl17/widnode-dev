#!/usr/bin/env python3
import os
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dateutil import tz

TACH_DIR = Path(os.getenv("TACH_DIR", "/data/tacho"))
LOCAL_TZ = tz.tzlocal()


def _find_daily_file(target: datetime):
    """Devuelve el archivo CSV diario correspondiente a la fecha del target"""
    fname = TACH_DIR / f"tacho-{target.strftime('%Y-%m-%d')}.csv"
    if fname.exists():
        return fname
    # fallback: si justo cambió de día, probar +/-1
    for delta in (-1, 1):
        alt = TACH_DIR / f"tacho-{(target + timedelta(days=delta)).strftime('%Y-%m-%d')}.csv"
        if alt.exists():
            return alt
    
    # Fallback legacy: archivo plano
    legacy = TACH_DIR / "tacho.csv"
    if legacy.exists():
        return legacy

    return None

def _aware(dt: datetime) -> datetime:
    """Convierte datetime naive a timezone local"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt


def get_rpm_nearest(timestamp: datetime):
    """
    Busca en los CSV diarios la RPM más cercana al timestamp dado.
    Devuelve (rpm, timestamp_encontrado) o (None, None) si no hay datos.
    """
    fpath = _find_daily_file(timestamp)
    if not fpath:
        return None, None

    target = _aware(timestamp)

    closest = None
    min_diff = timedelta.max

    with open(fpath, newline="") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
                ts = _aware(ts)
                diff = abs(ts - target)
                if diff < min_diff:
                    min_diff = diff
                    closest = (float(row["rpm"]), ts)
            except Exception:
                continue

    return closest if closest else (None, None)


def get_rpm_range(start: datetime, end: datetime):
    """
    Devuelve una lista [(timestamp, rpm), ...] entre dos fechas dadas.
    Puede abarcar varios archivos diarios.
    """
    results = []
    cur = start
    while cur.date() <= end.date():
        fpath = _find_daily_file(cur)
        if fpath and fpath.exists():
            with open(fpath, newline="") as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    try:
                        ts = datetime.fromisoformat(row["timestamp"])
                        if start <= ts <= end:
                            results.append((ts, float(row["rpm"])))
                    except Exception:
                        continue
        cur += timedelta(days=1)
    return results
