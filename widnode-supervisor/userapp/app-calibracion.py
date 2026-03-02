from utils import get_modbus_server_config, getMacBluetooth, restart_bluetooth, get_ip_addresses,get_api_server_address,detectar_nodo_ocupado
from widnode_signal import descomponer_medicion, descomponer_medicion_ext
from command_process import execute_scheduled_tasks
import httpx
from dotenv import load_dotenv
import subprocess
import os
import time
import binascii
import logging
import base64
import struct
import datetime
import asyncio
import numpy as np
import json,gzip
from typing import Optional
from bleak import BleakScanner, BleakClient, BleakError
from typing import Dict, Set,Iterable, List, Tuple
from collections.abc import Awaitable  # ← tipo correcto para corutinas/awaitables
from tacho_utils import get_rpm_nearest
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import configparser
from pathlib import Path
import math
import uuid
import random
# from sdnotify import SystemdNotifier
from modbus_server import GatewayModbusServer, Map



# --- Agent BlueZ (dbus-next)
from dbus_next.aio import MessageBus
from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.service import ServiceInterface, method
from dbus_next.errors import DBusError

# ============================================================
# BlueZ Agent para Passkey fijo (LESC Passkey Entry automático)
# ============================================================
AGENT_PATH = "/com/widnode/BLEAgent"
CAPABILITY = "KeyboardDisplay"  # Para que BlueZ llame RequestPasskey
BLUEZ_BUS = None  # se inicializa al registrar el agent

CSI = "\x1b["  # ANSI
RED = f"{CSI}31m"
GREEN = f"{CSI}32m"
RESET = f"{CSI}0m"
WIDNODE_GW = ""

# --- Control de recuperación BLE / supervisor ---
# Si el adaptador queda colgado (Powered: no + timeouts 110), evitamos loop infinito.
RESTART_FLAG = Path(os.getenv("RESTART_FLAG", "/data/restart.flag"))
BT_FAIL_STREAK_MAX = int(os.getenv("BT_FAIL_STREAK_MAX", "3"))  # ciclos consecutivos
BT_FAIL_STREAK = 0

rssi_by_identity: Dict[str, int] = {}


# --- Selector de partes a ejecutar dentro de process_widnode ---
# valores válidos: "config", "rms", "meas"
PROCESS_PARTS: set[str] = set()

def _parse_parts(s: str) -> set[str]:
    parts = {p.strip().lower() for p in (s or "").replace(";", ",").split(",") if p.strip()}
    valid = {"config", "rms", "meas"}
    bad = parts - valid
    if bad:
        logging.warning(f"⚠️ Parts inválidas ignoradas: {sorted(bad)} (válidas={sorted(valid)})")
        parts = parts & valid
    return parts

def init_process_parts_from_env():
    """
    ENV opcional: WIDNODE_PARTS=config,rms,meas
    Si no está seteada, default = todas.
    """
    global PROCESS_PARTS
    env = os.getenv("WIDNODE_PARTS", "").strip()
    PROCESS_PARTS = _parse_parts(env) if env else {"config", "rms", "meas"}

def _request_container_restart(reason: str) -> None:
    """Pide al supervisor que reinicie el contenedor (compose) escribiendo restart.flag."""
    try:
        tmp = str(RESTART_FLAG) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(reason[:500])
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, RESTART_FLAG)
    except Exception as e:
        logging.error(f"[CTRL] No pude crear {RESTART_FLAG}: {e}")

def _compress_ranges(indices: List[int]) -> List[Tuple[int, int]]:
    """Comprime [2,3,4,7] -> [(2,4),(7,7)]"""
    if not indices:
        return []
    res = []
    start = prev = indices[0]
    for x in indices[1:]:
        if x == prev + 1:
            prev = x
        else:
            res.append((start, prev))
            start = prev = x
    res.append((start, prev))
    return res

def summarize_measure_array(arr: Iterable[int], base_offset: int = 2, preview: int = 100):
    """
    Devuelve:
      - ones: cantidad de 1s
      - zeros: cantidad de 0s
      - ranges_str: rangos de índices (ya con offset del dispositivo)
      - colored_line: string con 0/1 donde el 1 va en color (primeros N)
      - ones_idx_dev: lista de índices *del dispositivo* con 1 (offset aplicado)
    """
    arr = list(arr)
    ones_idx = [i for i, v in enumerate(arr) if v == 1]
    ones = len(ones_idx)
    zeros = len(arr) - ones

    ones_idx_dev = [i + base_offset for i in ones_idx]
    ranges = _compress_ranges(ones_idx_dev)
    ranges_str = ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in ranges) if ranges else "-"


    return ones, zeros, ranges_str, ones_idx_dev


modbus_srv = None
widnode_modbus_index = {}
# notifier: Optional[SystemdNotifier] = None


# session_id por dispositivo (se invalida en cada start_notify nuevo)
session_id_by_addr: Dict[str, int] = {}

# tasks vivas por sesión/dispositivo (para cancelarlas al desconectar o reintentar)
session_tasks_by_addr: Dict[str, Set[asyncio.Task]] = {}

# set de opcodes permitidos por fase/sesión (gating para evitar mezclas)
expected_cmds_by_addr: Dict[str, Set[int]] = {}

HEALTH_PORT = int(os.getenv("USERAPP_HEALTH_PORT", "9090"))
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *args, **kwargs):  # silenciar logs
        return


def led_blink(duration=5, leds=("lan",), freq_hz=2.0, path="/data/led_blink.json"):
    tmp = path + ".tmp"
    payload = {"duration": float(duration), "leds": list(leds), "freq_hz": float(freq_hz)}
    with open(tmp, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _start_health_server():
    srv = HTTPServer(("127.0.0.1", HEALTH_PORT), _HealthHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv

def new_session(addr: str) -> int:
    """Abre nueva sesión/epoch para un dispositivo."""
    session_id_by_addr[addr] = session_id_by_addr.get(addr, 0) + 1
    # reset contenedor de tasks y expected opcodes
    session_tasks_by_addr[addr] = set()
    expected_cmds_by_addr[addr] = set()
    return session_id_by_addr[addr]

def get_session_id(addr: str) -> int:
    return session_id_by_addr.get(addr, 0)

def set_expected(addr: str, opcodes: Set[int]) -> None:
    expected_cmds_by_addr[addr] = set(opcodes)

def clear_expected(addr: str) -> None:
    expected_cmds_by_addr[addr] = set()

   
def track_task(addr: str, aw: Awaitable) -> asyncio.Task:
    """Crea y registra una task asociada a la sesión del addr."""
    t = asyncio.create_task(aw)
    session_tasks_by_addr.setdefault(addr, set()).add(t)
    t.add_done_callback(lambda tt, a=addr: session_tasks_by_addr[a].discard(tt))
    return t

# ============================================================
# RX pipeline optimizada para 0x60 (medición extendida)
#   - Evita crear 1 task por notificación (eso mata el throughput).
#   - La callback de notificación en Bleak/BlueZ NO es async: solo encola.
#   - Un único worker por dispositivo procesa en orden.
# ============================================================
ext60_queue_by_addr: Dict[str, "asyncio.Queue[tuple[bytes, BleakClient]]"] = {}
ext60_worker_task_by_addr: Dict[str, asyncio.Task] = {}

def _ensure_ext60_worker(addr: str) -> None:
    """Crea el worker de 0x60 si no existe (o si murió)."""
    t = ext60_worker_task_by_addr.get(addr)
    if t is not None and not t.done():
        return
    q = ext60_queue_by_addr.setdefault(addr, asyncio.Queue(maxsize=5000))
    # Worker asociado a la sesión actual; si cambia la sesión, se cancela arriba con cancel_session_tasks().
    ext60_worker_task_by_addr[addr] = track_task(addr, _ext60_worker(addr, q))

def enqueue_ext60_packet(addr: str, data: bytes, client: BleakClient) -> None:
    """Encola un paquete 0x60 para procesarlo en orden."""
    _ensure_ext60_worker(addr)
    q = ext60_queue_by_addr[addr]
    try:
        # Copiamos a bytes para que no dependas del buffer interno de Bleak/BlueZ.
        q.put_nowait((bytes(data), client))
    except asyncio.QueueFull:
        # Si llegás acá, ya estás perdiendo datos: preferible loggear fuerte.
        logging.error(f"[{addr}] 🚨 RX queue 0x60 llena (drop). Aumentá throughput o bajá tasa de notificaciones.")
        # Drop del paquete (no bloqueamos callback)

async def _ext60_worker(addr: str, q: "asyncio.Queue[tuple[bytes, BleakClient]]") -> None:
    """Procesa paquetes 0x60 secuencialmente."""
    while True:
        data, client = await q.get()
        try:
            # Mantengo el lock existente por seguridad (aunque 1 worker ya serializa).
            async with globals()["measurement_ext_lock"]:
                await process_60_response(data, client)
        except Exception:
            logging.exception(f"[{addr}] 🚨 Excepción en worker 0x60")
        finally:
            q.task_done()

async def cancel_session_tasks(addr: str):
    """Cancela con seguridad todas las tareas de la sesión actual del addr."""
    tasks = list(session_tasks_by_addr.get(addr, set()))
    for t in tasks:
        t.cancel()
    if tasks:
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            pass
    session_tasks_by_addr[addr] = set()

async def consultar_comando(WIDNODE_GW,cookies):
    gateway_cmd = f"{API_URL}/gateway/hdi_cd/{WIDNODE_GW}"
    # logging.info(f"----- 🧾 Consultando comando en {gateway_cmd}")
    try:
        async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientgwcmd:
            response = await clientgwcmd.get(gateway_cmd, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            return data.get("command")
    except httpx.HTTPError as e:
        print(f"Error al consultar el comando: {e}")
        return None

def is_tach_enabled(cfg_path="/data/config.ini"):
    config = configparser.ConfigParser()
    if not Path(cfg_path).exists():
        return False
    config.read(cfg_path)
    return config.getboolean("Tachometer", "enabled", fallback=False)


def ejecutar_comando(cmd_str):
    try:
        result = subprocess.run(
            cmd_str,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60
        )
        return result.stdout
    except Exception as e:
        return f"Error al ejecutar comando: {str(e)}"


async def enviar_resultado(salida,WIDNODE_GW, cookies):
    url = f"{API_URL}/gateway/hdi_cd/{WIDNODE_GW}"
    payload = {"output": salida}
    try:
        async with httpx.AsyncClient(cookies=cookies, timeout=10.0) as client:
            response = await client.put(url, json=payload)
            response.raise_for_status()
            logging.info("✅ Resultado enviado correctamente.")
    except httpx.HTTPError as e:
        logging.error(f"🚨 Error al enviar el resultado: {e}")

async def find_device_by_identity_dbus(target_identity: str) -> Optional[str]:
    """
    Devuelve la MAC actual (puede ser RPA) conocida por BlueZ para la 'identity' pedida.
    No escanea. Es 100% pasivo contra el ObjectManager.
    """
    bus = globals().get("BLUEZ_BUS")
    local_bus = False
    if bus is None:
        from dbus_next.aio import MessageBus
        from dbus_next import BusType
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        local_bus = True
    try:
        reply = await _dbus_call(
            bus, "org.bluez", "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"
        )
        objects = reply.body[0]
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            addr  = dev.get("Address").value  if "Address"  in dev else None
            ident = dev.get("Identity").value if "Identity" in dev else None  # puede no existir
            if target_identity and (addr == target_identity or (ident and ident == target_identity)):
                return addr  # devolvemos la MAC “actual” que tiene BlueZ (RPA o pública)
        return None
    finally:
        if local_bus:
            try:
                await bus.wait_for_disconnect()
            except Exception:
                pass

async def _get_bluez_bus():
    bus = globals().get("BLUEZ_BUS")
    if bus:
        return bus, False
    from dbus_next.aio import MessageBus
    from dbus_next import BusType
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    return bus, True

async def bluetooth_stop_discovery_if_active():
    bus, local_bus = await _get_bluez_bus()
    try:
        # Get org.bluez.Adapter1.Discovering
        # Properties.Get(interface:str, property:str) => signature "ss"
        reply = await _dbus_call(
            bus,
            "org.bluez",
            "/org/bluez/hci0",
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",                                     # <- FIRMA
            ["org.bluez.Adapter1", "Discovering"],    # <- BODY como lista
        )
        # dbus-next devuelve un Variant; extraemos su .value
        # Extraer el booleano del Variant
        discovering_variant = reply.body[0]
        discovering = bool(getattr(discovering_variant, "value", discovering_variant))

        if discovering:
            try:
                await _dbus_call(bus, "org.bluez", "/org/bluez/hci0", "org.bluez.Adapter1", "StopDiscovery")
                logging.info("⏹️ StopDiscovery ejecutado antes de conectar")
                await asyncio.sleep(0.2)
            except Exception as e:
                logging.debug(f"No se pudo StopDiscovery: {e}")
    finally:
        if local_bus:
            try:
                await bus.wait_for_disconnect()
            except Exception:
                pass


async def wait_event(event: asyncio.Event, total_timeout: float, tick: float = 10.0):
    """
    Espera un asyncio.Event con latidos intermedios al watchdog.
    Reemplaza a: await asyncio.wait_for(event.wait(), timeout=total_timeout)
    """
    # global notifier
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        try:
            return await asyncio.wait_for(event.wait(), timeout=min(tick, remaining))
        except asyncio.TimeoutError:
            # Patea el watchdog y sigue esperando por otro 'tick'
            # if notifier:
            #     notifier.notify("WATCHDOG=1")
            # cede el control al loop
            await asyncio.sleep(0)

def merge_alarma(byte_a, byte_b):
    """
    byte_a: 4 bits (rmsx, rmsy, rmz, temp)
    byte_b: 3 bits (rmsx_vel, rmsy_vel, rmsz_vel)
    """
    return (byte_b << 4) | (byte_a & 0x0F)

async def get_modbus_index_for_widnode(widnode_id: str) -> int:
    """Consulta a backend /modbus_id/<widnode_id>. Devuelve 0 si no existe."""
    global cookies
    url = f"{API_URL}/widnodes/modbus_id/{widnode_id}"
    try:
        async with httpx.AsyncClient(cookies=cookies, timeout=15.0) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return int(r.json().get("modbus_id", 0))
            return 0
    except Exception as e:
        logging.error(f"Error consultando modbus_id de {widnode_id}: {e}")
        return 0
    
def _get_default_pin():
    import os
    # Lee de entorno; si no está, usa 483729
    return int(os.getenv("WIDNODE_DEFAULT_PIN", "483729"))

class BLEAgent(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()  # legacy pairing (string)
    def RequestPinCode(self, device: "o") -> "s":
        pin = f"{_get_default_pin():06d}"
        logging.info(f"[AGENT] RequestPinCode {device} -> {pin}")
        return pin

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        logging.info(f"[AGENT] DisplayPinCode {device}: {pincode}")

    @method()  # LESC (uint32)
    def RequestPasskey(self, device: "o") -> "u":
        pin_val = int(_get_default_pin())
        logging.info(f"[AGENT] RequestPasskey {device}")
        return pin_val

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        logging.info(f"[AGENT] DisplayPasskey {device}: {passkey:06d} (entered={entered})")

    @method()  # Numeric comparison: NO lo usamos; rechazamos para forzar passkey
    def RequestConfirmation(self, device: "o", passkey: "u"):
        logging.warning(f"[AGENT] RequestConfirmation {device} {passkey:06d} -> Rejected")
        raise DBusError("org.bluez.Error.Rejected", "Numeric comparison no soportado")

    @method()
    def RequestAuthorization(self, device: "o"):
        logging.info(f"[AGENT] RequestAuthorization {device} -> OK")
        return

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        logging.info(f"[AGENT] AuthorizeService {device}, uuid={uuid} -> OK")
        return

    @method()
    def Cancel(self):
        logging.info("[AGENT] Cancel")

async def _dbus_call(bus: MessageBus, dest: str, path: str, iface: str, member: str,
                     signature: str = "", body=None):
    if body is None:
        body = []
    msg = Message(destination=dest, path=path, interface=iface, member=member,
                  signature=signature, body=body)
    reply = await bus.call(msg)
    if reply.message_type == MessageType.ERROR:
        raise DBusError(reply.error_name or "org.bluez.Error.Failed",
                        reply.body[0] if reply.body else "")
    return reply

async def start_bt_agent():
    """
    Conecta al system bus, exporta el Agent y lo registra como Default.
    Llamar una sola vez al inicio del programa.
    """
    global BLUEZ_BUS
    if BLUEZ_BUS is not None:
        return  # ya iniciado

    BLUEZ_BUS = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = BLEAgent()
    BLUEZ_BUS.export(AGENT_PATH, agent)

    # RegisterAgent + RequestDefaultAgent
    await _dbus_call(BLUEZ_BUS, "org.bluez", "/org/bluez",
                     "org.bluez.AgentManager1", "RegisterAgent",
                     "os", [AGENT_PATH, CAPABILITY])
    await _dbus_call(BLUEZ_BUS, "org.bluez", "/org/bluez",
                     "org.bluez.AgentManager1", "RequestDefaultAgent",
                     "o", [AGENT_PATH])
    # logging.info(f"[AGENT] Registrado en {AGENT_PATH} como Default (cap={CAPABILITY}). "
    #              f"PIN={_get_default_pin():06d}")

async def mark_trusted(mac_address: str):
    """
    (Opcional) Marca un dispositivo como Trusted al terminar el bonding.
    Lo llamamos luego de conectar con éxito, para que BlueZ recuerde el vínculo.
    """
    if not mac_address:
        return
    if not BLUEZ_BUS:
        return
    dev_path = f"/org/bluez/hci0/dev_{mac_address.replace(':', '_')}"
    try:
        await _dbus_call(BLUEZ_BUS, "org.bluez", dev_path,
                         "org.freedesktop.DBus.Properties", "Set",
                         "ssv", ["org.bluez.Device1", "Trusted", Variant('b', True)])
        logging.info(f"[AGENT] Device Trusted = True -> {mac_address}")
    except Exception as e:
        logging.warning(f"[AGENT] No se pudo marcar Trusted {mac_address}: {e}")

load_dotenv()

class WidnodeEvents:
    def __init__(self):
        self.msg_received_event = asyncio.Event()
        self.config_processed_event = asyncio.Event()
        self.error_status_processed_event = asyncio.Event()
        self.alarm_processed_event = asyncio.Event()
        self.measurements_processed_event = asyncio.Event()
        self.measurementsEX_processed_event = asyncio.Event()
        self.cmd_processed_event = asyncio.Event()
        self.rms_processed_event = asyncio.Event()
        self.rms_descargada_event = asyncio.Event()
        self.med_descargada_event = asyncio.Event()
        self.med_array_event = asyncio.Event()
        self.rms_mem_save_event = asyncio.Event()
        self.msg_connect_event = asyncio.Event()

events = WidnodeEvents()



PACKET_PAYLOAD = 238
HEADER_SIZE     = 17
# WRITE_CHAR_UUID = "0000FE42-8E22-4541-9D4C-21EDAE82ED19"
# NOTIFY_CHAR_UUID = "0000FE44-8E22-4541-9D4C-21EDAE82ED19"
WRITE_CHAR_UUID = os.getenv("WRITE_CHAR_UUID")
NOTIFY_CHAR_UUID = os.getenv("NOTIFY_CHAR_UUID")

WIDNODE_SET: set[str] = set()

# --- Override de lista de nodos para modo prueba/calibración ---
OVERRIDE_WIDNODES: Optional[List[str]] = None  # si no es None, se usa esto y se omite API /widnodes/gw/...

def _parse_widnode_ids_text(text: str) -> List[str]:
    """
    Acepta:
      - líneas con MAC (AA:BB:CC:DD:EE:FF)
      - separadas por coma/espacio
      - permite comentarios con '#'
    """
    ids: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        # soporta "a,b,c" o "a b c"
        parts = [p.strip() for p in line.replace(",", " ").split()]
        for p in parts:
            if p:
                ids.append(p)
    # dedupe preservando orden
    seen = set()
    out = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def load_widnodes_from_file(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return _parse_widnode_ids_text(f.read())


# --- NUEVO: filtro por servicio y cache de RPA ---
WIDNODE_SERVICE_UUID = os.getenv("WIDNODE_SERVICE_UUID")  # opcional, ej: "0000fe42-8e22-4541-9d4c-21edae82ed19"
WIDNODE_NAME_PREFIX = os.getenv("WIDNODE_NAME_PREFIX", "")  # opcional, ej: "WIDNODE"
LAST_SEEN_FILE = os.getenv("LAST_SEEN_FILE", "/var/lib/widnode/last_seen_rpa.json")
_last_seen_map = {}


async def prime_rssi_cache(timeout: float = 20.0) -> None:
    """
    Escanea durante `timeout` segundos y actualiza rssi_by_identity[addr]
    solo para devices dentro de GW_WIDNODES_SET.
    No borra valores previos.
    """
    global rssi_by_identity, WIDNODE_SET

    if not WIDNODE_SET:
        logging.info("📡 prime_rssi_cache: GW_WIDNODES_SET vacío, no se escanea.")
        return

    def detection_callback(device, advertisement_data):
        addr = device.address
        rssi = advertisement_data.rssi
        if addr in WIDNODE_SET and isinstance(rssi, int):
            rssi_by_identity[addr] = rssi

    scanner_kwargs = {"detection_callback": detection_callback}
    if WIDNODE_SERVICE_UUID:
        scanner_kwargs["service_uuids"] = [WIDNODE_SERVICE_UUID]

    scanner = BleakScanner(**scanner_kwargs)

    logging.info(f"📡 Precargando RSSI: scanning {timeout:.1f}s para {len(WIDNODE_SET)} widnodes...")
    try:
        await scanner.start()
        await asyncio.sleep(timeout)
    finally:
        await scanner.stop()

    logging.info(f"📡 Precarga RSSI completa. Cache size={len(rssi_by_identity)}")



def _load_last_seen():
    global _last_seen_map
    try:
        os.makedirs(os.path.dirname(LAST_SEEN_FILE), exist_ok=True)
        if os.path.exists(LAST_SEEN_FILE):
            with open(LAST_SEEN_FILE, "r") as f:
                _last_seen_map = json.load(f)
        else:
            _last_seen_map = {}
    except Exception as e:
        logging.warning(f"No se pudo cargar LAST_SEEN_FILE: {e}")
        _last_seen_map = {}

def _save_last_seen():
    try:
        with open(LAST_SEEN_FILE, "w") as f:
            json.dump(_last_seen_map, f)
    except Exception as e:
        logging.warning(f"No se pudo guardar LAST_SEEN_FILE: {e}")

# def get_cached_rpa(identity_mac: str) -> Optional[str]:
#     return _last_seen_map.get(identity_mac)

# def set_cached_rpa(identity_mac: str, rpa_mac: str):
#     _last_seen_map[identity_mac] = rpa_mac
#     _save_last_seen()
def get_cached_rpa(identity_mac: str) -> Optional[str]:
    entry = _last_seen_map.get(identity_mac)
    if not entry:
        return None
    # retrocompat: puede ser string o dict
    if isinstance(entry, str):
        return entry
    try:
        ttl = int(os.getenv("RPA_TTL_SECONDS", "1800"))  # 30 min por defecto
        if time.time() - entry.get("ts", 0) > ttl:
            return None
        return entry.get("addr")
    except Exception:
        return None

def set_cached_rpa(identity_mac: str, rpa_mac: str):
    try:
        _last_seen_map[identity_mac] = {"addr": rpa_mac, "ts": int(time.time())}
    except Exception:
        _last_seen_map[identity_mac] = rpa_mac  # fallback string
    _save_last_seen()


# CONFIG_FILE = "/home/widnode/dev/widnode_ap/config.ini"
CONFIG_FILE = os.getenv("CONFIG_FILE")

# API_URL="http://192.168.5.4:5008/api"
# API_URL="http://190.191.28.5:5008/api"
API_URL = get_api_server_address()

LOGIN_ENDPOINT = "/login/"


## COMANDOS ##
COMMAND_CONFIG = bytearray([0x49, 0x44, 0xCA])  # SOLICITA LA CONFIGURACION
COMMAND_ERROR_STATUS = bytearray([0x49, 0x44, 0x29])  # SOLICITA LA CONFIGURACION
# SOLICITA ARRAY DE ESTADO DE MEDICIONES
COMMAND_MED = bytearray([0x49, 0x44, 0xCC])
# SOLICITA DESCARGA DE MEDICION EXTENDIDA
COMMAND_MED_EXT = bytearray([0x49, 0x44, 0x60])
# SOLICITA TODAS LAS MEDICIONES GUARDADAS DE RMS
COMMAND_RMS = bytearray([0x49, 0x44, 0xDD])
# SOLICITA DESCARGA DE UNA MEDICION EN PARTICULAR
COMMAND_CE = bytearray([0x49, 0x44, 0xCE])
# MARCA LA MEDICION X COMO DESCARGADA
COMMAND_CF = bytearray([0x49, 0x44, 0xCF])
# MARCA LA MEDICION RMS X COMO DESCARGADA
COMMAND_DB = bytearray([0x49, 0x44, 0xDB])
# GUARDA LA STATUS RMS EN LA MEMORIA INTERNA
COMMAND_SAVE_RMS_MEM = bytearray([0x49, 0x44, 0x52])
# IDNODE_GET_STATE_ALARM OBTIENE EL ESTADO DE ALARMA DEL NODO
COMMAND_ALARM_STATE = bytearray([0x49, 0x44, 0x25])


## VARIABLES ##
medicion_descargada = True
fecha = datetime.datetime.now()
fecha_extendida = datetime.datetime.now()
rmsx = 0
rmsy = 0
rmsz = 0
temp = 0
sample_rate = 0
alarma = 0
cantidadMensajes = 0
cantidadMensajesRMS = 0
rmsx_vel = 0
rmsy_vel = 0
rmsz_vel = 0
alarma_vel = 0
idx_actual = 0
rms_idx_list = []
indices_para_marcar = []
# rssi_device_connected = -1
command_id_actual = 0
buffer = bytearray(49152)
buffer_ext = bytearray(6000*PACKET_PAYLOAD)
msg_count = 0
TIMEOUT = 250
MAX_RETRIES = 3
SCAN_TIMEOUT = 60
TIMEOUT_DEVICE_PROCESS = 2000
TIME_TO_MARK_DOWNLOAD = 50
TIME_TO_SAVE_MEM = 10
hayComandos = False
token = 0
cookies = httpx.Cookies()


# ============================================================
# Auth / Re-auth (minimal + robust)
# ============================================================
class ReauthNeeded(Exception):
    'Senal interna para forzar re-login (por ejemplo, HTTP 401).'


AUTH_BACKOFF_MIN = float(os.getenv('AUTH_BACKOFF_MIN', '1'))
AUTH_BACKOFF_MAX = float(os.getenv('AUTH_BACKOFF_MAX', '60'))

# lock para evitar que varias corutinas re-logueen a la vez
_AUTH_LOCK = asyncio.Lock()

def _raise_if_unauthorized(resp: httpx.Response, ctx: str = '') -> None:
    if resp is not None and getattr(resp, 'status_code', None) == 401:
        detail = ''
        try:
            detail = resp.text
        except Exception:
            detail = ''
        raise ReauthNeeded(f'401 unauthorized {ctx}'.strip() + (f' | {detail}' if detail else ''))

async def reauth(email: str, password: str) -> str:
    'Hace re-login de forma serializada. Devuelve el token (string) o lanza si falla.'
    global token
    async with _AUTH_LOCK:
        new_token = await login(email, password)
        if not new_token:
            raise ReauthNeeded('Re-login failed')
        token = str(new_token).lstrip('$')
        return token


async def ensure_authenticated(email: str, password: str, reason: str = '') -> str:
    """Alias de reauth (mantiene el nombre usado en el loop)."""
    return await reauth(email, password)


cantidad_byte_med_ext = 0

error_en_fecha = False
rms_download_active = False
MAX_WAIT_TIME_BUSY = 140

## EVENTOS DE CONTROL ##


# Configuración básica de logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                    handlers=[logging.StreamHandler()])

# Establece el nivel de registro de httpx en WARNING para evitar logs de solicitudes HTTP
# logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx").disabled = True

logging.info('....🚀widnode gateway start version 1.0.0 🚀....')



async def login(email, password):
    global cookies
    # Nota: el backend devuelve access_token en JSON y el resto de la app usa cookie 'access_token_cookie'.
    try:
        if not email or not password:
            logging.error('🚨 EMAIL/PASSWORD no configurados en variables de entorno')
            return None

        # Importante: limpiar la cookie previa para evitar estados raros
        cookies.clear()

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f'{API_URL}{LOGIN_ENDPOINT}',
                json={'email': email, 'password': password},
            )

            if response.status_code == 200:
                data = response.json()
                access_token = data.get('access_token')
                if not access_token:
                    logging.error('🚨 Login OK pero sin access_token en respuesta')
                    return None
                cookies.set('access_token_cookie', access_token)
                logging.info('✔️ Login successful')
                return access_token

            logging.error(f'🚨 Login failed: {response.status_code} - {response.text}')
            return None
    except Exception as e:
        logging.error(f'Exception during login: {e}')
        return None


# async def refresh_rssi_for_identity(identity: str, addr: str, timeout: float = 2.0):
#     # scan corto para capturar un advertisement y su RSSI
#     devices = await BleakScanner.discover(timeout=timeout)
#     for d in devices:
#         if d.address == addr:
#             # en Bleak, a veces viene en d.rssi
#             rssi = getattr(d, "rssi", None)
#             if rssi is not None:
#                 rssi_by_identity[identity] = int(rssi)
#             return

async def find_device_by_address(target_address: str):
    """
    target_address = 'identidad' que trae tu API (puede ser Public/Static).
    1) Intenta por última RPA cacheada.
    2) Escanea con filtro por Service UUID (si está definido), admitiendo:
       - address == target_address (identidad)
       - address == cached_rpa
       - advertisement_data contiene WIDNODE_SERVICE_UUID (+ opcional nombre que matchee prefix)
    """

        # 0) Intentar por identidad conocida en BlueZ (sin escanear)
    global rssi_by_identity
    
    
    try:
        dbus_addr = await find_device_by_identity_dbus(target_address)
        if dbus_addr:
            dev = await BleakScanner.find_device_by_address(dbus_addr, timeout=5.0)
            if dev:
                logging.info(f"🔎 Encontrado vía D-Bus (sin scan): identity={target_address} -> addr={dbus_addr}")

                try:
                    set_cached_rpa(target_address, dbus_addr)
                except Exception:
                    pass
                return dev
            
    except Exception as e:
        logging.debug(f"D-Bus identity lookup falló: {e}")

    
    found_device = None
    cached_rpa = get_cached_rpa(target_address)

    # 1) Fast path por última RPA
    if cached_rpa:
        try:
            dev = await BleakScanner.find_device_by_address(cached_rpa, timeout=5.0)
            if dev:
                # await refresh_rssi_for_identity(target_address, dev.address, timeout=1.5)
                logging.info(f"🔎 Usando RPA cacheada para {target_address}: {cached_rpa}")
                return dev
        except Exception as e:
            logging.debug(f"No se encontró por RPA cacheada {cached_rpa}: {e}")

    # 2) Escaneo con filtros
    def matches_service(ad):
        if not WIDNODE_SERVICE_UUID:
            return False
        try:
            su = ad.service_uuids or []
            return any(u.lower() == WIDNODE_SERVICE_UUID.lower() for u in su)
        except Exception:
            return False

    def name_ok(name: Optional[str]):
        if not WIDNODE_NAME_PREFIX:
            return True
        return (name or "").startswith(WIDNODE_NAME_PREFIX)

    def detection_callback(device, advertisement_data):
        nonlocal found_device
        global rssi_by_identity
        # global rssi_device_connected
        addr = device.address
        name = device.name
        # 1) Cache last-seen RSSI (siempre que venga)
        rssi = advertisement_data.rssi
        if isinstance(rssi, int) and addr in WIDNODE_SET:
            rssi_by_identity[addr] = rssi      
        # 2) Tu lógica actual de "encontré el target"
        if addr == target_address or (cached_rpa and addr == cached_rpa):
            logging.info(f"----- Dispositivo {target_address} encontrado (addr match). RSSI: {advertisement_data.rssi}")
            found_device = device
            return

        if matches_service(advertisement_data) and name_ok(name):
            logging.info(f"----- Dispositivo {target_address} probable por UUID/Name. Addr={addr}, RSSI={advertisement_data.rssi}")
            found_device = device
            return

    # Intenta pasar filtro por service_uuids al scanner si está disponible
    scanner_kwargs = {"detection_callback": detection_callback}
    if WIDNODE_SERVICE_UUID:
        scanner_kwargs["service_uuids"] = [WIDNODE_SERVICE_UUID]
    scanner = BleakScanner(**scanner_kwargs)

    try:
        await scanner.start()
        for _ in range(SCAN_TIMEOUT * 10):
            if found_device:
                break
            await asyncio.sleep(0.1)
    finally:
        await scanner.stop()

    # Si encontramos, cacheamos su RPA para el futuro
    if found_device:
        try:
            set_cached_rpa(target_address, found_device.address)
        except Exception as e:
            logging.debug(f"No se pudo cachear RPA para {target_address}: {e}")

    return found_device

# async def write_characteristic(client, char_uuid, data, max_retries=3):
#     retries = 0
#     while retries < max_retries:
#         try:
#             await client.write_gatt_char(char_uuid, data)
#             # await write_characteristic(client, char_uuid, data)
#             # logging.info(f"Escritura exitosa en la característica {char_uuid}")
#             return True  # Salir si la escritura fue exitosa
#         except BleakError as e:
#             retries += 1
#             msg = str(e)
#             logging.error(f"🚨 Error al escribir en {char_uuid}: {e}")
#             if ("Service Discovery has not been performed" in msg) or ("Not connected" in msg):
#                 try:
#                     if getattr(client, "is_connected", False):
#                         # 1) Intento de rediscovery
#                         try:
#                             # Bleak >=0.22: get_services(force_update=True); si no, sin arg.
#                             await client.get_services()
#                         except TypeError:
#                             await client.get_services()
#                     # Si no está conectado, devolvemos False para que el caller decida reconectar
#                 except Exception as rec_e:
#                     logging.debug(f"⚠️ Falló intento de rediscovery: {rec_e}")
#             logging.info(f"🔂 Reintentando... ({retries}/{max_retries})")
#             await asyncio.sleep(1)  # Espera un segundo antes de reintentar
#         except Exception as e:
#             logging.error(
#                 f"🚨 Error inesperado al escribir en {char_uuid}: {e}")
#             break  # Salir del bucle en caso de un error inesperado

#     logging.error(
#         f"🚨 No se pudo escribir en la característica {char_uuid} después de {max_retries} intentos")
#     return False
async def write_characteristic(client, char_uuid, data, max_retries=3):
    retries = 0
    while retries < max_retries:
        try:
            await client.write_gatt_char(char_uuid, data)
            return True
        except BleakError as e:
            msg = str(e)
            retries += 1
            logging.error(f"🚨 Error al escribir en {char_uuid}: {msg}")

            need_reconnect = (
                "Not connected" in msg or
                "Service Discovery has not been performed" in msg
            )

            try:
                if need_reconnect:
                    # Si está conectado, intentá refrescar servicios; si no, reconectá.
                    if getattr(client, "is_connected", False):
                        try:
                            await client.get_services()
                        except Exception:
                            pass
                    else:
                        # reconexión de 1 tiro
                        await client.connect()
                        # rearmar notify si lo teníamos activo en el flujo
                        try:
                            sid = new_session(client.address)
                            await client.start_notify(
                                NOTIFY_CHAR_UUID,
                                lambda s, d, cli=client, session_id=sid: notification_handler(s, d, cli, session_id)
                            )
                        except Exception as ne:
                            logging.debug(f"No se pudo reactivar notify: {ne}")
            except Exception as rec_e:
                logging.debug(f"⚠️ Falló intento de recuperación: {rec_e}")

            logging.info(f"🔂 Reintentando... ({retries}/{max_retries})")
            await asyncio.sleep(min(1.0 + 0.5 * retries, 2.0))

        except Exception as e:
            logging.error(f"🚨 Error inesperado al escribir en {char_uuid}: {e}")
            break

    logging.error(f"🚨 No se pudo escribir en la característica {char_uuid} después de {max_retries} intentos")
    return False

async def ensure_notify_ready(client, timeout: float = 3.0) -> None:
    """
    Verifica que las notificaciones están vivas mandando un ping no destructivo (COMMAND_ERROR_STATUS).
    Si no hay respuesta, reinicia notify una vez. Lanza si no se logra.
    """
    addr = getattr(client, "address", "")
    set_expected(addr, {0x29})
    events.error_status_processed_event.clear()


    # 1er intento: ping directo
    await write_characteristic(client, WRITE_CHAR_UUID, COMMAND_ERROR_STATUS)
    try:
        await asyncio.wait_for(events.error_status_processed_event.wait(), timeout=timeout)
        return
    except Exception:
        pass
    finally:
        # Ojo: lo limpiamos antes de rehacer notify para no gatear raro
        clear_expected(addr)

    # Reintento único: rehacer notify y volver a ping
    try:
        try:
            await client.stop_notify(NOTIFY_CHAR_UUID)
        except Exception:
            pass
        sid = new_session(client.address)
        await client.start_notify(
        NOTIFY_CHAR_UUID,
        lambda s, d, cli=client, session_id=sid: notification_handler(s, d, cli, session_id)
        )
        events.error_status_processed_event.clear()
        set_expected(addr, {0x29})
        try:
            await write_characteristic(client, WRITE_CHAR_UUID, COMMAND_ERROR_STATUS)
            await asyncio.wait_for(events.error_status_processed_event.wait(), timeout=timeout)
        finally:
            clear_expected(addr)
    except Exception as e:
        raise asyncio.TimeoutError(f"Notify no respondió al ping: {e}")


async def setConnect_device(client):
    addr = getattr(client, "address", "")
    current_time = datetime.datetime.now()

    year = current_time.year - 2000
    month = current_time.month
    day = current_time.day

    hours = current_time.hour
    minutes = current_time.minute
    seconds = current_time.second+1
    day_of_week = (current_time.weekday() + 1) % 7
# msg_connect_event
    set_expected(addr, {0xE1})
    events.msg_connect_event.clear()

    TIMECOMMAND = bytearray(
        [0x49, 0x44, 0xE1, year, month, day, hours, minutes, seconds, day_of_week])
    # await client.write_gatt_char(WRITE_CHAR_UUID, TIMECOMMAND)
    await write_characteristic(client, WRITE_CHAR_UUID, TIMECOMMAND)
    logging.info("----- 🗓️ Sincronizando Connexion")
    try:
        # await asyncio.wait_for(events.config_processed_event.wait(), timeout=6)
        await wait_event(events.msg_connect_event, 6)
    except asyncio.TimeoutError:
        logging.error("***** ⚠️ Timeout Solicitando configuración.")
        events.config_processed_event.set()  # Asegúrate de liberar el evento
    except Exception as e:
        logging.error(
            f"***** 🚨 Error inesperado Solicitando configuración: {e}")
        events.config_processed_event.set()  # Establece el evento en caso de error
    finally:
        clear_expected(addr)

async def setTime_device(client):
    current_time = datetime.datetime.now()

    year = current_time.year - 2000
    month = current_time.month
    day = current_time.day

    hours = current_time.hour
    minutes = current_time.minute
    seconds = current_time.second+1
    day_of_week = (current_time.weekday() + 1) % 7

    TIMECOMMAND = bytearray(
        [0x49, 0x44, 0xd6, year, month, day, hours, minutes, seconds, day_of_week])
    # await client.write_gatt_char(WRITE_CHAR_UUID, TIMECOMMAND)
    await write_characteristic(client, WRITE_CHAR_UUID, TIMECOMMAND)
    logging.info("----- 🗓️ Sincronizando fecha")


async def configure_device(client):
    addr = getattr(client, "address", "")
    set_expected(addr, {0xCA})
    events.config_processed_event.clear()
    # await client.write_gatt_char(WRITE_CHAR_UUID, COMMAND_CONFIG)
    await write_characteristic(client, WRITE_CHAR_UUID, COMMAND_CONFIG)
    logging.info("----- 🔧 Solicitando configuración")
    try:
        # await asyncio.wait_for(events.config_processed_event.wait(), timeout=6)
        await wait_event(events.config_processed_event, 6)
    except asyncio.TimeoutError:
        logging.error("***** ⚠️ Timeout Solicitando configuración.")
        events.config_processed_event.set()  # Asegúrate de liberar el evento
    except Exception as e:
        logging.error(
            f"***** 🚨 Error inesperado Solicitando configuración: {e}")
        events.config_processed_event.set()  # Establece el evento en caso de error
    finally:
        clear_expected(addr)

async def get_error_status_device(client):
    addr = getattr(client, "address", "")
    set_expected(addr, {0x29})
    events.error_status_processed_event.clear()
    # await client.write_gatt_char(WRITE_CHAR_UUID, COMMAND_CONFIG)
    await write_characteristic(client, WRITE_CHAR_UUID, COMMAND_ERROR_STATUS)
    logging.info("----- 🔧 Solicitando error status")
    try:
        # await asyncio.wait_for(events.error_status_processed_event.wait(), timeout=5)
        await wait_event(events.error_status_processed_event, 5)
    except asyncio.TimeoutError:
        logging.error("***** ⚠️ Timeout Solicitando error status.")
        events.error_status_processed_event.set()  # Asegúrate de liberar el evento
    except Exception as e:
        logging.error(
            f"***** 🚨 Error inesperado Solicitando error status: {e}")
        events.error_status_processed_event.set()  # Establece el evento en caso de error
    finally:
        clear_expected(addr)


async def update_alarm(client):
    events.alarm_processed_event.clear()
    addr = getattr(client, "address", "")
    set_expected(addr, {0x25})
    await write_characteristic(client, WRITE_CHAR_UUID, COMMAND_ALARM_STATE)
    logging.info("----- 🧾 Solicitando Estado de Alarmas")
    try:
        # await asyncio.wait_for(events.alarm_processed_event.wait(), timeout=10)
        await wait_event(events.alarm_processed_event, 10)
    except asyncio.TimeoutError:
        logging.error("***** ⚠️Timeout Solicitando Estado de Alarmas")
        events.alarm_processed_event.set()  # Asegúrate de liberar el evento
    except Exception as e:
        logging.error(
            f"***** 🚨 Error inesperado Solicitando Estado de Alarmas: {e}")
        events.alarm_processed_event.set()  # Establece el evento en caso de error
    finally:
        clear_expected(addr)


async def download_rms(client):
    global rms_download_active
    addr = getattr(client, "address", "")
    rms_download_active = True
    rms_idx_list.clear()

    events.rms_processed_event.clear()
    # === NEW: gating por fase RMS (DE y DD)
    set_expected(addr, {0xDE, 0xDD})

    await write_characteristic(client, WRITE_CHAR_UUID, COMMAND_RMS)
    logging.info("----- 🧾 Solicitando RMS guardadas")
    try:
        # await asyncio.wait_for(events.rms_processed_event.wait(), timeout=290)
        await wait_event(events.rms_processed_event, 400)
    except asyncio.TimeoutError:
        logging.error("***** ⚠️ Timeout Solicitando RMS guardadas")
        rms_download_active = False
        events.rms_processed_event.set()  # Asegúrate de liberar el evento
    except Exception as e:
        logging.error(
            f"***** 🚨 Error inesperado Solicitando RMS guardadas: {e}")
        rms_download_active = False
        events.rms_processed_event.set()  # Establece el evento en caso de error
    finally:
        rms_download_active = False
        clear_expected(addr)

async def mark_rms_downloaded(client, rms_idx_list):
    addr = getattr(client, "address", "")
    # === NEW: durante marcado esperamos 0xDB; luego 0x52 al guardar
    set_expected(addr, {0xDB})    
    # Procesa los índices de RMS
    if len(rms_idx_list) > 0:
        for idx_rms in rms_idx_list:
            events.rms_descargada_event.clear()
            byte_array = idx_rms.to_bytes(2, byteorder='little', signed=True)
            command_db = COMMAND_DB + byte_array

            # Marca cada medición RMS como descargada
            await write_characteristic(client, WRITE_CHAR_UUID, command_db)
            logging.info(
                f"----- ✅ Marcando medición RMS {idx_rms} como descargada")
            await asyncio.sleep(0.2)
            try:
                # await asyncio.wait_for(events.rms_descargada_event.wait(), timeout=TIME_TO_MARK_DOWNLOAD)
                await wait_event(events.rms_descargada_event, TIME_TO_MARK_DOWNLOAD)
            except asyncio.TimeoutError:
                logging.error(
                    f"***** ⚠️ Function:process_device | TimeOut Marcando medición RMS {idx_rms} como descargada")
                events.rms_descargada_event.set()
            except Exception as e:
                logging.error(
                    f"***** 🚨 Error inesperado Marcando medición RMS {idx_rms}: {e}")
                events.rms_descargada_event.set()  # Establece el evento en caso de error

        # Guarda el estado de RMS en memoria
        set_expected(addr, {0x52})  # esperar ACK SAVE
        # Guarda el estado de RMS en memoria
        await write_characteristic(client, WRITE_CHAR_UUID, COMMAND_SAVE_RMS_MEM)
        logging.info(f"----- 📌 Guardando el estado de RMS en memoria")

        try:
            # await asyncio.wait_for(events.rms_mem_save_event.wait(), timeout=TIME_TO_SAVE_MEM)
            await wait_event(events.rms_mem_save_event, TIME_TO_SAVE_MEM)
        except asyncio.TimeoutError:
            logging.error(
                f"***** ⚠️ Function:process_device | TimeOut guardando RMS status en memoria")
            events.rms_mem_save_event.set()
        except Exception as e:
            logging.error(
                f" 🚨 Error inesperado guardando RMS status en memoria: {e}")
            events.rms_mem_save_event.set()  # Establece el evento en caso de error
        finally:
            clear_expected(addr)   # === NEW: salir sin gating


async def download_measurements(client):
    addr = getattr(client, "address", "")
    # === NEW: en esta fase esperamos header 0xCC y luego ráfaga 0x60
    # set_expected(addr, {0xCC, 0x60})
    set_expected(addr, {0xCC})
    events.measurements_processed_event.clear()
    # await client.write_gatt_char(WRITE_CHAR_UUID, COMMAND_MED)
    await write_characteristic(client, WRITE_CHAR_UUID, COMMAND_MED)
    logging.info("----- 🧾 Solicitando mediciones guardadas")
    try:
        # await asyncio.wait_for(events.measurements_processed_event.wait(), timeout=TIMEOUT_DEVICE_PROCESS)
        await wait_event(events.measurements_processed_event, TIMEOUT_DEVICE_PROCESS)
    except asyncio.TimeoutError:
        logging.error("***** ⚠️ Timeout Solicitando mediciones guardadas.")
        events.measurements_processed_event.set()  # Asegúrate de liberar el evento
    except Exception as e:
        logging.error(
            f"***** 🚨 Error inesperado Solicitando mediciones guardadas: {e}")
        # Establece el evento en caso de error
        events.measurements_processed_event.set()
    finally:
        clear_expected(addr)  # === NEW


async def process_commands(client, device_address):
    global hayComandos, command_id_actual, cookies
    WIDNODE_CMD = []
    url = f"{API_URL}/command/widnode/{device_address}"
    async with httpx.AsyncClient(cookies=cookies) as clientWeb:
        response = await clientWeb.get(url)

        if response.status_code == 200:
            data = response.json()
            # Convierte la respuesta en una lista de listas
            WIDNODE_CMD = [[item['command_id'], item['widnode_id'],
                            item['command'], item['date'], item['ejecutado']] for item in data]
        else:
            logging.error(
                f"🚨 Failed to fetch commands for device {device_address}: {response.status_code}")

    # logging.info(f"WIDNODE ID COMMAND: {WIDNODE_CMD}")

    for command in WIDNODE_CMD:
        # print(f"commando por ejecutar {command}")
        command_id, widnode_id, command_text, date, ejecutado = command

        # print(f"command_text por ejecutar {command_text}")
        logging.info(f"----- 🧾 Enviando comando agendado id: {command_id}")
        command_id_actual = command_id

        # if command_text == 'SURd':
        hayComandos = True
        events.cmd_processed_event.clear()
        # print(base64.b64decode(command_text))
        # await client.write_gatt_char(WRITE_CHAR_UUID, base64.b64decode(command_text))
        await write_characteristic(client, WRITE_CHAR_UUID, base64.b64decode(command_text))
        try:
            # await asyncio.wait_for(events.cmd_processed_event.wait(), timeout=TIMEOUT_DEVICE_PROCESS)
            await wait_event(events.cmd_processed_event, TIMEOUT_DEVICE_PROCESS)
        except asyncio.TimeoutError:
            logging.error("***** ⚠️ Timeout Enviando comando agendado.")
            events.cmd_processed_event.set()  # Asegúrate de liberar el evento
        except Exception as e:
            logging.error(f"🚨 Error inesperado Enviando comando agendado: {e}")
            events.cmd_processed_event.set()  # Establece el evento en caso de error

        hayComandos = False

async def should_restart_adapter() -> bool:
    """
    Devuelve True si el adaptador parece no saludable:
    - bluetoothctl Powered != yes
    - 2 escaneos cortos consecutivos (3s) sin ver ni un solo anuncio
    """
    try:
        from utils import is_bluetooth_powered
        if not is_bluetooth_powered(logging):
            logging.warning("⚠️ Adapter no está Powered en bluetoothctl show.")
            return True
    except Exception as e:
        logging.debug(f"No se pudo verificar Powered: {e}")

    try:
        r1 = await BleakScanner.discover(timeout=3.0)
        if len(r1) > 0:
            return False
        r2 = await BleakScanner.discover(timeout=3.0)
        if len(r2) > 0:
            return False
        logging.warning("⚠️ Escaneo vacío (2x3s); se sugiere reiniciar hci0.")
        return True
    except Exception as e:
        logging.debug(f"Error en discover para health-check: {e}")
        # si ni siquiera podemos escanear, reiniciar
        return True


# HANDLER BLE NOTIFICATIONS
def notification_handler(sender, data, client, sid=None):
    global buffer, msg_count,rms_download_active,cantidad_byte_med_ext,node_busy

    addr = getattr(client, "address", None)
    # 1) Epoch/Session guard: descartar frames atrasados o de otra sesión
    if addr:
        current_sid = get_session_id(addr)
        if sid is not None and sid != current_sid:
            logging.debug(f"[{addr}] 𝘿𝙍𝙊𝙋 late frame (epoch mismatch: got={sid}, curr={current_sid})")
            return
        
    # logging.info(data)
    command_type = data[6]

    # 2) Gating por fase: si hay set esperado y no está el opcode, se descarta
    expected = expected_cmds_by_addr.get(addr or "", set())
    if expected and (command_type not in expected):
        logging.debug(f"[{addr}] 𝘿𝙍𝙊𝙋 opcode 0x{command_type:02X} no esperado en fase (esperado={sorted(expected)})")
        return
    
    # 3) Dispatch con tracking de tasks
    if command_type == 0xE1:    # SOLICITA LA CONFIGURACION
        # asyncio.create_task(process_ca_response(data, client))
        track_task(addr, process_e1_response(data, client))
    if command_type == 0xCA:    # SOLICITA LA CONFIGURACION
        # asyncio.create_task(process_ca_response(data, client))
        track_task(addr, process_ca_response(data, client))
    elif command_type == 0x29:    # SOLICITA ERROR STATUS
        # asyncio.create_task(process_29_response(data, client))
        track_task(addr, process_29_response(data, client))
    elif command_type == 0x25:    # SOLICITA ALARM STATE
        # asyncio.create_task(process_25_response(data, client))
        track_task(addr, process_25_response(data, client))
    elif command_type == 0xDE:   # MENSAJE CON CANTIDAD DE MENSAJES QUE SE VAN A RECIBIR
        process_de_response(data, client)
    elif command_type == 0xDD:   # RECEPCION DE MEDICION RMS
        if rms_download_active:
            # asyncio.create_task(process_dd_response_locked(data, client))
            track_task(addr, process_dd_response_locked(data, client))
        else:
            logging.debug(f"[{addr}] 𝘿𝙍𝙊𝙋 0xDD tardío (rms_download_active=False)")
    elif command_type == 0xCC:  # RECEPCION DE HEADER MEDICIONES
        # asyncio.create_task(process_cc_response(data, client))
        track_task(addr, process_cc_response(data, client))
    elif command_type == 0xDB:   # RMS marcado como descargado
        events.rms_descargada_event.set()
    elif command_type == 0xCF:   # MED marcado como descargado
        events.med_descargada_event.set()
    elif command_type == 0x52:
        events.rms_mem_save_event.set()
    elif command_type == 0x60:  # download medicion extendida
        # OJO: NO crear 1 task por notificación (te destruye el throughput)
        enqueue_ext60_packet(addr, data, client)
async def process_60_response_locked(data, client):
    async with globals()["measurement_ext_lock"]:
        await process_60_response(data, client)
async def process_dd_response_locked(data, client):
    async with globals()["rms_lock"]:
        await process_dd_response(data, client)

def parse_diagnostico_ble(msg: str) -> dict:
    errores = {}
    try:
        for parte in msg.split(','):
            if ':' in parte:
                clave, valor = parte.split(':')
                errores[clave] = int(valor)
    except Exception as e:
        logging.error(f"Error al parsear diagnóstico: {e}")
    return errores
# TRATAMIENTOD DE RESPUESTA DE ERROR STATUS
async def process_29_response(data, client):
    global cookies
    mensaje_bytes = data[7:]  # Saltás los primeros 7 bytes (ID, encabezado, etc.)
    mensaje = mensaje_bytes.decode('utf-8').strip()
    errores = parse_diagnostico_ble(mensaje)  # Esto devuelve un dict
    widnode_id = client.address
    urlErrorStatus = f"{API_URL}/widnodes/error_status/{widnode_id}"
    logging.info(f"----- 🧾 Enviando error status | mensaje: {errores}")
    try:
        async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientconfig:
            response = await clientconfig.put(urlErrorStatus, json=errores)
            # Manejar la respuesta
            # if response.status_code == 201 or response.status_code == 200:
            #     logging.info(f"----- ✅ wIdnode error status actualizada")
            if response.status_code == 400:
                logging.error(f"***** 🚨 Error Widnode update Bad Request: {response.json()}")
            elif response.status_code == 404:
                logging.error(f"***** 🚨 Error Widnode not found.")
            elif response.status_code != 200 and response.status_code !=201:
                logging.error(f"***** 🚨 Error actualizando error status {response.status_code}: {response.json()}")
    except Exception as e:
        print("An error occurred:", str(e))

    events.error_status_processed_event.set()

def build_alarm_datetime(year2k, month, day, hour, minute, second):
    # Normaliza segundo (a veces 60)
    second = min(int(second), 59)
    y = 2000 + int(year2k)
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
        return None
    try:
        return datetime.datetime(y, int(month), int(day), int(hour), int(minute), second)
    except ValueError:
        return None
    
async def process_e1_response(data, client):
    global cookies,rssi_by_identity
    logging.info(f"----- 📟 Procesando configuración recibida (E1)")
    logging.info(f"data length: {len(data)}")
    data_byte = data
    ##### CONFIGURATION MESSAGE PARSING #####
    sample_rate = data_byte[8]
    time_to_mon = int.from_bytes(data_byte[9:11], byteorder='little')
    time_to_med = int.from_bytes(data_byte[11:13], byteorder='little')
    state = data_byte[13]
    alarm_x = struct.unpack('<f', data_byte[14:18])[0]
    alarm_y = struct.unpack('<f', data_byte[18:22])[0]
    alarm_z = struct.unpack('<f', data_byte[22:26])[0]
    alarm_temp = struct.unpack('<f', data_byte[26:30])[0]
    alarm_x_vel = struct.unpack('<f', data_byte[30:34])[0]
    alarm_y_vel = struct.unpack('<f', data_byte[34:38])[0]
    alarm_z_vel = struct.unpack('<f', data_byte[38:42])[0]
    alarm_mode = data_byte[42]
    filtro = data_byte[43]
    battlevel = struct.unpack('<f', data_byte[44:48])[0]
    
   # logging.info(f"RSSI: {rssi_device_connected}")

    # Convertir las listas de Python a arrays de PostgreSQL
    widnode_id = client.address

    # logging.info(f"----- ✅  Actualizando configuracion widnode:{widnode_id}")
    # logging.info(f"----- sample: {sample_rate} | time_to_mon:{time_to_mon} | time_to_med: {time_to_med} | state:{state} | alarm_x:{alarm_x } | alarm_y:{alarm_z } | alarm_x:{alarm_z } | alarm_temp:{alarm_temp } | alarm_x_vel:{alarm_x_vel} | alarm_y_vel: {alarm_y_vel} | alarm_z_vel: {alarm_z_vel}")
    rssi = rssi_by_identity.get(widnode_id, -1)
    # print(f"RSSI: {rssi}")
    # print(f"widnode ID: {client.address}")
    try:
        # Insertar los datos en la tabla measurements
        data = {
            "sample_rate": sample_rate,
            "time_to_mon": time_to_mon,
            "time_to_med": time_to_med,
            "alarm_x": alarm_x,
            "alarm_y": alarm_y,
            "alarm_z": alarm_z,
            "alarm_temp": alarm_temp,
            "state": state,
            "alarm_x_vel": alarm_x_vel,
            "alarm_y_vel": alarm_y_vel,
            "alarm_z_vel": alarm_z_vel,
            "rssi": rssi,
            "alarm_mode": alarm_mode,
            "battlevel": battlevel,
        }

        urlconfig = f"{API_URL}/widnodes/{widnode_id}?update_type=complementary"

        async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientconfig:
            response = await clientconfig.put(urlconfig, json=data)
            # Manejar la respuesta
            # if response.status_code == 200:
                # logging.info(f"----- ✅ wIdnode configuracion actualizada")
            if response.status_code == 400:
                logging.error(f"***** 🚨 Error Widnode update Bad Request: {response.json()}")
            elif response.status_code == 404:
                logging.error(f"***** 🚨 Error Widnode not found.")
            elif response.status_code != 200:
                logging.error(f"***** 🚨 Error actualizando configuracion {response.status_code}: {response.json()}")

    except Exception as e:
        print("An error occurred:", str(e))        

    ### ERROR MESSAGE PARSING ####

    mensaje_error_bytes = data_byte[48:48+20]  # exactamente 20 bytes
    errores_tuple = struct.unpack('<10H', mensaje_error_bytes)
    mensaje_error_str = ",".join(f"E{i}:{valor}" for i, valor in enumerate(errores_tuple))

    urlErrorStatus = f"{API_URL}/widnodes/error_status/{widnode_id}"
    logging.info(f"----- 🧾 Enviando error status | mensaje: {mensaje_error_str}")
    try:
        async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientconfig:
            response = await clientconfig.put(urlErrorStatus, json=mensaje_error_str)
            # Manejar la respuesta
            # if response.status_code == 201 or response.status_code == 200:
            #     logging.info(f"----- ✅ wIdnode error status actualizada")
            if response.status_code == 400:
                logging.error(f"***** 🚨 Error Widnode update Bad Request: {response.json()}")
            elif response.status_code == 404:
                logging.error(f"***** 🚨 Error Widnode not found.")
            elif response.status_code != 200 and response.status_code !=201:
                logging.error(f"***** 🚨 Error actualizando error status {response.status_code}: {response.json()}")
    except Exception as e:
        print("An error occurred:", str(e))

    ### ALARM UPDATE MESSAGE PARSING ####
    alarm_rms = data_byte[68]
    alarm_vel = data_byte[69]
    alarm_year = data_byte[70]
    alarm_month = data_byte[71]
    alarm_day = data_byte[72]
    alarm_hour = data_byte[73]
    alarm_minute = data_byte[74]
    alarm_second = data_byte[75]
    # logging.info(f'tamaño buffer alarma {len(data)}')
    
    if len(data_byte)>80: 
        alarm_med_x=struct.unpack('<f', data_byte[76:80])[0]
        alarm_med_y=struct.unpack('<f', data_byte[80:84])[0]
        alarm_med_z=struct.unpack('<f', data_byte[84:88])[0]
        alarm_med_temp=struct.unpack('<f', data_byte[88:92])[0]
        alarm_med_x_vel=struct.unpack('<f', data_byte[92:96])[0]
        alarm_med_y_vel=struct.unpack('<f', data_byte[96:100])[0]
        alarm_med_z_vel=struct.unpack('<f', data_byte[100:104])[0]
    else:
        alarm_med_x = 0
        alarm_med_y = 0
        alarm_med_z = 0
        alarm_med_temp = 0
        alarm_med_x_vel = 0
        alarm_med_y_vel = 0
        alarm_med_z_vel = 0
    
    # fecha_alarm = datetime.datetime(alarm_year+2000, alarm_month, alarm_day, alarm_hour, alarm_minute, alarm_second)
    fecha_alarm = build_alarm_datetime(alarm_year, alarm_month, alarm_day,alarm_hour, alarm_minute, alarm_second)
    if fecha_alarm is None and (alarm_rms != 0 or alarm_vel != 0):
        logging.error(
            f"[{widnode_id}] Fecha alarma inválida: "
            f"y={alarm_year} m={alarm_month} d={alarm_day} "
            f"h={alarm_hour} min={alarm_minute} s={alarm_second} "
        )
    else:
        widnode_id = client.address
        # logging.info(f"----- ✅  Actualizando alarmas widnode:{widnode_id}")
        data_alarm = {
            "alarm_rms": alarm_rms,
            "alarm_vel": alarm_vel,
            "fecha": fecha_alarm.strftime('%Y-%m-%d %H:%M:%S'),
            "med_x": alarm_med_x,
            "med_y": alarm_med_y,
            "med_z": alarm_med_z,
            "med_temp": alarm_med_temp,
            "med_x_vel": alarm_med_x_vel,
            "med_y_vel": alarm_med_y_vel,
            "med_z_vel": alarm_med_z_vel
        }

        idx = widnode_modbus_index.get(client.address, 0)
        if (idx > 0 and (alarm_rms>0 or alarm_vel>0)):
            # normaliza year si te llega en dos dígitos
            y = alarm_year if alarm_year >= 2000 else (2000 + alarm_year)
            # proteger segundo=60
            ts_in = int(datetime.datetime(y, alarm_month, alarm_day, alarm_hour, alarm_minute, min(alarm_second, 59)).timestamp())
            merged = merge_alarma(alarm_rms, alarm_vel)
            vals = [
                merged,
            ]

            if modbus_srv is not None:
                try:
                    updated = await modbus_srv.update_alarm_block(idx, ts_in, vals)
                    if updated:
                        logging.info(f"[MODBUS_SERVER] actualizado idx={idx} ({widnode_id})")
                    else:
                        logging.info(f"[MODBUS_SERVER] omitido idx={idx} (timestamp no más nuevo)")
                except Exception as e:
                    logging.error(f"[MODBUS_SERVER] error al actualizar idx={idx}: {e}")

        urlalarm = f"{API_URL}/alarm/{widnode_id}"
        # logging.info(urlalarm)
        if (alarm_rms != 0 or alarm_vel != 0) and fecha_alarm is not None:
            try:
                async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientconfig:
                    response = await clientconfig.post(urlalarm, json=data_alarm)
                    # Manejar la respuesta
                    # if response.status_code == 201 or response.status_code == 200:
                    #     logging.info(f"----- ✅ wIdnode alarm actualizada")
                    if response.status_code == 400:
                        logging.error(f"***** 🚨 Error Widnode update Bad Request: {response.json()}")
                    elif response.status_code == 404:
                        logging.error(f"***** 🚨 Error Widnode not found.")
                    elif response.status_code == 409:
                        logging.error(f"***** ⚠️ Alarma ya registrada .")
                    elif response.status_code != 200 and response.status_code !=201:
                        logging.error(f"***** 🚨 Error actualizando alarma {response.status_code}: {response.json()}")
            except Exception as e:
                print("An error occurred:", str(e))
    
    events.msg_connect_event.set()

# TRATAMIENTO DE RESPUESTA DE CONFIGURACION RECIBIDA
async def process_ca_response(data, client):
    global cookies,rssi_by_identity
    sample_rate = data[8]
    time_to_mon = int.from_bytes(data[9:11], byteorder='little')
    time_to_med = int.from_bytes(data[11:13], byteorder='little')
    state = data[13]
    alarm_x = struct.unpack('<f', data[14:18])[0]
    alarm_y = struct.unpack('<f', data[18:22])[0]
    alarm_z = struct.unpack('<f', data[22:26])[0]
    alarm_temp = struct.unpack('<f', data[26:30])[0]
    alarm_x_vel = struct.unpack('<f', data[30:34])[0]
    alarm_y_vel = struct.unpack('<f', data[34:38])[0]
    alarm_z_vel = struct.unpack('<f', data[38:42])[0]
    alarm_mode = data[42]
    filtro = data[43]
    if len(data) > 48:
        battlevel = struct.unpack('<f', data[44:48])[0]
    else:
        battlevel = 0

    # logging.info(f"RSSI: {rssi_device_connected}")

    # Convertir las listas de Python a arrays de PostgreSQL
    widnode_id = client.address

    # logging.info(f"----- ✅  Actualizando configuracion widnode:{widnode_id}")
    # logging.info(f"----- sample: {sample_rate} | time_to_mon:{time_to_mon} | time_to_med: {time_to_med} | state:{state} | alarm_x:{alarm_x } | alarm_y:{alarm_z } | alarm_x:{alarm_z } | alarm_temp:{alarm_temp } | alarm_x_vel:{alarm_x_vel} | alarm_y_vel: {alarm_y_vel} | alarm_z_vel: {alarm_z_vel}")
    rssi = rssi_by_identity.get(widnode_id, -1)
    # print(f"RSSI: {rssi}")
    # print(f"widnode ID: {client.address}")
    try:
        # Insertar los datos en la tabla measurements
        data = {
            "sample_rate": sample_rate,
            "time_to_mon": time_to_mon,
            "time_to_med": time_to_med,
            "alarm_x": alarm_x,
            "alarm_y": alarm_y,
            "alarm_z": alarm_z,
            "alarm_temp": alarm_temp,
            "state": state,
            "alarm_x_vel": alarm_x_vel,
            "alarm_y_vel": alarm_y_vel,
            "alarm_z_vel": alarm_z_vel,
            "rssi": rssi,
            "alarm_mode": alarm_mode,
            "battlevel": battlevel,
        }

        urlconfig = f"{API_URL}/widnodes/{widnode_id}?update_type=complementary"

        async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientconfig:
            response = await clientconfig.put(urlconfig, json=data)
            # Manejar la respuesta
            # if response.status_code == 200:
                # logging.info(f"----- ✅ wIdnode configuracion actualizada")
            if response.status_code == 400:
                logging.error(f"***** 🚨 Error Widnode update Bad Request: {response.json()}")
            elif response.status_code == 404:
                logging.error(f"***** 🚨 Error Widnode not found.")
            elif response.status_code != 200:
                logging.error(f"***** 🚨 Error actualizando configuracion {response.status_code}: {response.json()}")

    except Exception as e:
        print("An error occurred:", str(e))

    events.config_processed_event.set()

def clean_json_floats(obj):
    """Convierte NaN o infinitos en None para compatibilidad JSON"""
    return {
        k: (None if isinstance(v, float) and (
            math.isnan(v) or math.isinf(v)) else v)
        for k, v in obj.items()
    }

# TRATAMIENTO DE RESPUESTA DE ALARMA STATE
async def process_25_response(data, client):
    global cookies
    try:
        alarm_rms = data[7]
        alarm_vel = data[8]
        alarm_year = data[9]
        alarm_month = data[10]
        alarm_day = data[11]
        alarm_hour = data[12]
        alarm_minute = data[13]
        alarm_second = data[14]
        # logging.info(f'tamaño buffer alarma {len(data)}')
        
        if len(data)>16: 
            alarm_med_x=struct.unpack('<f', data[15:19])[0]
            alarm_med_y=struct.unpack('<f', data[19:23])[0]
            alarm_med_z=struct.unpack('<f', data[23:27])[0]
            alarm_med_temp=struct.unpack('<f', data[27:31])[0]
            alarm_med_x_vel=struct.unpack('<f', data[31:35])[0]
            alarm_med_y_vel=struct.unpack('<f', data[35:39])[0]
            alarm_med_z_vel=struct.unpack('<f', data[39:43])[0]
        else:
            alarm_med_x = 0
            alarm_med_y = 0
            alarm_med_z = 0
            alarm_med_temp = 0
            alarm_med_x_vel = 0
            alarm_med_y_vel = 0
            alarm_med_z_vel = 0
        
        fecha_alarm = datetime.datetime(alarm_year+2000, alarm_month, alarm_day, alarm_hour, alarm_minute, alarm_second)
        widnode_id = client.address
        # logging.info(f"----- ✅  Actualizando alarmas widnode:{widnode_id}")
        data_alarm = {
            "alarm_rms": alarm_rms,
            "alarm_vel": alarm_vel,
            "fecha": fecha_alarm.strftime('%Y-%m-%d %H:%M:%S'),
            "med_x": alarm_med_x,
            "med_y": alarm_med_y,
            "med_z": alarm_med_z,
            "med_temp": alarm_med_temp,
            "med_x_vel": alarm_med_x_vel,
            "med_y_vel": alarm_med_y_vel,
            "med_z_vel": alarm_med_z_vel
        }

        idx = widnode_modbus_index.get(client.address, 0)
        if (idx > 0 and (alarm_rms>0 or alarm_vel>0)):
            # normaliza year si te llega en dos dígitos
            y = alarm_year if alarm_year >= 2000 else (2000 + alarm_year)
            # proteger segundo=60
            ts_in = int(datetime.datetime(y, alarm_month, alarm_day, alarm_hour, alarm_minute, min(alarm_second, 59)).timestamp())
            merged = merge_alarma(alarm_rms, alarm_vel)
            vals = [
              merged,
            ]

            if modbus_srv is not None:
                try:
                    updated = await modbus_srv.update_alarm_block(idx, ts_in, vals)
                    if updated:
                        logging.info(f"[MODBUS_SERVER] actualizado idx={idx} ({widnode_id})")
                    else:
                        logging.info(f"[MODBUS_SERVER] omitido idx={idx} (timestamp no más nuevo)")
                except Exception as e:
                    logging.error(f"[MODBUS_SERVER] error al actualizar idx={idx}: {e}")

                    
        # logging.info(data_alarm)
        urlalarm = f"{API_URL}/alarm/{widnode_id}"
        # logging.info(urlalarm)
        if (alarm_rms == 0 and alarm_vel == 0):
            events.alarm_processed_event.set()
            return
        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientconfig:
                response = await clientconfig.post(urlalarm, json=data_alarm)
                # Manejar la respuesta
                # if response.status_code == 201 or response.status_code == 200:
                #     logging.info(f"----- ✅ wIdnode alarm actualizada")
                if response.status_code == 400:
                    logging.error(f"***** 🚨 Error Widnode update Bad Request: {response.json()}")
                elif response.status_code == 404:
                    logging.error(f"***** 🚨 Error Widnode not found.")
                elif response.status_code == 409:
                    logging.error(f"***** ⚠️ Alarma ya registrada .")
                elif response.status_code != 200 and response.status_code !=201:
                    logging.error(f"***** 🚨 Error actualizando alarma {response.status_code}: {response.json()}")
        except Exception as e:
            print("An error occurred:", str(e))

    except ValueError:
        logging.error(f'***** 🚨 📅 Function:process_25_response |  Formato de fecha inccorecto widnode: {client.address} ')
        logging.error(f"***** 🔢 Data: {binascii.hexlify(data).decode('utf-8')}")
        
    events.alarm_processed_event.set()

# TRATAMIENTO DE RESPUESTA DE CANTIDAD DE MENSAJES A RECIBIR -RMS
def process_de_response(data, client):
    global cantidadMensajesRMS, rms_idx_list
    rms_idx_list = []
    cantidadMensajesRMS = int.from_bytes(data[7:9], byteorder='little')
    # print(f'cantidad de mensajes a recibir {cantidadMensajes}')
    if cantidadMensajesRMS > 0:
        events.rms_processed_event.clear()
    else:
        rms_idx_list = []
        events.rms_processed_event.set()
# TRATAMIENTO DE RESPUESTA DE RMS RECIBIDA
async def process_dd_response(data, client):
    global cantidadMensajesRMS, cookies,widnode_modbus_index,modbus_pub,rms_download_active
    
    index_rms = int.from_bytes(data[7:9], byteorder='little')
    index_rms2 = int.from_bytes(data[9:11], byteorder='little')
    # index_rms = int.from_bytes(data[3:5], byteorder='little')

    # print(f"index_rms 1: {index_rms} | index_rms 2: {index_rms2}")

    widnode_id = client.address
    year = data[11]
    month = data[12]
    day = data[13]
    hour = data[14]
    minute = data[15]
    second = data[16]

    if second > 59:
        second = 59

    rmsx = struct.unpack('<f', data[17:21])[0]
    rmsy = struct.unpack('<f', data[21:25])[0]
    rmsz = struct.unpack('<f', data[25:29])[0]
    temp = struct.unpack('<f', data[29:33])[0]
    rmsx_vel = struct.unpack('<f', data[33:37])[0]
    rmsy_vel = struct.unpack('<f', data[37:41])[0]
    rmsz_vel = struct.unpack('<f', data[41:45])[0]

    sample_rate = data[45]
    alarma = data[46]
    alarma_vel = data[47]

    mX = 0
    rX = 0
    skX = 0
    kuX = 0
    pX = 0
    cfX = 0
    sfX = 0
    imfX = 0

    mY = 0
    rY = 0
    skY = 0
    kuY = 0
    pY = 0
    cfY = 0
    sfY = 0
    imfY = 0

    mZ = 0
    rZ = 0
    skZ = 0
    kuZ = 0
    pZ = 0
    cfZ = 0
    sfZ = 0
    imfZ = 0

    if len(data) >= 144:
        mX = struct.unpack('<f', data[48:52])[0]
        rX = struct.unpack('<f', data[52:56])[0]
        skX = struct.unpack('<f', data[56:60])[0]
        kuX = struct.unpack('<f', data[60:64])[0]
        pX = struct.unpack('<f', data[64:68])[0]
        cfX = struct.unpack('<f', data[68:72])[0]
        sfX = struct.unpack('<f', data[72:76])[0]
        imfX = struct.unpack('<f', data[76:80])[0]

        mY = struct.unpack('<f', data[80:84])[0]
        rY = struct.unpack('<f', data[84:88])[0]
        skY = struct.unpack('<f', data[88:92])[0]
        kuY = struct.unpack('<f', data[92:96])[0]
        pY = struct.unpack('<f', data[96:100])[0]
        cfY = struct.unpack('<f', data[100:104])[0]
        sfY = struct.unpack('<f', data[104:108])[0]
        imfY = struct.unpack('<f', data[108:112])[0]

        mZ = struct.unpack('<f', data[112:116])[0]
        rZ = struct.unpack('<f', data[116:120])[0]
        skZ = struct.unpack('<f', data[120:124])[0]
        kuZ = struct.unpack('<f', data[124:128])[0]
        pZ = struct.unpack('<f', data[128:132])[0]
        cfZ = struct.unpack('<f', data[132:136])[0]
        sfZ = struct.unpack('<f', data[136:140])[0]
        imfZ = struct.unpack('<f', data[140:144])[0]

        try:
            fecha = datetime.datetime(
                year + 2000, month, day, hour, minute, second)
        except Exception as e:
            logging.error(f'🚨 Error al construir la fecha: {e} | index:  {index_rms2}')
            logging.error(
                f"year = {year} | month = {month} | day = {day} | hour = {hour} | minute = {minute} | second = {second}")
            logging.error(
                f"***** 🔢 Data: {binascii.hexlify(data).decode('utf-8')}")
            cantidadMensajesRMS -= 1
            return  # Salir de la función
        # logging.info(f"----- 📥 Procesando RPM para fecha: {fecha}")
        if is_tach_enabled():
            rpm,fecha_rpm =  get_rpm_nearest(fecha)
            logging.info(f"----- 🔢 RPM obtenidas: {rpm} | fecha : {fecha_rpm}")
        else:
            rpm = 0
            fecha_rpm = None
            
        # logging.info(f"----- 🔢 RPM obtenidas: {rpm} | fecha: {fecha_rpm}")
        data_monitoring = {
            "widnode_id": widnode_id,
            "fecha": fecha.strftime('%Y-%m-%d %H:%M:%S'),
            "rms_x": rmsx,
            "rms_y": rmsy,
            "rms_z": rmsz,
            "temp": temp,
            "sample_rate": sample_rate,
            "alarma": alarma,
            "rms_x_vel": rmsx_vel,
            "rms_y_vel": rmsy_vel,
            "rms_z_vel": rmsz_vel,
            "alarma_vel": alarma_vel,
            "media_x": mX,
            "rms_axis_x": rX,
            "skew_x": skX,
            "kurtosis_x": kuX,
            "ptp_x": pX,
            "crest_factor_x": cfX,
            "shape_factor_x": sfX,
            "impulse_factor_x": imfX,
            "media_y": mY,
            "rms_axis_y": rY,
            "skew_y": skY,
            "kurtosis_y": kuY,
            "ptp_y": pY,
            "crest_factor_y": cfY,
            "shape_factor_y": sfY,
            "impulse_factor_y": imfY,
            "media_z": mZ,
            "rms_axis_z": rZ,
            "skew_z": skZ,
            "kurtosis_z": kuZ,
            "ptp_z": pZ,
            "crest_factor_z": cfZ,
            "shape_factor_z": sfZ,
            "impulse_factor_z": imfZ,
            "rpm": rpm,
            "fecha_rpm": fecha_rpm.strftime('%Y-%m-%d %H:%M:%S') if fecha_rpm else None,
        }
        data_monitoring = clean_json_floats(data_monitoring)
        # print(data)
        urlmonitoring = f"{API_URL}/monitoring/"
        try:
            async with httpx.AsyncClient(cookies=cookies, timeout=20.0) as clientMon:
                response = await clientMon.post(urlmonitoring, json=data_monitoring)
                if response.status_code == 201:
                    rms_idx_list.append(index_rms2)
                    logging.info(
                        f"----- ✅ Medicion de RMS insertada indice: {index_rms2} fecha: {fecha}")
                else:
                    logging.error(
                        f"🚨 Error inserting monitoring data: {response.status_code} - {response.text}")
        except httpx.ConnectTimeout:
            logging.error(
                "Connection timed out. The server may be down or unreachable.")
        except httpx.RequestError as e:
            logging.error(
                f"🚨 Error de red en la petición a {e.request.url!r}: {e}")
        except Exception as e:
            logging.error(
                f"🚨 Error inesperado al insertar RMS: {type(e).__name__}: {e}")
            logging.error(
                f"Payload: {json.dumps(data_monitoring, default=str)}")


    # --- Publicación a Modbus (si config habilitada y hay idx) ---
    if modbus_srv is not None:
        widnode_id = client.address
        idx = widnode_modbus_index.get(widnode_id, 0)
        if idx > 0:
            # normaliza year si te llega en dos dígitos
            y = year if year >= 2000 else (2000 + year)
            # proteger segundo=60
            ts_in = int(datetime.datetime(y, month, day, hour, minute, min(second, 59)).timestamp())

            vals = {
                "rmsx": rmsx, "rmsy": rmsy, "rmsz": rmsz,
                "temp": temp,
                "rmsx_vel": rmsx_vel, "rmsy_vel": rmsy_vel, "rmsz_vel": rmsz_vel,
            }
            try:
                updated = await modbus_srv.update_block(idx, ts_in, vals)
                if updated:
                    logging.info(f"[MODBUS_SERVER] actualizado idx={idx} ({widnode_id})")
                else:
                    logging.info(f"[MODBUS_SERVER] omitido idx={idx} (timestamp no más nuevo)")
            except Exception as e:
                logging.error(f"[MODBUS_SERVER] error al actualizar idx={idx}: {e}")
    # --- fin publicación Modbus ---

    cantidadMensajesRMS -= 1
    if cantidadMensajesRMS <= 0:
        rms_download_active = False          # ← FALTA
        events.rms_processed_event.set()

# RECIBE EL ARRAY DE ESTADO DE MEDICIONES
async def process_cc_response(data, client):
    uint16_value = int.from_bytes(data[7:9], byteorder='little')
    array_50 = data[9:209]
    # logging.info(array_50)
    logging.info(f"----- 📥 Recibido estado de mediciones almacenadas.")
    events.med_array_event.clear()
    # print(f"Recibido CC. Uint16: {uint16_value}, Array: {array_50}")
    
    await send_commands_ce(array_50, client)
    try:
        # await asyncio.wait_for(events.med_array_event.wait(), timeout=TIMEOUT_DEVICE_PROCESS)
        await wait_event(events.med_array_event, TIMEOUT_DEVICE_PROCESS)
    except asyncio.TimeoutError:
        logging.error("*****⚠️ Timeout Solicitando estado de Mediciones")
        events.med_array_event.set()  # Asegúrate de liberar el evento
    except Exception as e:
        logging.error(f"🚨 Error inesperado Solicitando estado de Mediciones: {e}")
        events.med_array_event.set()  # Establece el evento en caso de error

def reset_measurement_state():
    global fecha_extendida, cantidad_byte_med_ext, medicion_descargada, sample_rate
    global temp, alarma, alarma_vel, error_en_fecha
    fecha_extendida = None
    cantidad_byte_med_ext = 0
    medicion_descargada = False
    sample_rate = 0
    temp = None
    alarma = 0
    alarma_vel = 0
    error_en_fecha = False

# PROCESA EL ARRAY DE ESTADO DE MEDICIONES Y ENVIA LOS COMANDOS PARA DESCARGARLAS
async def send_commands_ce(array_50, client):
    global msg_count, idx_actual, medicion_descargada, error_en_fecha,node_busy,MAX_WAIT_TIME_BUSY

    # Resumen y short-circuit si no hay nada que pedir
    ones, zeros, ranges_str, ones_idx_dev = summarize_measure_array(array_50, base_offset=2, preview=200)
    logging.info("----- 📊 Resumen | ones=%d | zeros=%d | activos=%s", ones, zeros, ranges_str)
    logging.info(f"----- 📊 Índices activos: {ones_idx_dev}" )
    if ones == 0:
        logging.info("----- ✅ No hay mediciones pendientes. No se envían comandos CE.")
        events.med_array_event.set()
        events.measurements_processed_event.set()
        return

    try:
        # for idx, value in enumerate(array_50):

        #     if value == 1:
        for idx in ones_idx_dev:
            msg_count = 0
            events.msg_received_event.clear()
            command_ce = COMMAND_CE + bytearray([idx + 2])
            # logging.info(f"comando enviado {command_ce}")
            reset_measurement_state()

            idx_actual = idx + 2
            addr = getattr(client, "address", "")
            # set_expected(addr, {0xCC, 0x60})
            set_expected(addr, {0x60})
            try:
                # await client.write_gatt_char(WRITE_CHAR_UUID, command_ce)
                await write_characteristic(client, WRITE_CHAR_UUID, command_ce)
                logging.info(
                    f"----- 🧾 Solicitud de descarga de medicion índice {idx + 2}")
            except BleakError as e:
                logging.error(
                    f"***** 🚨 Error al solicitar medicion índice {idx + 2}: {e}")

            try:
                # await asyncio.wait_for(events.msg_received_event.wait(), timeout=TIMEOUT)
                await wait_event(events.msg_received_event, TIMEOUT)
                # logging.info(f"----- 🧾 SALIENDO DEL WAIT  msg_received_event índice {idx + 2}")
                
                if medicion_descargada:

                    events.med_descargada_event.clear()
                    addr = getattr(client, "address", "")
                    set_expected(addr, {0xCF}) 
                    command_cf = COMMAND_CF + bytearray([idx_actual])
                    await write_characteristic(client, WRITE_CHAR_UUID, command_cf)
                    logging.info(f"----- ✅ Marcando medicion {idx_actual} como descargada")
                    try:
                        
                        await wait_event(events.med_descargada_event, TIME_TO_MARK_DOWNLOAD)
                    except asyncio.TimeoutError:
                        logging.error(
                            f"***** ⚠️ TimeOut Marcando medicion {idx_actual} como descargada")
                        events.med_descargada_event.set()
                    except Exception as e:
                        logging.error(
                            f"***** 🚨 Error inesperado Marcando medicion {idx_actual} como descargada: {e}")
                        events.med_descargada_event.set()  # Establece el evento en caso de error
                else:
                    logging.error(f"***** 🚨 Function:send_commands_ce | Error al intentar descargar la medicion indice {idx_actual} ")

            except asyncio.TimeoutError:
                logging.error(f"***** 🚨 Function:send_commands_ce | events.msg_received_event timeout problema en la recepcion de la medicion indice {idx_actual}")
                try:
                    if client.is_connected:
                        # refrescar servicios/notify por si quedaron colgados
                        try:
                            await client.get_services()
                        except Exception:
                            pass
                        try:
                            await client.start_notify(NOTIFY_CHAR_UUID, lambda s,d: notification_handler(s,d, client))
                        except Exception:
                            pass
                    else:
                        logging.warning("🔌 Cliente no conectado tras timeout; el caller debería reconectar.")
                except Exception as e:
                    logging.debug(f"refresh post-timeout: {e}")
                events.med_descargada_event.clear()
                if error_en_fecha:
                    command_cf = COMMAND_CF + bytearray([idx_actual])
                    await write_characteristic(client, WRITE_CHAR_UUID, command_cf)
                    logging.info(f"----- 🚨 Error: Marcando medicion {idx_actual} como descargada por error en fecha ")


                    events.msg_received_event.set()

            except Exception as e:
                logging.error(f"***** 🚨 Error inesperado Function:send_commands_ce: {e}")
                events.msg_received_event.set()  # Establece el evento en caso de error
            await asyncio.sleep(2)


    finally:
        # Asegurar que los eventos se establecen al final, independientemente de los errores
        events.med_array_event.set()
        events.measurements_processed_event.set()

# RECIBE Y PROCESA ENCABEZADO DE MEDICION
async def process_60_response(data, client):
    global fecha_extendida, cantidad_byte_med_ext, medicion_descargada, sample_rate, temp, alarma, alarma_vel, error_en_fecha,node_busy
    # logging.error(f"***** Data 60: {binascii.hexlify(data).decode('utf-8')}")
    result = True

    VALORES_PERMITIDOS = {
                        48017,
                        96017,
                        160019,
                        240017,
                        320021,
                        400019,
                        480023,
                        640025,
                        800027,
                        960029,
                        1120031,
                        1280033,
                        2560049,
                }


    msg_number = int.from_bytes(data[7:11], byteorder='little')
    if msg_number == 0 or msg_number % 100 == 0:
        logging.info(f"----- numero de msg recibido: {msg_number}   ")

    if msg_number == 0:

        error_en_fecha = False
        # logging.info(f"📥 Recibiendo encabezado: {binascii.hexlify(data).decode('utf-8')}")
        year, month, day, hour, minute, second = data[11:17]
        if (
            year > 99 or
            month < 1 or month > 12 or
            day < 1 or day > 31 or
            hour < 0 or hour > 23 or
            minute < 0 or minute > 59 or
            second < 0 or second > 59
        ):
            logging.warning(
                f"❌ Fecha/hora inválida: Y:{year} M:{month} D:{day} {hour:02}:{minute:02}:{second:02}"
            )    
        try:
            
            fecha_extendida = datetime.datetime(
                year + 2000, month, day, hour, minute, second)
            cantidad_byte_med_ext = struct.unpack('<I', data[17:21])[0]
            sample_rate = data[21]
            temp = struct.unpack('<f', data[22:26])[0]
            alarma = data[26]
            alarma_vel = data[27]
            # logging.info(f"📅 Fecha decodificada: {fecha_extendida}")
            logging.info(f"----- 🔢 Cantidad de bytes esperados: {cantidad_byte_med_ext}")

            # 📌 Validar que `cantidad_byte_med_ext` sea uno de los valores permitidos
            if cantidad_byte_med_ext not in VALORES_PERMITIDOS:
                logging.error(f"🚨 Error: cantidad_byte_med_ext {cantidad_byte_med_ext} no válido. Saltando procesamiento.")
                error_en_fecha = True
                return  # 🔴 Salir sin procesar más
        except ValueError as ve:
            logging.error(f"***** 🚨 Function:process_60_response |  Error en la creación de la fecha: {ve}")
            logging.error(f"***** 🔢 Data: {binascii.hexlify(data).decode('utf-8')}")
            error_en_fecha = True
        except TypeError as te:
            logging.error(f"***** 🚨 Function:process_60_response |  Error de tipo en la creación de la fecha: {te}")
            logging.error(f"***** 🔢 Data: {binascii.hexlify(data).decode('utf-8')}")
            error_en_fecha = True
        except Exception as e:
            logging.error(f"🚨 Error inesperado: {e}")
            error_en_fecha = True

    else:
        index = (msg_number-1) * PACKET_PAYLOAD
        # logging.info(f"indice reccibido: {index} msg_number: {msg_number} Cantidad de bytes esperados: {cantidad_byte_med_ext}")
        buffer_ext[index:index+PACKET_PAYLOAD] = data[11:249]
        # logging.info(f"medicion ext idx: {msg_number}")
        # if msg_number == 2142:
        total_bytes = cantidad_byte_med_ext
        expected_packets = math.ceil((total_bytes - HEADER_SIZE) / PACKET_PAYLOAD)

        # if msg_number >= ((cantidad_byte_med_ext - HEADER_SIZE + PACKET_PAYLOAD-1) / PACKET_PAYLOAD)-1:
        if msg_number >= expected_packets:

            if fecha_extendida is None:
                logging.error("🚨 fecha_extendida no fue inicializada correctamente.")
                events.msg_received_event.set()
                events.measurementsEX_processed_event.set()
                return
            
            logging.info(f"----- ✅ Medicion EXT recibia completa nodo: {client.address} fecha: {fecha_extendida}")
            
            raw_data = buffer_ext[: total_bytes - HEADER_SIZE]
            # resultado = descomponer_medicion_ext(buffer_ext[:total_bytes-HEADER_SIZE])
            resultado = descomponer_medicion_ext(raw_data)

            cantidad_byte_med_ext = 0
            # Convertir las listas de Python a arrays de PostgreSQL
            widnode_id = client.address
            if resultado is None:
                logging.error(
                    "🚨 descomponer_medicion_ext returned None. Skipping measurement insertion.")
                medicion_descargada = False
                events.msg_received_event.set()
                events.measurementsEX_processed_event.set()
                return

            if is_tach_enabled():
                rpm,fecha_rpm =  get_rpm_nearest(fecha_extendida)
                logging.info(f"----- 🔢 RPM obtenidas: {rpm} | fecha : {fecha_rpm}")
            else:
                rpm = 0
                fecha_rpm = None


            data = {
                "widnode_id": widnode_id,
                "serie_x": resultado["serieX"],
                "serie_y": resultado["serieY"],
                "serie_z": resultado["serieZ"],
                "fecha": fecha_extendida.strftime('%Y-%m-%d %H:%M:%S'),
                "rms_x": resultado["rmsX"],
                "rms_y": resultado["rmsY"],
                "rms_z": resultado["rmsZ"],
                "rms_x_vel": resultado["rmsX_vel"],
                "rms_y_vel": resultado["rmsY_vel"],
                "rms_z_vel": resultado["rmsZ_vel"],
                "sample_rate": sample_rate,
                "temp": temp,
                "alarma": alarma,
                "alarma_vel": alarma_vel,
                # === Globales tiempo, igual que el paquete RMS ===
                "media_x": resultado["mediaX"],
                "rms_axis_x": resultado["rmsAxisX"],
                "skew_x": resultado["skewX"],
                "kurtosis_x": resultado["kurtosisX"],
                "ptp_x": resultado["ptpX"],
                "crest_factor_x": resultado["crestFactorX"],
                "shape_factor_x": resultado["shapeFactorX"],
                "impulse_factor_x": resultado["impulseFactorX"],

                "media_y": resultado["mediaY"],
                "rms_axis_y": resultado["rmsAxisY"],
                "skew_y": resultado["skewY"],
                "kurtosis_y": resultado["kurtosisY"],
                "ptp_y": resultado["ptpY"],
                "crest_factor_y": resultado["crestFactorY"],
                "shape_factor_y": resultado["shapeFactorY"],
                "impulse_factor_y": resultado["impulseFactorY"],

                "media_z": resultado["mediaZ"],
                "rms_axis_z": resultado["rmsAxisZ"],
                "skew_z": resultado["skewZ"],
                "kurtosis_z": resultado["kurtosisZ"],
                "ptp_z": resultado["ptpZ"],
                "crest_factor_z": resultado["crestFactorZ"],
                "shape_factor_z": resultado["shapeFactorZ"],
                "impulse_factor_z": resultado["impulseFactorZ"],
                "rpm": rpm,
                "fecha_rpm": fecha_rpm.strftime('%Y-%m-%d %H:%M:%S') if fecha_rpm else None,
            }
            # logging.info(f" rmsy: {data.rms_y}")
            # print(data)
            # logging.info(f"RMS FFT X: {rmsx}, Y: {rmsy}, Z: {rmsz}")
            # logging.info(f"RMS FFT vel X: {rmsx_vel}, Y: {rmsy_vel}, Z: {rmsz_vel}")
            # logging.info(f"fecha : {fecha_extendida.strftime('%Y-%m-%d %H:%M:%S')}")
            # logging.info(f"alarma: {alarma} alarma_vel: {alarma_vel}")

            urlmeasurement = f"{API_URL}/measurement_ext/"
            try:
                # logging.info("📡 Insertando medición extendida en el backend...")
                # logging.debug(f"📦 Payload: {data}")
                # logging.debug(f"🌐 URL destino: {urlmeasurement}")
                # ——— observabilidad extra ———
                cid = uuid.uuid4().hex[:8]  # correlation id corto
                pts_x = len(data.get("serie_x", []))
                pts_y = len(data.get("serie_y", []))
                pts_z = len(data.get("serie_z", []))
                approx_bytes = 4 * (pts_x + pts_y + pts_z)  # float32*3
                logging.info(f"📡[{cid}] Insertando medición ext: "
                             f"pts=({pts_x},{pts_y},{pts_z}) ~{approx_bytes/1024:.1f} KiB → {urlmeasurement}")

                timeout = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=60.0)
                limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)

                t0 = time.perf_counter()
                
                # async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientMeaExt:
                # async with httpx.AsyncClient(cookies=cookies, timeout=timeout, limits=limits) as clientMeaExt:
                async with httpx.AsyncClient(cookies=cookies, timeout=timeout, limits=limits, http2=True) as clientMeaExt:
                    # response = await clientMeaExt.post(urlmeasurement, json=data)
                    # resp = await clientMeaExt.post(
                    #     urlmeasurement,
                    #     json=data,
                    #     headers={"X-Request-ID": cid}
                    # )

                    body = json.dumps(data, separators=(",", ":")).encode("utf-8")
                    gz  = gzip.compress(body, compresslevel=5)
                    headers = {
                        "Content-Type": "application/json",
                        "Content-Encoding": "gzip",
                        "X-Request-ID": cid,
                    }
                    resp = await clientMeaExt.post(urlmeasurement, content=gz, headers=headers)
                dt = time.perf_counter() - t0
                ctype = resp.headers.get("content-type")
                clen  = resp.headers.get("content-length")
                logging.info(f"📬[{cid}] HTTP {resp.status_code} en {dt:.2f}s "
                             f"(ctype={ctype}, len={clen})")

                    # logging.info("📬 Respuesta del backend: %s", response.status_code)
                if resp.status_code == 201:
                    # logging.info("✅ Measurement extended inserted successfully via API")
                    logging.info(f"✅[{cid}] Measurement ext registrada")
                    medicion_descargada = True
                else:
                    body_snip = (resp.text or "")[:300].replace("\n", " ")
                    logging.error(f"🚨[{cid}] HTTP {resp.status_code} body='{body_snip}…'")
                    medicion_descargada = False

            except httpx.ConnectTimeout:
                logging.error(f"⏱️ Conexión agotada (connect timeout) al insertar medición ext")
                medicion_descargada = False
            except httpx.ReadTimeout:
                logging.error(f"⏳ Lectura agotada (read timeout) esperando respuesta del backend")
                medicion_descargada = False
            except httpx.ConnectError as e:
                cause = repr(getattr(e, "__cause__", e))
                logging.error(f"🔌 Error de conexión (DNS/firewall/拒否). Causa: {cause}")
                medicion_descargada = False
            except httpx.RequestError as e:
                cause = repr(getattr(e, "__cause__", e))
                logging.error(f"🌐 RequestError: {e!r} | cause={cause}")
                medicion_descargada = False
            except Exception:
                logging.exception("🚨 Error inesperado al insertar medición extendida")
                medicion_descargada = False
            finally:
                # logging.info("📦 Finalizando recepción de medición extendida para índice %s", idx_actual)
                events.msg_received_event.set()
                events.measurementsEX_processed_event.set()

async def process_widnode(device_address):
    global rms_idx_list,indices_para_marcar, command_id_actual, hayComandos, cookies,node_busy,widnode_modbus_index,rms_download_active,WIDNODE_GW
    phase = "start"
    # al inicio, una vez por ciclo del nodo:
    modbus_id = await get_modbus_index_for_widnode(device_address)
    if modbus_id <= 0:
        logging.warning(f"Modbus idx faltante para {device_address}; no se publicará a Modbus")
    widnode_modbus_index[device_address] = modbus_id

    # Verifica si hay comandos pendientes por procesar
    logging.info("Ejecutando schedule task (tareas programadas)")
    # Verifica si hay comandos pendientes por procesar con execute_scheduled_tasks
    try:
        # Llama a la nueva función desde app2.py
        await execute_scheduled_tasks(cookies,WIDNODE_GW)
        # await ejecutar_tareas_programadas(API_URL,cookies)
        # logging.info("Scheduled tasks executed successfully.")
    except Exception as e:
        logging.error(f"🚨 Error executing scheduled tasks: {e}")

    # inicio de proceso para widnode {device_address}
    # logging.info(f"📌START PROCESS DEVICE: {device_address}...")
    logging.info(f"----- 🔍 Buscando dispositivo {device_address}...")
    device = await find_device_by_address(device_address)

    if device:
        retries = 0
        success = False
        phase = "connect"
        while retries < MAX_RETRIES and not success:
            client = BleakClient(device,address_type="public",Timeout=10.0)
            try:
                await bluetooth_stop_discovery_if_active()
                t0 = time.time()
                # await client.connect()
                # logging.info(f"----- 🔌 Conectado a {device_address}. (t_connect={time.time()-t0:.2f}s)")
                
                try:
                    # 0) corta cualquier scan y da un respiro al bus
                    await bluetooth_stop_discovery_if_active()
                    await asyncio.sleep(0.2)

                    # 1) primer intento
                    await asyncio.wait_for(client.connect(), timeout=10)
                except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                    logging.warning(f"⏱️ Timeout/Cancelled al conectar con {device_address} ({type(e).__name__}). Reintento con limpieza…")

                    # 2) limpieza defensiva del cliente “sucio”
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

                    # NUEVO: barrido de desconexión a nivel BlueZ para soltar estados pegados
                    try:
                        await disconnect_all_devices()
                        await asyncio.sleep(0.5)  # dar tiempo a que BlueZ cierre handles
                    except Exception as _e:
                        logging.debug(f"disconnect_all_devices() ignorado: {_e}")
                        
                    # 3) recrear el BleakClient para evitar estado interno roto
                    try:
                        del client
                    except Exception:
                        pass
                    await asyncio.sleep(0.8)                     # deja que BlueZ cierre handles
                    client = BleakClient(device, address_type="public", timeout=12.0)

                    # 4) segundo intento, nuevamente asegurando no haya scan
                    await bluetooth_stop_discovery_if_active()
                    await asyncio.sleep(0.2)
                    await asyncio.wait_for(client.connect(), timeout=12)

                logging.info(f"----- 🔌 Conectado a {device_address}. (t_connect={time.time()-t0:.2f}s)")
                
                # Marcar bonded device como Trusted (opcional, recomendado)

                try:
                    set_cached_rpa(device_address, client.address)
                except Exception:
                    pass
                try:
                    await mark_trusted(device_address)
                except Exception as _:
                    pass
                

                logging.info(f"----- ✅ MTU {client.mtu_size}.")
                phase = "detectando_nodo_ocupado"        
                busy = await detectar_nodo_ocupado(client)

                if busy:
                    logging.warning("⏳ Nodo ocupado. Esperando que termine antes de continuar...")
                    try:
                        await client.stop_notify(NOTIFY_CHAR_UUID)
                    except Exception:
                        pass
                    try:
                        await cancel_session_tasks(client.address)
                    except Exception:
                        pass
                    try:
                        new_session(client.address)
                    except Exception:
                        pass
                    try:
                        clear_expected(client.address)
                    except Exception:
                        pass
                    try:
                        
                        rms_download_active = False
                    except Exception:
                        pass
                    await asyncio.sleep(0.1)
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    logging.info(f"----- Cliente desconectado de {device_address} (skip por ocupado).")
                    return

                phase = "start_notify"
                sid = new_session(client.address)
                # Comienza la notificación y configura el dispositivo
                if NOTIFY_CHAR_UUID is None:
                    raise ValueError("NOTIFY_CHAR_UUID environment variable is not set.")
                # await client.start_notify(NOTIFY_CHAR_UUID, lambda sender, data: notification_handler(sender, data, client ))
                await client.start_notify(
                    NOTIFY_CHAR_UUID,
                    lambda sender, data, cli=client, session_id=sid: notification_handler(sender, data, cli, session_id)
                )
                await asyncio.sleep(0.1)  # Espera antes de continuar
                await ensure_notify_ready(client, timeout=3.0)  # ← nuevo
                # --- CONFIGURACIÓN (parte "config") ---
                if "config" in PROCESS_PARTS:
                    phase = "sync_connect"
                    await setConnect_device(client)
                    # await asyncio.sleep(0.5)
                else:
                    logging.info(f"[{device_address}] 🧪 Skip parte: config")

                # --- DESCARGA RMS + MARCADO (parte "rms") ---
                if "rms" in PROCESS_PARTS:
                    phase = "download_rms"
                    await download_rms(client)
                    # await asyncio.sleep(0.5)

                    indices_para_marcar = rms_idx_list.copy()
                    rms_idx_list.clear()
                    phase = "mark_rms_downloaded"
                    await mark_rms_downloaded(client, indices_para_marcar)
                    # await asyncio.sleep(0.5)
                else:
                    logging.info(f"[{device_address}] 🧪 Skip parte: rms")

                # --- DESCARGA MEDICIONES (parte "meas") ---
                if "meas" in PROCESS_PARTS:
                    phase = "download_measurements"
                    await download_measurements(client)
                else:
                    logging.info(f"[{device_address}] 🧪 Skip parte: meas")

                success = True

            except Exception as e:
                retries += 1
                logging.exception(f"***** 🚨 Function:process_device | Error en fase '{phase}' con {device_address}: {e}")
                # insertar_log(
                #     widnode_id=device_address,
                #     tipo="ERR-DISP-DISCONECT",
                #     descripcion=f"Error: {e} | reintento: {retries}"
                # )

                if retries < MAX_RETRIES:
                    logging.info(
                        f" 🔄 Reintentando conexión a {device_address} ({retries}/{MAX_RETRIES})...")
                    await asyncio.sleep(2)  # Espera antes de reintentar
                else:
                    logging.error(f"🚨 No se pudo procesar el dispositivo {device_address} después de {MAX_RETRIES} intentos")

            finally:
                # Desconecta solo después de todos los intentos o si el proceso fue exitoso
                if client.is_connected:
               # Cierre SIEMPRE consistente de la sesión BLE
                    try:
                        if client.is_connected:
                            # 1) Parar notificaciones para cortar el flujo de callbacks
                            try:
                                await client.stop_notify(NOTIFY_CHAR_UUID)
                            except Exception:
                                pass

                            # 2) Cancelar TODAS las tasks asociadas a esta sesión
                            try:
                                await cancel_session_tasks(client.address)
                            except NameError:
                                # si aún no implementaste cancel_session_tasks, ignora
                                pass
                            except Exception:
                                pass

                            # 3) Invalidar el epoch actual: cualquier frame rezagado queda descartado
                            try:
                                new_session(client.address)
                            except Exception:
                                pass

                            # 4) Limpiar gating de opcodes esperados y bajar flags de fase
                            try:
                                clear_expected(client.address)
                            except NameError:
                                pass
                            except Exception:
                                pass
                            # Asegurar que no queden fases "encendidas"
                            try:
                                
                                rms_download_active = False
                            except Exception:
                                pass

                            # 5) Pequeño drenaje y desconexión
                            await asyncio.sleep(0.1)
                            try:
                                await client.disconnect()
                            except Exception:
                                pass

                            logging.info(f"----- Cliente desconectado de {device_address}.")
                    except Exception:
                        # Cualquier excepción en cierre no debe romper el flujo del loop superior
                        logging.exception("Error al cerrar sesión BLE")

        if success:
            logging.info(f" ✔️ Proceso completado exitosamente para el dispositivo {device_address}")
        else:
            logging.error(f" 🚨 El proceso para el dispositivo {device_address} falló después de {MAX_RETRIES} intentos")

    else:
        logging.info(f"----- ❌ No se encontró el dispositivo.")

def get_system_timezone_via_timedatectl() -> str:
    """Devuelve la zona horaria del sistema según timedatectl."""
    result = subprocess.run(
        ['timedatectl', 'show', '--property=Timezone', '--value'],
        capture_output=True, text=True, check=True
    )
    return result.stdout.strip()

def get_system_timezone() -> str:
    import os, configparser, pathlib, subprocess

    cfg = configparser.ConfigParser()
    cfg.read(os.environ.get('CONFIG_FILE','/data/config.ini'))
    tz = cfg.get('System', 'timezone', fallback=None)
    if tz:
        return tz

    # Fallbacks (por si alguna vez falta en config.ini)
    # 1) env TZ
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    # 2) timedatectl
    try:
        tz = subprocess.check_output(
            ["timedatectl","show","--property=Timezone","--value"], text=True
        ).strip()
        if tz:
            return tz
    except Exception:
        pass
    # 3) /etc/timezone
    etc_tz = pathlib.Path("/etc/timezone")
    if etc_tz.exists():
        return etc_tz.read_text().strip()
    # 4) symlink /etc/localtime
    try:
        target = pathlib.Path("/etc/localtime").resolve()
        parts = target.parts
        if "zoneinfo" in parts:
            idx = parts.index("zoneinfo") + 1
            return "/".join(parts[idx:])
    except Exception:
        pass

    return "UTC"

# def get_system_timezone() -> str:
#     """
#     Devuelve la zona horaria del sistema de forma robusta:
#     - timedatectl (cuando existe)
#     - /etc/timezone (si existe)
#     - symlink /etc/localtime -> /usr/share/zoneinfo/Region/City
#     - env TZ o 'UTC' como último recurso
#     """
#     import os, pathlib, subprocess, configparser
#     cfg = configparser.ConfigParser()
#     cfg.read(os.environ.get('CONFIG_FILE','/data/config.ini'))
#     tz = cfg.get('System', 'timezone', fallback=None)
#     if tz:
#         return tz
#     # …luego los fallbacks actuales (timedatectl, /etc/timezone, symlink, env TZ, UTC)
#     # 1) timedatectl (cuando hay systemd)
#     try:
#         out = subprocess.run(
#             ['timedatectl', 'show', '--property=Timezone', '--value'],
#             capture_output=True, text=True, check=True
#         )
#         tz = (out.stdout or "").strip()
#         if tz:
#             return tz
#     except Exception:
#         pass

#     # 2) /etc/timezone
#     tz_file = pathlib.Path('/etc/timezone')
#     try:
#         if tz_file.exists():
#             tz = tz_file.read_text(encoding='utf-8').strip()
#             if tz:
#                 return tz
#     except Exception:
#         pass

#     # 3) symlink /etc/localtime
#     try:
#         real = pathlib.Path('/etc/localtime').resolve()
#         prefix = pathlib.Path('/usr/share/zoneinfo')
#         if str(real).startswith(str(prefix)):
#             return str(real.relative_to(prefix))
#     except Exception:
#         pass

#     # 4) env TZ o UTC
#     return os.environ.get('TZ', 'UTC')
# ============================================================
# Desconectar cualquier Device1 conectado en BlueZ (hci0)
# ============================================================
async def disconnect_all_devices():
    """
    Recorre todos los org.bluez.Device1 y llama Disconnect() en los que
    tengan Connected=True. Ignora errores benignos (NotConnected).
    """
    # Usamos el bus del agente si ya está, o abrimos uno temporal
    local_bus = False
    bus = globals().get("BLUEZ_BUS")
    if bus is None:
        from dbus_next.aio import MessageBus
        from dbus_next import BusType
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        local_bus = True

    try:
        # Obtener todos los objetos administrados por BlueZ
        reply = await _dbus_call(
            bus,
            "org.bluez",
            "/",
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
        )
        objects = reply.body[0]  # dict[path] -> dict[iface] -> dict[prop]=Variant

        total = 0
        disconnected = 0
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            # Extraer Address y Connected desde Variant
            addr = dev.get("Address").value if "Address" in dev else "unknown"
            connected = dev.get("Connected").value if "Connected" in dev else False
            total += 1
            if connected:
                try:
                    await _dbus_call(bus, "org.bluez", path, "org.bluez.Device1", "Disconnect")
                    logging.info(f"🔌 Desconectado {addr} ({path})")
                    disconnected += 1
                except Exception as e:
                    # Ignorar errores de estado (ya se desconectó, etc.)
                    logging.warning(f"⚠️ No se pudo desconectar {addr}: {e}")

        logging.info(f"🔎 BlueZ tiene {total} devices; desconectados ahora: {disconnected}")
    finally:
        if local_bus:
            try:
                await bus.wait_for_disconnect()  # opcional
            except Exception:
                pass


async def process_devices():
    global cookies,WIDNODE_GW  # Usamos las cookies globales
    global WIDNODE_SET
    
    LOCAL_ADDRESS = getMacBluetooth(logging)

    if not LOCAL_ADDRESS:
        # Si no tenemos MAC local, el backend no puede asociar el gateway y quedamos en loop.
        # Esto suele ocurrir cuando hci0 está down/colgado.
        raise RuntimeError("LOCAL_ADDRESS is None (adaptador BLE no disponible)")

    # logging.info(f" LOCAL ADDRESS = {LOCAL_ADDRESS}")
    ip_addresses = get_ip_addresses()
    timezone = get_system_timezone()
    gw_temp = 0
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        gw_temp = int(f.read().strip()) / 1000.0

    logging.info(f"🧾 TIMEZONE = {timezone} | IP ADDRESS = {ip_addresses} | | TEMP = {gw_temp}")
    

    # LOCAL_ADDRESS = '98:03:CF:D2:25:EF'
    # logging.info(f"🧾 LOCAL_ADDRESS = {LOCAL_ADDRESS}")
    url = f"{API_URL}/gateway/{LOCAL_ADDRESS}"
    # logging.info(f"🧾 URL = {url}")

    # Leer la versión del firmware desde version.txt
    # version_file = "/home/widnode/dev/widnode/version.txt"
    # firmware_version = "0.0.0"  # Valor por defecto si falla

    # try:
    #     with open(version_file, "r") as vf:
    #         firmware_version = vf.read().strip()
    # except Exception as e:
    #     logging.warning(f"⚠️ No se pudo leer el archivo de versión: {e}")

    # logging.info(f"Firmware Version = {firmware_version}")

    # Llamada a la API OBTENGO EL ID DEL GATEWAY
    async with httpx.AsyncClient(cookies=cookies) as clientWeb:

        response = await clientWeb.get(url)

        if response.status_code == 200:
            data = response.json()
            WIDNODE_GW = data.get("gw_id")
        else:
            if response.status_code == 401:
                raise ReauthNeeded(f'401 fetching gw_id for LOCAL_ADDRESS {LOCAL_ADDRESS}')
            logging.error(f" 🚨 Failed to fetch gw_id for LOCAL_ADDRESS {LOCAL_ADDRESS}: {response.status_code}")
            led_blink(3, ("err"), 3.0)
            return

    gateway_ip = f"{API_URL}/gateway/ip/{WIDNODE_GW}"
    # gateway_tz = f"{API_URL}/gateway/tz/{WIDNODE_GW}"
    ip_string = " ".join(f"{key}={value}" for key,
                         value in ip_addresses.items())
    
    # logging.info(f"🧾 gateway_ip = {gateway_ip}")
    # logging.info(f"🧾 ip_string = {ip_string}")

    # logging.info("consultando comando...")
    # comando = await consultar_comando(WIDNODE_GW, cookies)
    # # logging.info(f"Comando consultado: {comando}") 
    # if comando:
    #     # logging.info(f"Comando consultado: {comando}")   
    #     resultado = ejecutar_comando(comando)
    #     # logging.info(f"Resultado:\n{resultado}")
    #     await enviar_resultado(resultado,WIDNODE_GW, cookies)

    try:
        # Llamada a la API ACTUALIZO LA IP DEL DISPOSITIVO EN LA BASE DE DATOS
        async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientgwIP:
            payload = {
                "ip": ip_string,
                "temp": gw_temp,
                "tz": timezone,
            }
            response_ip = await clientgwIP.put(gateway_ip, json=payload)
            if response_ip.status_code != 200:
                if response_ip.status_code == 401:
                    raise ReauthNeeded('401 updating gateway ip/tz/temp')
            #     logging.info("✅  Informacion del gateway updatesuccessfully via API")
            # else:
                logging.error(
                    f"🚨 Error Updating gateway data: {response_ip.status_code} - {response_ip.text}")
                led_blink(5, ("err",), 3.0)
                return
                
    except httpx.ConnectTimeout:
        logging.error("Connection timed out. The server may be down or unreachable.")
        led_blink(5, ("err",), 3.0)
    except httpx.RequestError as e:
        logging.error(f"🚨 An error occurred while requesting {e.request.url!r}.")
        led_blink(5, ("err",), 3.0)

    # try:
    #     # Llamada a la tz ACTUALIZO LA IP DEL DISPOSITIVO EN LA BASE DE DATOS
    #     async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientgwTZ:
    #         response_tz = await clientgwTZ.put(gateway_tz, json={"tz": timezone})
    #         if response_tz.status_code != 200:
    #         #     logging.info("✅  TZ updatesuccessfully via API")
    #         # else:
    #             logging.error(
    #                 f"🚨 Error Updating TZ data: {response_tz.status_code} - {response_tz.text}")
    #             return
                
    except httpx.ConnectTimeout:
        logging.error("Connection timed out. The server may be down or unreachable.")
    except httpx.RequestError as e:
        logging.error(f"🚨 An error occurred while requesting {e.request.url!r}.")

    # ============================================================
    # Lista de nodos: override (archivo/CLI) o backend (modo actual)
    # ============================================================
    global WIDNODE_SET, OVERRIDE_WIDNODES

    if OVERRIDE_WIDNODES is not None:
        WIDNODE = OVERRIDE_WIDNODES
        logging.info(f"🧪 MODO PRUEBA: usando lista de widnodes por override: {WIDNODE}")
    else:
        widnode_by_gw = f"{API_URL}/widnodes/gw/{WIDNODE_GW}"

        # Llamada a la API OBTENGO LA LISTA DE NODOS VINCULADOS
        async with httpx.AsyncClient(cookies=cookies) as clientWeb:
            response_widnode = await clientWeb.get(widnode_by_gw)

            if response_widnode.status_code == 200:
                data_widnode = response_widnode.json()
                WIDNODE = [item['widnode_id'] for item in data_widnode]
                logging.info(f"🧾 WIDNODE IDs = {WIDNODE}")
            else:
                if response_widnode.status_code == 401:
                    raise ReauthNeeded(f'401 fetching widnode list for gw_id {WIDNODE_GW}')
                logging.error(f" 🚨 Failed to fetch widnode_id list for gw_id {WIDNODE_GW}: {response_widnode.status_code}")
                return

    WIDNODE_SET = set(WIDNODE)
    await prime_rssi_cache(timeout=20.0)
    logging.info(f"📡 RSSI cache snapshot (post-precarga): {rssi_by_identity}")
    # ***************************************
    # Llamada A PROCESAR CADA NODO
    # ***************************************
    for device_address in WIDNODE:
        logging.info(f"*********************{device_address}******************************")
        logging.info(f"********************************************************************")
        await process_widnode(device_address)
        logging.info(f"********************************************************************")


# async def watchdog_task():
#     """Tarea independiente que patea el watchdog de systemd periódicamente."""
#     global notifier
#     if notifier is None:
#         return
#     # Intervalo de pateo: mitad de WATCHDOG_USEC (o 10s si no está seteado)
#     wd_usec = int(os.environ.get("WATCHDOG_USEC", "0") or "0")
#     interval = max(5.0, wd_usec / 2_000_000.0) if wd_usec else 10.0
#     while True:
#         try:
#             notifier.notify("WATCHDOG=1")
#         except Exception:
#             pass
#         await asyncio.sleep(interval)
# def kick():
#     """Patea el watchdog de systemd de forma inmediata (seguro si notifier no existe)."""
#     global notifier
#     if notifier:
#         try:
#             notifier.notify("WATCHDOG=1")
#         except Exception:
#             pass

# Función principal que contiene tu lógica actual
async def main_loop():
    global events, token
    global BT_FAIL_STREAK
    # global notifier

    # ✅ Inicialización segura de eventos y locks
    events = WidnodeEvents()
    rms_lock = asyncio.Lock()
    measurement_ext_lock = asyncio.Lock()


    
    # ✅ Guardar los locks en globales si otros bloques los necesitan
    globals()["rms_lock"] = rms_lock
    globals()["measurement_ext_lock"] = measurement_ext_lock

    email = os.getenv("EMAIL")
    password = os.getenv("PASSWORD")
    
    # Paso 1: Autenticación
    token = await login(email, password)
    # logging.info(f"token: ${token}")
    if not token:
        logging.error("🚨 Unable to log in. Exiting.")
        led_blink(5, ("err",), 3.0)
        return

    token = token.lstrip("$")

    # === systemd watchdog: READY + tarea de latidos ===
    # notifier = SystemdNotifier()
    # try:
    #     notifier.notify("READY=1")
    #     asyncio.create_task(watchdog_task())
    # except Exception as e:
    #     logging.warning(f"Watchdog systemd no disponible: {e}")

    _load_last_seen()  # <-- NUEVO: carga cache RPA al inicio

    # === Iniciar Agent BlueZ para passkey automático ===
    try:
        await start_bt_agent()
    except Exception as e:
        logging.error(f"[AGENT] Error iniciando Agent BlueZ: {e}")

    logging.info("🔁 Iniciando bucle principal widnode...")

    auth_backoff = AUTH_BACKOFF_MIN

    while True:
        start_time = time.time()
        
        try:
            if await should_restart_adapter():
                ok = restart_bluetooth(logging)
                if not ok:
                    BT_FAIL_STREAK += 1
                    logging.warning(f"[BT] restart_bluetooth falló; fail_streak={BT_FAIL_STREAK}/{BT_FAIL_STREAK_MAX}")
                else:
                    # Un restart exitoso suele recuperar el bus.
                    BT_FAIL_STREAK = 0

            await process_devices()  # Llama a la función principal de tu script
            BT_FAIL_STREAK = 0
            auth_backoff = AUTH_BACKOFF_MIN
        except ReauthNeeded as e:
            # 401 / cookie expirada / backend rechazó sesión => re-login sin esperar 300s
            logging.warning(f"🔑 Re-auth requerido: {e}")
            led_blink(3, ("lan", "lte"), 3.0)

            # Backoff exponencial con jitter para no spamear backend en caídas
            await asyncio.sleep(min(auth_backoff, AUTH_BACKOFF_MAX) + random.random())
            auth_backoff = min(max(AUTH_BACKOFF_MIN, auth_backoff * 2), AUTH_BACKOFF_MAX)

            # Re-login (con lock)
            try:
                await ensure_authenticated(email, password, reason=str(e))
            except ReauthNeeded as e2:
                logging.error(f"🚨 Re-login falló: {e2}")
                continue  # vuelve al inicio del loop (no esperar 300s)

            continue  # reintentar ciclo inmediatamente
        except Exception as e:
            # Si el BLE está roto (sin MAC / no powered / timeouts), evitamos un loop infinito.
            msg = str(e)
            ble_related = (
                "LOCAL_ADDRESS is None" in msg
                or "org.bluez.Error.Failed" in msg
                or "Connection timed out" in msg
                or "hci0" in msg
            )
            if ble_related:
                BT_FAIL_STREAK += 1
                logging.error(f"🚨 Error BLE detectado: {e} | fail_streak={BT_FAIL_STREAK}/{BT_FAIL_STREAK_MAX}")
            else:
                logging.error(f"🚨 Error en la ejecución del proceso principal: {e}")

            led_blink(10, ("err",), 3.0)

            if BT_FAIL_STREAK >= BT_FAIL_STREAK_MAX:
                reason = f"ble-fail-streak={BT_FAIL_STREAK} last_err={msg}"
                logging.error(f"[CTRL] {reason} -> solicitando reinicio del contenedor")
                _request_container_restart(reason)
                raise SystemExit(0)

        # === NUEVO: liberar conexiones BLE pendientes en el host ===
        try:
            await disconnect_all_devices()
        except Exception as e:
            logging.warning(f"⚠️ No se pudo forzar desconexión inicial: {e}")


        # Calcula el tiempo que tomó ejecutar el proceso
        elapsed_time = time.time() - start_time
        wait_time = max(0, 300 - elapsed_time)  # 1200 segundos = 20 minutos

        if rssi_by_identity:
            logging.info("📡 RSSI cache snapshot (last seen):")
            for addr, rssi in rssi_by_identity.items():
                logging.info("   %s -> %s dBm", addr, rssi)
        else:
            logging.info("📡 RSSI cache snapshot: vacío")
            
        if wait_time > 0:
            logging.info(
                f"⏱️ Esperando {wait_time} segundos para la siguiente ejecución...")
            await asyncio.sleep(wait_time)
        else:
            logging.info(
                "🏁 Reiniciando inmediatamente debido al tiempo de ejecución prolongado...")


# Ejecutar ambas pruebas de conexión
if __name__ == "__main__":
    import argparse
    import importlib.metadata, logging, asyncio


    parser = argparse.ArgumentParser(description="Widnode Gateway - modo prueba/calibración")
    parser.add_argument("--widnode", "--node", action="append", default=[],
                        help="Widnode ID/MAC. Repetible. Ej: --widnode AA:BB:CC:DD:EE:FF")
    parser.add_argument("--widnodes-file", default=None,
                        help="Archivo con lista de widnodes (1 por línea o separados por coma/espacio).")
    parser.add_argument(
    "--parts",
    default=None,
    help="Partes a ejecutar en process_widnode. Ej: config,rms,meas  |  config  |  rms  |  meas"
)
    args = parser.parse_args()

    # Si se provee archivo o --widnode, activamos override y se omite consulta al backend
    if args.widnodes_file:
        OVERRIDE_WIDNODES = load_widnodes_from_file(args.widnodes_file)

    if args.widnode:
        OVERRIDE_WIDNODES = (OVERRIDE_WIDNODES or []) + args.widnode

    if OVERRIDE_WIDNODES is not None and not OVERRIDE_WIDNODES:
        logging.error("Se indicó override de widnodes pero quedó vacío.")
        raise SystemExit(2)
    
    init_process_parts_from_env()  # default si no hay CLI

    if args.parts:
        PROCESS_PARTS = _parse_parts(args.parts)
        if not PROCESS_PARTS:
            logging.error("No quedó ninguna parte válida seleccionada. Abortando.")
            raise SystemExit(2)

    logging.info(f"▶️ PROCESS_PARTS = {sorted(PROCESS_PARTS)}")

    _start_health_server()
     # Log de versión de pymodbus
    try:
        logging.info(f"pymodbus version: {importlib.metadata.version('pymodbus')}")
    except Exception:
        led_blink(3, ("err",), 5.0)
        logging.info("pymodbus no instalado en este intérprete")

    # Construís el servidor, pero NO lo arrancás todavía
    try:
        ms = get_modbus_server_config()
        modbus_srv = GatewayModbusServer(
            bind_host=ms["bind"],
            port=ms["port"],
            unit_id=ms["unit_id"],
            mapping=Map(base=ms["base"], stride=ms["stride"], max_index=ms["max_index"])
        )
    except Exception as e:
        logging.error(f"MODBUS SERVER deshabilitado: {e}")
        modbus_srv = None

    async def entry():
        # Ahora sí: dentro del loop
        if modbus_srv is not None:
            await modbus_srv.start()
            logging.info(
                f"Modbus SERVER escuchando en {ms['bind']}:{ms['port']} "
                f"base={ms['base']} stride={ms['stride']} max_index={ms['max_index']}"
            )
        await main_loop()

    asyncio.run(entry())

    # try:
    #     loop = asyncio.new_event_loop()
    #     asyncio.set_event_loop(loop)
    #     loop.run_until_complete(main_loop())
    #     loop.close()
    # finally:
    #     logging.info("bey bey...")
