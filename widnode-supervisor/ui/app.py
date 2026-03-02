import os
import subprocess
from flask import Flask, render_template, request, redirect, url_for, jsonify,flash
import configparser
import asyncio
from bleak import BleakScanner
import shutil
import zipfile
import platform
# from cryptography.fernet import Fernet
from datetime import datetime
import traceback
from werkzeug.utils import secure_filename
from pathlib import Path


app = Flask(__name__)

# Define paths
app.secret_key = 'supersecretkey'

# Constantes
APP_DIR = "/home/widnode/dev/widnode"
BACKUP_DIR = "/home/widnode/dev/widnode_backup"
UPLOAD_DIR = "/home/widnode/dev/widnode_ap"
# APP_DIR = r"N:\STM32\RADXA\simulacion\widnode"
# BACKUP_DIR = r"N:\STM32\RADXA\simulacion\widnode_backup"
# UPLOAD_DIR = r"N:\STM32\RADXA\simulacion\widnode_ap"
FILES_TO_BACKUP = ["app.py", "command_process.py", "utils.py", "widnode_signal.py"]
# FERNET_KEY = b'WSsZTPSF9g3OBr5VUPwEKK4N8x4C312s2xm1O1aGV2w='  # ESTA ES TU CLAVE

CONFIG_FILE = os.getenv("CONFIG_FILE", "/data/config.ini")

RESTART_FLAG = "/data/restart.flag"

# --- al comienzo, junto a constantes ---
# ALLOWED_SECTIONS = {"WiFi", "Ethernet", "LTE-4G", "API","MODBUS_SERVER"}
ALLOWED_SECTIONS = ["WiFi", "Ethernet", "LTE-4G", "API","MODBUS_SERVER", "Tachometer"]


@app.post("/restart")
def restart_container():
    try:
        Path(RESTART_FLAG).touch()
        return jsonify({"ok": True, "message": "Reiniciando servicio..."}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/healthz")
def healthz():
    return {"ok": True}, 200

# def make_backup():
#     timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
#     zip_path = os.path.join(BACKUP_DIR, f"widnode_backup_{timestamp}.zip")
#     enc_path = zip_path.replace(".zip", ".enc")

#     # Crear el ZIP
#     with zipfile.ZipFile(zip_path, 'w') as zipf:
#         for filename in FILES_TO_BACKUP:
#             src = os.path.join(APP_DIR, filename)
#             if os.path.exists(src):
#                 zipf.write(src, arcname=filename)

#     # Encriptar con Fernet
#     with open(zip_path, 'rb') as f:
#         zip_data = f.read()

#     fernet = Fernet(FERNET_KEY)
#     encrypted_data = fernet.encrypt(zip_data)

#     with open(enc_path, 'wb') as f:
#         f.write(encrypted_data)

#     os.remove(zip_path)  # Eliminar el zip sin cifrar

#     print(f"✅ Backup cifrado creado: {enc_path}")
#     return enc_path  # Devolvemos el path para borrarlo si falla


# def decrypt_and_unzip(file_path, output_dir, key):
#     print("🔐 Leyendo archivo cifrado...")
#     with open(file_path, 'rb') as encrypted_file:
#         encrypted_data = encrypted_file.read()

#     fernet = Fernet(key)
#     print("🔐 Desencriptando datos...")
#     decrypted_data = fernet.decrypt(encrypted_data)

#     temp_zip = os.path.join(output_dir, "decrypted.zip")
#     print(f"📦 Guardando ZIP temporal: {temp_zip}")
#     with open(temp_zip, 'wb') as f:
#         f.write(decrypted_data)

#     print("📦 Extrayendo archivos...")
#     with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
#         zip_ref.extractall(output_dir)

# def apply_update(encrypted_file_path):
#     backup_path = None
#     temp_extract_dir = os.path.join(UPLOAD_DIR, "temp_extract")

#     try:
#         print("🔐 Iniciando backup...")
#         backup_path = make_backup()

#         os.makedirs(temp_extract_dir, exist_ok=True)

#         print("🔓 Desencriptando y descomprimiendo...")
#         decrypt_and_unzip(encrypted_file_path, temp_extract_dir, FERNET_KEY)

#         print("📂 Copiando archivos actualizados...")
#         for filename in FILES_TO_BACKUP:
#             src = os.path.join(temp_extract_dir, filename)
#             dst = os.path.join(APP_DIR, filename)

#             if os.path.exists(src):
#                 try:
#                     shutil.copy(src, dst)
#                     print(f"✅ Copiado: {filename}")
#                 except Exception as e:
#                     print(f"⚠️ No se pudo copiar {filename}: {e}")
#             else:
#                 print(f"⚠️ Archivo no encontrado en el zip: {filename}")

#         print("🧹 Limpieza temporal completada.")
#         shutil.rmtree(temp_extract_dir, ignore_errors=True)

#     except Exception as e:
#         print("❌ Error durante apply_update:")
#         traceback.print_exc()

#         if backup_path and os.path.exists(backup_path):
#             os.remove(backup_path)
#             print(f"🗑️ Backup eliminado: {backup_path}")

#         raise  # vuelve a lanzar la excepción para rollback o notificación

# def restore_from_backup():
#     print("♻️ Iniciando restauración desde backup...")

#     # Buscar el último archivo .enc en el directorio de backup
#     backups = sorted(
#         [f for f in os.listdir(BACKUP_DIR) if f.endswith(".enc")],
#         reverse=True
#     )

#     if not backups:
#         print("⚠️ No hay backups disponibles.")
#         return False

#     latest_backup = backups[0]
#     enc_path = os.path.join(BACKUP_DIR, latest_backup)

#     # Leer y descifrar el archivo
#     with open(enc_path, 'rb') as f:
#         encrypted_data = f.read()

#     fernet = Fernet(FERNET_KEY)
#     try:
#         decrypted_data = fernet.decrypt(encrypted_data)
#     except Exception as e:
#         print(f"❌ Error al descifrar backup: {e}")
#         return False

#     # Escribir el ZIP temporal
#     temp_zip = os.path.join(BACKUP_DIR, "temp_restore.zip")
#     with open(temp_zip, 'wb') as f:
#         f.write(decrypted_data)

#     # Extraer a carpeta temporal
#     temp_restore_dir = os.path.join(BACKUP_DIR, "temp_restore")
#     os.makedirs(temp_restore_dir, exist_ok=True)

#     with zipfile.ZipFile(temp_zip, 'r') as zipf:
#         zipf.extractall(temp_restore_dir)

#     # Copiar los archivos restaurados a APP_DIR
#     for filename in FILES_TO_BACKUP:
#         src = os.path.join(temp_restore_dir, filename)
#         dst = os.path.join(APP_DIR, filename)

#         if os.path.exists(src):
#             shutil.copy2(src, dst)
#             print(f"✅ Restaurado: {filename}")
#         else:
#             print(f"⚠️ Faltante en backup: {filename}")

#     # Limpieza
#     os.remove(temp_zip)
#     shutil.rmtree(temp_restore_dir, ignore_errors=True)

#     print("✅ Restauración completada con éxito.")
#     return True

# @app.route("/upload_update", methods=["POST"])
# def upload_update():
#     if 'update_file' not in request.files:
#         flash("Archivo no encontrado.")
#         return redirect(request.referrer)
    
#     file = request.files['update_file']
#     if file.filename == '':
#         flash("Nombre de archivo inválido.")
#         return redirect(request.referrer)

#     upload_path = os.path.join(UPLOAD_DIR, secure_filename(file.filename))
#     file.save(upload_path)
    
#     try:
#         print("🔄 Iniciando actualización con archivo:", upload_path)
#         apply_update(upload_path)
#         flash("✅ Actualización aplicada correctamente.")
#     except Exception as e:
#         rollback()
#         flash(f"❌ Error durante la actualización: {str(e)}")
#         print("❌ Error durante apply_update:", e)

#     return redirect("/")

# def rollback():
#     for filename in FILES_TO_BACKUP:
#         src = os.path.join(BACKUP_DIR, filename)
#         dst = os.path.join(APP_DIR, filename)

#         if os.path.exists(src):
#             try:
#                 shutil.copy(src, dst)
#                 print(f"♻️ Restaurado: {filename}")
#             except Exception as e:
#                 print(f"⚠️ Error al restaurar {filename}: {e}")
#         else:
#             print(f"⚠️ Archivo faltante en el backup: {filename}")

def read_config():
    config = configparser.RawConfigParser()
    config.read(CONFIG_FILE)
    return config

def read_config_filtered():
    """Devuelve solo las secciones permitidas para la UI."""
    cfg = read_config()
    view = {}
    for s in ALLOWED_SECTIONS:
        if cfg.has_section(s):
            view[s] = dict(cfg.items(s))  # dict llano para Jinja
    return view

def write_config(config):
    with open(CONFIG_FILE, 'w') as configfile:
        config.write(configfile)

# @app.route("/restore_backup", methods=["POST"])
# def restore_backup():
#     success = restore_from_backup()
#     if success:
#         flash("✅ Backup restaurado correctamente.")
#     else:
#         flash("❌ Error al restaurar el backup.")
#     return redirect("/")
# def restore_from_specific_backup(enc_path):
#     print(f"♻️ Restaurando desde: {enc_path}")

#     try:
#         with open(enc_path, 'rb') as f:
#             encrypted_data = f.read()

#         fernet = Fernet(FERNET_KEY)
#         decrypted_data = fernet.decrypt(encrypted_data)

#         temp_zip = os.path.join(BACKUP_DIR, "temp_restore.zip")
#         with open(temp_zip, 'wb') as f:
#             f.write(decrypted_data)

#         temp_restore_dir = os.path.join(BACKUP_DIR, "temp_restore")
#         os.makedirs(temp_restore_dir, exist_ok=True)

#         with zipfile.ZipFile(temp_zip, 'r') as zipf:
#             zipf.extractall(temp_restore_dir)

#         for filename in FILES_TO_BACKUP:
#             src = os.path.join(temp_restore_dir, filename)
#             dst = os.path.join(APP_DIR, filename)
#             if os.path.exists(src):
#                 shutil.copy2(src, dst)
#                 print(f"✅ Restaurado: {filename}")
#             else:
#                 print(f"⚠️ Faltante: {filename}")

#         os.remove(temp_zip)
#         shutil.rmtree(temp_restore_dir, ignore_errors=True)

#         return True

#     except Exception as e:
#         print(f"❌ Error restaurando backup: {e}")
#         return False


@app.route('/')
def editor():
    # config = read_config()
    config = read_config_filtered()
    available_timezones = get_available_timezones()
    # current_timezone = get_current_timezone()
    current_timezone = load_current_timezone()
    return render_template('editor.html', config=config, timezones=available_timezones, current_timezone=current_timezone)

@app.route("/backups", methods=["GET"])
def list_backups():
    backups = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".enc")],
        reverse=True
    )

    parsed_backups = []

    for f in backups:
        try:
            # Ejemplo: widnode_backup_20250625_110213.enc
            base = f.replace(".enc", "").replace("widnode_backup_", "")
            dt = datetime.strptime(base, "%Y%m%d_%H%M%S")
            fecha_legible = dt.strftime("%d de %B de %Y - %H:%M:%S")
        except Exception:
            fecha_legible = "Fecha desconocida"

        parsed_backups.append({
            "filename": f,
            "fecha": fecha_legible
        })

    return render_template("backups.html", backups=parsed_backups)

# @app.route("/restore_backup/<filename>", methods=["POST"])
# def restore_specific_backup(filename):
#     enc_path = os.path.join(BACKUP_DIR, filename)
#     if not os.path.exists(enc_path):
#         flash("❌ Backup no encontrado.")
#         return redirect("/backups")

#     success = restore_from_specific_backup(enc_path)
#     if success:
#         flash(f"✅ Backup restaurado: {filename}")
#     else:
#         flash(f"❌ Error al restaurar: {filename}")
#     return redirect("/backups")

@app.route('/mantenimiento', methods=['GET', 'POST'])
def mantenimiento():
    mensaje = ''
    if request.method == 'POST':
        accion = request.form.get('accion')
        try:
            if accion == 'restart-ssh':
                subprocess.run(['systemctl', 'restart', 'ssh'], check=True)
                mensaje = 'Servicio SSH reiniciado correctamente.'
            elif accion == 'gateway-mode':
                subprocess.run(['systemctl', 'restart', 'check_ap.service'], check=True)
                mensaje = 'Modo Gateway activado (check_ap.service reiniciado).'
            elif accion == 'run-command':
                comando_raw = request.form.get('comando', '').strip()
                if comando_raw.startswith("wIDNode"):
                    comando = comando_raw[len("wIDNode"):]
                    try:
                        salida = subprocess.check_output(comando, shell=True, stderr=subprocess.STDOUT, text=True, timeout=10)
                    except subprocess.CalledProcessError as e:
                        salida = f"[ERROR]\n{e.output}"
                    except Exception as e:
                        salida = f"[EXCEPCIÓN] {str(e)}"
                else:
                    salida = "⚠️ Comando rechazado: debe comenzar con 'wIDNode'"    
            else:
                mensaje = 'Acción no reconocida.'
        except subprocess.CalledProcessError as e:
            mensaje = f'Error al ejecutar la acción: {str(e)}'

    estado_red = obtener_estado_red()
    return render_template('mantenimiento.html', mensaje=mensaje, estado_red=estado_red, salida=salida if 'salida' in locals() else '')

def obtener_estado_red():
    estado = {}

    try:
        estado['ip'] = subprocess.check_output(['ip', 'addr'], universal_newlines=True)
    except subprocess.CalledProcessError as e:
        estado['ip'] = f"Error al obtener 'ip addr': {str(e)}"

    try:
        estado['nmcli'] = subprocess.check_output(
            ['nmcli', 'device', 'status'],
            universal_newlines=True
        )
    except subprocess.CalledProcessError as e:
        estado['nmcli'] = f"Error al obtener 'nmcli device': {str(e)}"

    return estado

@app.route('/reboot', methods=['POST'])
def reboot():
    try:
        subprocess.Popen(['sudo', 'reboot'])  # Usamos Popen para no esperar el reinicio
        return "Rebooting...", 200
    except Exception as e:
        return f"Failed to reboot: {str(e)}", 500
        
@app.route('/update', methods=['POST'])
def update():
    config = read_config()

    # Lista de secciones esperadas
    for section in ALLOWED_SECTIONS:
        if not config.has_section(section):
            config.add_section(section)

        # --- Enabled (por checkbox principal de cada sección) ---
        enabled_key = f"{section}-enabled"
        enabled_value = "true" if enabled_key in request.form else "false"
        config[section]["enabled"] = enabled_value

        # --- Guardar cada valor del formulario ---
        for form_key, value in request.form.items():
            if not form_key.startswith(f"{section}-"):
                continue

            key = form_key.split(f"{section}-", 1)[1]

            # ya procesado arriba
            if key == "enabled":
                continue

            # Normalizar valores
            val = value.strip()

            # Checkbox vienen como "on"
            if val.lower() == "on":
                val = "true"

            config[section][key] = val

        # --- Detectar checkboxes desmarcados ---
        checkbox_keys = ["internet", "hidden", "failover", "dhcp", "static"]
        for ck in checkbox_keys:
            form_key = f"{section}-{ck}"
            if form_key not in request.form:
                # si la sección tiene ese campo, o lo creamos como false
                config[section][ck] = "false"

    write_config(config)
    return redirect(url_for("editor"))



@app.route('/update-timezone', methods=['POST'])
def update_timezone():
    import configparser
    new_timezone = request.form.get('timezone', 'UTC')

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    if 'System' not in cfg:
        cfg['System'] = {}
    cfg['System']['timezone'] = new_timezone

    with open(CONFIG_FILE, 'w') as f:
        cfg.write(f)

    # no tocamos el host; el entrypoint tomará esta TZ en el próximo arranque
    return ("", 204)


@app.route('/scan-bluetooth', methods=['GET'])
def scan_bluetooth():
    devices = asyncio.run(scan_bluetooth_devices())
    return jsonify(devices)

async def scan_bluetooth_devices():
    devices_dict = {}
    devices = await BleakScanner.discover(timeout=60)
    for device in devices:
        if device.name == "wIDNodeVib":
            devices_dict[device.address] = {
                'name': device.name,
                'RSSI': getattr(device, "rssi", "N/A")  # Compatible con bleak 1.0.1
            }
    return devices_dict

# def get_available_timezones():
#     output = subprocess.check_output(['timedatectl', 'list-timezones'], universal_newlines=True)
#     return output.splitlines()
# def get_available_timezones():
#     if platform.system() == "Windows":
#         # Simulación para desarrollo en Windows
#         return [
#             "UTC",
#             "America/Argentina/Buenos_Aires",
#             "Europe/Madrid",
#             "Asia/Tokyo"
#         ]
#     try:
#         output = subprocess.check_output(['timedatectl', 'list-timezones'], universal_newlines=True)
#         return output.strip().split('\n')
#     except Exception as e:
#         print(f"Error obteniendo timezones: {e}")
#         return ["UTC"]
# ====== LISTA DE TIMEZONES ======
def load_current_timezone():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg.get("System", "timezone", fallback=os.environ.get("TZ","UTC"))

def get_available_timezones():
    if platform.system() == "Windows":
        return [
            "UTC",
            "America/Argentina/Buenos_Aires",
            "Europe/Madrid",
            "Asia/Tokyo",
        ]
    # 1) intentar con 'timedatectl list-timezones' (no requiere sudo)
    try:
        out = subprocess.check_output(
            ["timedatectl", "list-timezones"], universal_newlines=True
        )
        tzs = [line.strip() for line in out.splitlines() if line.strip()]
        if tzs:
            return tzs
    except Exception:
        pass
    # 2) fallback robusto: recorrer /usr/share/zoneinfo
    tzs = []
    zroot = "/usr/share/zoneinfo"
    skip_top = {"posix", "right", "SystemV"}
    for root, dirs, files in os.walk(zroot):
        rel = os.path.relpath(root, zroot)
        top = rel.split(os.sep, 1)[0] if rel != "." else ""
        if top in skip_top:
            dirs[:] = []
            continue
        for f in files:
            # descartá archivos que no son zonas
            if f in ("zone.tab", "zone1970.tab", "leap-seconds.list"):
                continue
            relpath = f if rel == "." else f"{rel}/{f}"
            tzs.append(relpath)
    tzs.sort()
    return tzs
    
# def get_current_timezone():
#     output = subprocess.check_output(['timedatectl', 'show', '--property=Timezone'], universal_newlines=True)
#     return output.strip().split('=')[-1]
# ====== LECTURA DE TIMEZONE ACTUAL ======
def get_current_timezone():
    # a) vía D-Bus (host systemd-timedated)
    try:
        return _get_current_timezone_dbus()
    except Exception:
        pass
    # b) Debian-like
    try:
        with open("/etc/timezone", "r") as fh:
            return fh.read().strip()
    except Exception:
        pass
    # c) symlink /etc/localtime -> .../zoneinfo/Region/City
    try:
        target = os.path.realpath("/etc/localtime")
        idx = target.find("/zoneinfo/")
        if idx != -1:
            return target[idx + len("/zoneinfo/") :]
    except Exception:
        pass
    return "UTC"
    
def _get_current_timezone_dbus():
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType

    async def _run():
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introsp = await bus.introspect("org.freedesktop.timedate1", "/org/freedesktop/timedate1")
        obj = bus.get_proxy_object("org.freedesktop.timedate1", "/org/freedesktop/timedate1", introsp)
        props = obj.get_interface("org.freedesktop.DBus.Properties")
        tz = await props.call_get("org.freedesktop.timedate1", "Timezone")
        bus.disconnect()
        return tz.value

    return asyncio.run(_run())

# ====== SET DE TIMEZONE (host) ======
def set_system_timezone(new_tz: str):
    """
    Devuelve True si se pudo, False si falló.
    Cambia el timezone del HOST vía D-Bus (org.freedesktop.timedate1).
    """
    try:
        _set_timezone_dbus(new_tz)
        return True
    except Exception as e:
        print(f"[timezone] D-Bus set failed: {e}")
        return False

def _set_timezone_dbus(new_tz: str):
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType

    async def _run():
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introsp = await bus.introspect("org.freedesktop.timedate1", "/org/freedesktop/timedate1")
        obj = bus.get_proxy_object("org.freedesktop.timedate1", "/org/freedesktop/timedate1", introsp)
        iface = obj.get_interface("org.freedesktop.timedate1")
        # signature: SetTimezone(in s timezone, in b interactive)
        await iface.call_set_timezone(new_tz, False)
        bus.disconnect()

    return asyncio.run(_run())


if __name__ == '__main__':
    # app.run(host='0.0.0.0', port=8080, debug=True)
    app.run(host='0.0.0.0', port=8080, debug=False, use_reloader=False)
    # try:
    #     app.run(host='0.0.0.0', port=5020, debug=True)
    # except PermissionError as e:
    #     print("❌ Permiso denegado para abrir el puerto. Probá como administrador.")
    # except OSError as e:
    #     print(f"❌ Error de sistema: {e}")
