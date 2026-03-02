import asyncio
import base64
import logging
from bleak import BleakClient, BleakScanner
import httpx
import configparser
from utils import detectar_nodo_ocupado,node_busy,get_api_server_address, restart_bluetooth
from dotenv import load_dotenv
import os
import time
from typing import Dict

load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

COMMAND_MED = bytearray([0x49, 0x44, 0xD5])  # Comando base
NOTIFY_CHAR_UUID = os.getenv('NOTIFY_CHAR_UUID')  # UUID de notificación
WRITE_CHAR_UUID = os.getenv('WRITE_CHAR_UUID')  # UUID de escritura

CONFIG_FILE_ST = os.getenv('CONFIG_FILE')  # Archivo de configuración

API_URL=get_api_server_address()

MAX_WAIT_TIME_BUSY = 140

CONNECTION_LIMIT = 5  # Máximo de conexiones concurrentes
semaphore = asyncio.Semaphore(CONNECTION_LIMIT)  # Semáforo para limitar conexiones

MAX_RETRIES = 8  # Más reintentos para dispositivos lentos o inestables

def generar_handler(mac):
    def handler(sender, data):
        logging.info(f"📩 Notificación de {mac}: {data.hex()}")
    return handler

    
# async def conectar_secuencialmente(mac_list):
#     conexiones = {}

#     for mac in mac_list:
#         for intento in range(1, MAX_RETRIES + 1):
#             try:
#                 logging.info(f"🔍 Buscando {mac} (intento {intento})...")

#                 # Fuerza escaneo previo para mejorar descubrimiento
#                 await BleakScanner.discover(timeout=10.0)

#                 # Intentar encontrar el dispositivo varias veces dentro del tiempo límite
#                 device = None
#                 for _ in range(12):  # 12 x 5s = 60s
#                     device = await BleakScanner.find_device_by_address(mac, timeout=5.0)
#                     if device:
#                         break
#                     logging.debug(f"⏳ {mac} aún no encontrado, reintentando...")
#                     await asyncio.sleep(0.5)

#                 if not device:
#                     logging.warning(f"⚠️ {mac} no encontrado")
#                     continue

#                 # client = BleakClient(mac)
#                 # Crear cliente usando el BLEDevice real
#                 client = BleakClient(
#                     device,
#                     address_type="public",   # evita errores de RPA
#                     timeout=30.0
#                 )
#                 logging.info(f"🔌 Conectando a {mac}...")
#                 # await client.connect(timeout=60.0)
#                 await client.connect()
#                 if client.is_connected:
#                     logging.info(f"✅ Conectado a {mac}")
#                     conexiones[mac] = client
#                     await asyncio.sleep(1.5)
#                     break
#             except Exception as e:
#                 logging.warning(f"⚠️ Error conectando a {mac} (intento {intento}): {e}")
#                 await asyncio.sleep(2)
#         else:
#             logging.error(f"❌ No se pudo conectar a {mac} después de {MAX_RETRIES} intentos")

#     return conexiones
async def conectar_secuencialmente(
    macs,
    strict: bool = True,
    max_attempts: int = 3,
    prescan_timeout: float = 8.0,
) -> Dict[str, BleakClient]:
    """
    Conecta secuencialmente a uno o varios dispositivos BLE.
    NO ENVÍA COMANDOS.
    Devuelve un diccionario { mac: client } con clientes CONECTADOS y activos.

    - macs puede ser string → se convierte en [mac]
    - strict=True → si algún dispositivo falla, desconecta los conectados y devuelve {}
    - strict=False → devuelve solo los que se hayan podido conectar
    """

    # --- Normalizar entrada ---
    if isinstance(macs, str):
        mac_list = [macs]
    else:
        mac_list = list(macs)

    # --- PRE-SCAN: antes de conectar a nadie ---
    logging.info(
        f"🔎 Pre-scan BLE ({prescan_timeout}s) para {len(mac_list)} MAC(s)..."
    )
    discovered_devices = await BleakScanner.discover(timeout=prescan_timeout)
    by_addr = {d.address.upper(): d for d in discovered_devices}
    logging.info(
        f"📡 Pre-scan encontró {len(discovered_devices)} dispositivos en total."
    )

    conexiones: Dict[str, BleakClient] = {}

    # --- Intentar conectar cada MAC en orden ---
    for mac in mac_list:
        mac_u = mac.upper()
        client = None
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                logging.info(
                    f"🔍 [Intento {attempt}/{max_attempts}] Preparando conexión a {mac}..."
                )

                # 1) Intentar usar el BLEDevice del pre-scan
                device = by_addr.get(mac_u)

                # 2) Si no apareció en el pre-scan, plan B: intento puntual de find_device_by_address
                if device is None:
                    logging.warning(
                        f"⚠️ {mac} no apareció en pre-scan, usando find_device_by_address..."
                    )
                    device = await BleakScanner.find_device_by_address(
                        mac, timeout=5.0
                    )

                if not device:
                    last_error = f"No se encontró {mac} en intento {attempt}"
                    logging.warning(f"❌ {last_error}")
                    await asyncio.sleep(1.0)
                    continue

                logging.info(f"📡 Dispositivo para {mac}: {device.address}")

                # Crear cliente con BLEDevice real
                client = BleakClient(
                    device,
                    address_type="public",
                    timeout=30.0,
                )

                logging.info(
                    f"🔌 Conectando a {device.address} (intento {attempt})..."
                )
                await client.connect()

                if not client.is_connected:
                    last_error = (
                        f"Cliente de {mac} no quedó conectado en intento {attempt}"
                    )
                    logging.error(f"❌ {last_error}")
                    await asyncio.sleep(1.0)
                    continue

                logging.info(f"✅ Conectado a {device.address}")

                # Registrar conexión abierta
                conexiones[mac] = client
                break  # salimos del loop de intentos para esta MAC

            except Exception as e:
                last_error = str(e)
                logging.error(
                    f"❌ Error conectando a {mac} en intento {attempt}: {e}"
                )

                if (
                    "InProgress" in last_error
                    or "In Progress" in last_error
                    or "In progress" in last_error
                ):
                    logging.warning(
                        "⏳ BlueZ InProgress: esperando antes de reintentar..."
                    )
                    await asyncio.sleep(1.5)
                else:
                    await asyncio.sleep(1.0)

            finally:
                # Si falló este intento, aseguramos desconexión de este cliente
                if client is not None and mac not in conexiones:
                    try:
                        if client.is_connected:
                            await client.disconnect()
                            logging.info(
                                f"🔌 (finally) Desconectado {mac} después de error"
                            )
                    except Exception as e_disc:
                        logging.debug(
                            f"Error en disconnect (finally) para {mac}: {e_disc}"
                        )

        # --- Después de todos los intentos para esta MAC ---
        if mac not in conexiones:
            if strict:
                logging.error(
                    f"❌ Strict mode: falló la conexión a {mac}, abortando tarea completa"
                )
                # Desconectar todo lo que sí conectó
                for m, c in conexiones.items():
                    try:
                        if c.is_connected:
                            await c.disconnect()
                            logging.info(f"🔌 Desconectado {m}")
                    except Exception:
                        pass
                return {}  # abortar tarea

            else:
                logging.warning(
                    f"⚠️ No se pudo conectar {mac}, continuando (strict=False)"
                )

    return conexiones


async def disconnect_device(client):
    """Desconecta un dispositivo BLE."""
    try:
        if client and client.is_connected:
            await client.disconnect()
            logging.info(f"Disconnected from {client.address}")
    except Exception as e:
        logging.error(f"Error disconnecting from {client.address}: {e}")


async def enviar_comando(conexiones, comando):
    async def operar(client, mac):
        try:
            # Enviar comando
            logging.info(f"📤 Enviando comando a {mac}...")
            await client.write_gatt_char(WRITE_CHAR_UUID, comando)
            logging.info(f"✅ Comando enviado a {mac}")

            # Suscribirse
            logging.info(f"🔔 Subscribiéndose a notificaciones de {mac}")
            await client.start_notify(NOTIFY_CHAR_UUID, generar_handler(mac))

        except Exception as e:
            logging.error(f"❌ Error en operación con {mac}: {e}")

    await asyncio.gather(*[operar(client, mac) for mac, client in conexiones.items()])


async def execute_scheduled_tasks(cookies,gw_id):
    # Obtiene las tareas programadas y las ejecuta con conexión directa preferida sobre escaneo.
    urltasks = f"{API_URL}/scheduletask/tasks/{gw_id}/pending"
    # logging.info("urltasks: %s", urltasks)
    async with httpx.AsyncClient(cookies=cookies, timeout=60.0) as clientApi:
        try:
            response = await clientApi.get(urltasks)
            if response.status_code == 200:
                tasks = response.json()
                logging.info(f"Fetched {len(tasks)} scheduled tasks.")
                for task in tasks:
                    task_id = task['task_id']
                    mac_addresses = set(task['widnode_list']['widnodes'])
                    command_base64 = task['command']
                    strict = task['strict']
                    task_type = task['task_type']

                    restart_bluetooth(logging)
                    try:
                        command = base64.b64decode(command_base64)
                    except Exception as e:
                        logging.error(f"Error decoding Base64 command for task {task_id}: {e}")
                        continue

                    logging.info(f"Attempting direct connection to devices for task {task_id}...")
                    connected_clients = []


                    connected_clients = await conectar_secuencialmente(list(mac_addresses),strict=strict,max_attempts=6)



                    # Si sigue sin encontrarse todos los dispositivos y es estricta, abortar
                    if strict and len(connected_clients) < len(mac_addresses):
                        logging.error(f"Task {task_id} aborted. Not all required devices were found.")
                        continue

                    # Si no hay dispositivos conectados, abortar la tarea
                    if not connected_clients:
                        logging.error(f"No devices connected for task {task_id}. Skipping execution.")
                        continue

                    logging.info(f"Executing task {task_id} for devices: {list(connected_clients.keys())}")


                    logging.info(f"command = {command}")
                    # Escribir comando a los dispositivos conectados
                    # write_tasks = [client.write_gatt_char(WRITE_CHAR_UUID, command) for client in connected_clients.values()]
                    # await asyncio.gather(*write_tasks)
                    await enviar_comando(connected_clients, command)

                    logging.info(f"Command written to devices for task {task_id}.")

                    # Actualizar estado de la tarea
                    urlupdate = f"{API_URL}/scheduletask/tasks/{task_id}?task_type={task_type}"
                    try:
                        response = await clientApi.put(urlupdate)
                        if response.status_code == 200:
                            logging.info(f"Task {task_id} updated successfully.")
                        else:
                            logging.error(f"Error updating task {task_id}: {response.status_code} - {response.text}")
                    except httpx.RequestError as e:
                        logging.error(f"Request error while updating task {task_id}: {e}")

                    # Desconectar dispositivos
                    for client in connected_clients.values():
                        if client.is_connected:
                            logging.info(f"Disconnecting from {client.address}")
                            await client.disconnect()
                        else:
                            logging.info(f"{client.address} was already disconnected.")


            else:
                logging.error(f"Failed to fetch tasks: {response.status_code} - {response.text}")
        except Exception as e:
            logging.error(f"Error executing scheduled tasks: {e}")

# async def find_device_by_address(target_address):
#     found_device = None
    
#     def detection_callback(device, advertisement_data):
#         global rssi_device_connected
#         nonlocal found_device
#         if device.address == target_address:
#             logging.info(f"----- Dispositivo {target_address} encontrado. RSSI: {advertisement_data.rssi}")
#             rssi_device_connected = advertisement_data.rssi
#             found_device = device
#             return True

#     scanner = BleakScanner(detection_callback=detection_callback)

#     try:
#         await scanner.start()
#         for _ in range(60 * 10):
#             if found_device:
#                 break
#             await asyncio.sleep(0.1)
#     finally:
#         await scanner.stop()

#     return found_device

# async def connect_device_directly(mac_address, retries=3, delay=5, cooldown=3):
#     """
#     Intenta conectar a un dispositivo BLE con un tiempo límite, reintentos y mejor manejo de errores.
    
#     Args:
#         mac_address (str): Dirección MAC del dispositivo BLE.
#         retries (int): Número de intentos de conexión.
#         delay (int): Tiempo de espera entre intentos en segundos.
#         cooldown (int): Tiempo de espera adicional en caso de `Error.InProgress`.

#     Returns:
#         BleakClient: Cliente conectado o None si falla la conexión.
#     """
#     for attempt in range(retries):
#         client = None  # ✅ garantiza que esté definido
#         async with semaphore:
#             try:
#                 logging.info(f"Attempting connection to {mac_address} (Attempt {attempt+1}/{retries})")

#                 client = BleakClient(mac_address)

#                 # Intentar conectar con un tiempo límite
#                 try:
#                     await asyncio.wait_for(client.connect(), timeout=60)
#                 except asyncio.TimeoutError:
#                     logging.error(f"Connection attempt to {mac_address} timed out.")
#                     continue

#                 if not client.is_connected:
#                     raise Exception(f"Failed to connect to {mac_address} on attempt {attempt+1}")

#                 logging.info(f"Connected successfully to {mac_address}")

#                 # Suscribirse a notificaciones
#                 async def notification_handler(sender, data):
#                     logging.info(f"Notification from {mac_address}: {data}")

#                 await client.start_notify(NOTIFY_CHAR_UUID, notification_handler)
#                 logging.info(f"Notifications enabled for {mac_address}")

#                 return client  # Devuelve el cliente conectado

#             except Exception as e:
#                 error_message = str(e)

#                 if "org.bluez.Error.InProgress" in error_message:
#                     logging.warning(f"Connection to {mac_address} already in progress. Waiting {cooldown} seconds...")
#                     await asyncio.sleep(cooldown)
#                     continue  # Reintentar sin contar como fallo

#                 logging.error(f"Error connecting to {mac_address}: {e}")

#                 # Asegurar desconexión si hubo un fallo
#                 try:
#                     if client.is_connected:
#                         await client.disconnect()
#                 except Exception as disconnect_error:
#                     logging.warning(f"Error disconnecting {mac_address}: {disconnect_error}")

#                 if attempt < retries - 1:
#                     logging.info(f"Retrying in {delay} seconds...")
#                     await asyncio.sleep(delay)
    
#     logging.error(f"Failed to connect to {mac_address} after {retries} attempts.")
#     return None  # Retorna None si no logró conectarse

