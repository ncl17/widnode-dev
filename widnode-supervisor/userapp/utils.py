import subprocess
import time
import socket
import psutil
import re
import configparser
import asyncio
import logging
from dotenv import load_dotenv
import socket, struct, fcntl, configparser, os


load_dotenv()

node_busy = False  # Podés mover este estado a un objeto compartido si querés evitar globals


NOTIFY_CHAR_UUID = os.getenv('NOTIFY_CHAR_UUID')
CONFIG_FILE = os.getenv('CONFIG_FILE')

# def getMacBluetooth(logging):
#     try:
#         # logging.info('consultando MAC LOCAL')
#         # Ejecuta el comando hcitool con la ruta completa para evitar problemas de entorno
#         result = subprocess.run(['/usr/bin/hcitool', 'dev'], capture_output=True, text=True, check=True)
        
#         # Extrae la salida
#         output = result.stdout.strip()
        
#         # Usa una expresión regular para encontrar la dirección MAC en la salida
#         mac_address = re.search(r'([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', output)
        
#         if mac_address:
#             mac = mac_address.group(0)
#             logging.info(f"ℹ️ La dirección MAC del dispositivo Bluetooth local es: {mac_address.group(0)}")
#             return mac
#         else:
#             logging.info("No se encontró una dirección MAC en la salida.")
#             return None
        

#     except subprocess.CalledProcessError as e:
#         logging.info("Hubo un error al ejecutar hcitool:", e)
#         return None
def getMacBluetooth(logging, adapter: str = "hci0"):
    """Devuelve la MAC *pública* del adaptador.

    Nota: en estados raros (driver colgado / rfkill / firmware), `hcitool dev` puede no
    listar `hci0` y devolver None. Para hacerlo más robusto, hacemos fallback a sysfs.
    """
    # 1) Intento legacy: hcitool dev
    try:
        result = subprocess.run(["/usr/bin/hcitool", "dev"], capture_output=True, text=True, check=True)
        output = (result.stdout or "").strip()
        mac_address = re.search(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", output)
        if mac_address:
            mac = mac_address.group(0)
            logging.info(f"ℹ️ La dirección MAC del dispositivo Bluetooth local es: {mac}")
            return mac
        logging.info("No se encontró una dirección MAC en la salida.")
    except Exception as e:
        # No lo tratamos como fatal: a veces hcitool falla cuando el adaptador está down.
        logging.debug(f"getMacBluetooth(): hcitool dev falló: {e}")

    # 2) Fallback: sysfs
    try:
        sysfs_path = f"/sys/class/bluetooth/{adapter}/address"
        with open(sysfs_path, "r", encoding="utf-8") as f:
            mac = f.read().strip()
        if re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
            logging.info(f"ℹ️ MAC (sysfs {adapter}) = {mac}")
            return mac
    except Exception as e:
        logging.debug(f"getMacBluetooth(): sysfs address falló: {e}")

    return None
# def restart_bluetooth(logging):
#     try:
#         logging.info("🔁 Reiniciando adaptador hci0 (bajando y levantando)...")

#         # Apagar adaptador
#         subprocess.run(["sudo", "-n", "/usr/bin/hciconfig", "hci0", "down"], check=True)
#         time.sleep(2)

#         # Encender adaptador
#         subprocess.run(["sudo", "-n", "/usr/bin/hciconfig", "hci0", "up"], check=True)
#         time.sleep(2)

#         # logging.info("✅ Adaptador hci0 reiniciado correctamente (sin bluetoothd)")
#     except subprocess.CalledProcessError as e:
#         logging.error(f"🚨 Error al reiniciar hci0: {e}")

def restart_bluetooth(logging):
    """
    Reinicia el adaptador hci0 sin usar sudo.
    1) Intenta con 'bluetoothctl power off/on' (recomendado, usa DBus del host).
    2) Si falla, intenta con 'hciconfig hci0 down/up'.
    3) Si tampoco, prueba 'btmgmt -i hci0 power off/on'.
    """
    try:
        logging.info("🔁 Reiniciando adaptador hci0 (sin sudo, vía bluetoothctl)...")
        subprocess.run(["/usr/bin/bluetoothctl", "power", "off"], check=True)
        time.sleep(2)
        subprocess.run(["/usr/bin/bluetoothctl", "power", "on"], check=True)
        time.sleep(2)
        return True
    except Exception as e:
        logging.warning(f"bluetoothctl power off/on falló: {e}. Probando hciconfig...")

    try:
        subprocess.run(["/usr/bin/hciconfig", "hci0", "down"], check=True)
        time.sleep(2)
        subprocess.run(["/usr/bin/hciconfig", "hci0", "up"], check=True)
        time.sleep(2)
        return True
    except Exception as e:
        logging.warning(f"hciconfig down/up falló: {e}. Probando btmgmt...")

    try:
        subprocess.run(["/usr/bin/btmgmt", "-i", "hci0", "power", "off"], check=True)
        time.sleep(2)
        subprocess.run(["/usr/bin/btmgmt", "-i", "hci0", "power", "on"], check=True)
        time.sleep(2)
        return True
    except Exception as e:
        logging.error(f"🚨 No se pudo reiniciar hci0 con ninguno de los métodos: {e}")
        return False
        
def get_ip_addresses():
    ip_info = {}
    
    for interface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family == socket.AF_INET:  # Solo IPv4
                ip_info[interface] = addr.address
    
    return ip_info

def get_api_server_address():
    if CONFIG_FILE is None:
        raise ValueError("⚠️ La variable de entorno CONFIG_FILE no está definida en el archivo .env")

    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    if 'API' in config and 'API_URL' in config['API']:
        return config['API']['API_URL']
    else:
        raise ValueError("API_URL not found in config.ini")
    
# async def detectar_nodo_ocupado(client, timeout=4):
#     """
#     Escucha brevemente notificaciones BLE para detectar si el nodo está transmitiendo espontáneamente
#     mensajes de medición extendida (0x60) o RMS (0xDD), lo cual sugiere que quedó en estado interrumpido.
#     """
#     global node_busy
#     node_busy = False
#     flag_evento = asyncio.Event()

#     def handler_temporal(sender, data):
#         command_type = data[6]
#         if command_type in [0x60, 0xDD]:
#             logging.warning(f"⚠️ Nodo parece estar transmitiendo espontáneamente (tipo: {hex(command_type)})")
#             node_busy = True
#             flag_evento.set()

#     try:
#         await client.start_notify(NOTIFY_CHAR_UUID, handler_temporal)
#         try:
#             await asyncio.wait_for(flag_evento.wait(), timeout=timeout)
#         except asyncio.TimeoutError:
#             logging.info("✔️ Nodo no parece estar ocupado (sin mensajes espontáneos)")
#     finally:
#         await client.stop_notify(NOTIFY_CHAR_UUID)

async def detectar_nodo_ocupado(client, timeout=4) -> bool:
    """
    Devuelve True si el nodo emite 0x60/0xDD espontáneamente durante 'timeout' segundos.
    """
    flag_evento = asyncio.Event()
    busy = False

    def handler_temporal(sender, data):
        nonlocal busy
        command_type = data[6]
        if command_type in (0x60, 0xDD):
            logging.warning(f"⚠️ Nodo parece estar transmitiendo espontáneamente (tipo: {hex(command_type)})")
            busy = True
            flag_evento.set()

    try:
        await client.start_notify(NOTIFY_CHAR_UUID, handler_temporal)
        try:
            await asyncio.wait_for(flag_evento.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logging.info("✔️ Nodo no parece estar ocupado (sin mensajes espontáneos)")
    finally:
        try:
            await client.stop_notify(NOTIFY_CHAR_UUID)
        except Exception:
            pass
    return busy

def is_bluetooth_powered(logging) -> bool:
    """
    Devuelve True si 'bluetoothctl show' indica Powered: yes
    """
    try:
        out = subprocess.run(["/usr/bin/bluetoothctl", "show"], capture_output=True, text=True, check=True).stdout
        for line in out.splitlines():
            if "Powered:" in line:
                return "yes" in line.lower()
        # si no encontramos la línea, asumimos no powered
        return False
    except Exception as e:
        logging.debug(f"is_bluetooth_powered() error: {e}")
        return False

SIOCGIFADDR = 0x8915  # ioctl para obtener IPv4 de interfaz

def resolve_iface_ipv4(ifname: str) -> str:
    ifname = ifname[:15]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        data = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack('256s', ifname.encode('utf-8')))
        return socket.inet_ntoa(data[20:24])
    finally:
        s.close()


def get_modbus_server_config():
    """Lee [MODBUS_SERVER] y soporta BIND = iface:<ifname>."""
    if CONFIG_FILE is None:
        raise ValueError("⚠️ CONFIG_FILE no definido (.env)")
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    if 'MODBUS_SERVER' not in cfg:
        raise ValueError("[MODBUS_SERVER] no encontrada en config.ini")

    sec = cfg['MODBUS_SERVER']
    bind_raw  = sec.get("BIND", "0.0.0.0").strip()
    port      = sec.getint("PORT", 502)
    unit_id   = sec.getint("UNIT_ID", 1)
    base      = sec.getint("BASE", 0)
    stride    = sec.getint("STRIDE", 16)
    max_index = sec.getint("MAX_INDEX", 256)

    if bind_raw.lower().startswith("iface:"):
        iface = bind_raw.split(":", 1)[1].strip()
        bind = resolve_iface_ipv4(iface)
    else:
        bind = bind_raw

    return {
        "bind": bind,
        "port": port,
        "unit_id": unit_id,
        "base": base,
        "stride": stride,
        "max_index": max_index,
    }

