import os, re, sys,shlex, time, signal, subprocess, configparser
from pathlib import Path

import json

BLINK_FILE = "/data/led_blink.json"
BLINK_FREQ_HZ_DEFAULT = 2.0

_blink_until = 0.0
_blink_leds = set()  # {"lan", "lte", "wifi", "err"}
_blink_freq = BLINK_FREQ_HZ_DEFAULT


# === GPIO y config ===
GPIOCHIP = os.getenv("GPIOCHIP", "/dev/gpiochip0")
GPIOLINE = os.getenv("GPIOLINE", "6")
CONFIG_PATH = os.getenv("CONFIG_PATH", "/data/config.ini")
# arriba, opcional: control por env
KEEP_CLIENT_ON_STOP = os.getenv("KEEP_CLIENT_ON_STOP", "1") == "1"

ETH_IF  = os.getenv("ETH_IF",  "ethernet0")
WIFI_IF = os.getenv("WIFI_IF", "mlan0")

# Prioridades de ruta (menor métrica = mayor prioridad)
METRIC_LTE  = int(os.getenv("METRIC_LTE",  "50"))
METRIC_ETH  = int(os.getenv("METRIC_ETH",  "100"))
METRIC_WIFI = int(os.getenv("METRIC_WIFI", "200"))


POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SEC", "1.0"))
DEBOUNCE_SAMPLES = int(os.getenv("DEBOUNCE_SAMPLES", "3"))
DEBOUNCE_DELAY = float(os.getenv("DEBOUNCE_DELAY_SEC", "0.1"))

RESTART_FLAG = Path("/data/restart.flag")

GPIO_HOLDOFF_S = 4  # segundos mínimos entre cambios de modo
_last_switch_ts = 0


# === AP: una sola vía: script dedicado que usa SÓLO variables de entorno ===
AP_CMD = "/opt/app/ap_stack.sh"

ap_proc = None
app_proc = None
current_mode = None  # "AP" | "CLIENT"
_led_eth = None
_led_lte = None
_led_wifi = None   # nuevo
_led_err  = None   # nuevo

_last_eth = None
_last_lte = None
_last_wifi = None  # nuevo
_last_err  = None  # nuevo
tach_proc = None

def _unset_autoconnect_and_delete(name: str):
    # Desactiva autoconnect y borra TODAS las conexiones con ese NAME
    out = run_out(["nmcli","-t","-f","NAME,UUID","connection","show"])
    for line in out.splitlines():
        nm, uuid = (line.split(":")+["",""])[:2]
        if nm == name and uuid:
            run(["nmcli","con","mod",uuid,"connection.autoconnect","no"])
            run(["nmcli","con","delete","uuid",uuid])

def _reapply_or_up(name: str, dev: str) -> int:
    # 1) probá reapply (rápido si ya está activa)
    rc = run(["nmcli", "device", "reapply", dev])
    if rc == 0:
        return 0
    # 2) si reapply falla porque el device no está activo, levantá la conexión
    return run(["nmcli", "-w", "20", "con", "up", name])

def run(args: list[str]) -> int:
    cp = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if cp.returncode != 0:
        tag = "[NMCLI][ERR]" if args and args[0] == "nmcli" else "[CMD][ERR]"
        print(f"{tag} cmd={' '.join(args)}\n{cp.stdout.strip()}")
    return cp.returncode

def run_out(args: list[str]) -> str:
    cp = subprocess.run(args, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return cp.stdout


def _nm_dev_state(iface: str) -> str:
    # Devuelve "100 (connected)" / "30 (disconnected)" etc.
    out = run_out(["nmcli", "-t", "-f", "GENERAL.STATE", "device", "show", iface]).strip()
    return out

def _nm_dev_is_active(iface: str) -> bool:
    st = _nm_dev_state(iface)
    return st.startswith("100")  # 100 (connected)

def _nm_con_is_active(name: str) -> bool:
    out = run_out(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"])
    return any(line.strip() == name for line in out.splitlines())

def _nm_con_exists(name: str) -> bool:
    out = run_out(["nmcli", "-t", "-f", "NAME", "connection", "show"])
    return any(line.strip() == name for line in out.splitlines())


def _nm_active(name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"],
            text=True, stderr=subprocess.DEVNULL
        )
        return any(line.strip() == name for line in out.splitlines())
    except Exception:
        return False

def _iface_has_ipv4(dev: str) -> bool:
    try:
        out = subprocess.check_output(["ip","-br","-4","addr","show",dev], text=True)
        return bool(re.search(r"\d+\.\d+\.\d+\.\d+", out))
    except Exception:
        return False

def _lan_connected_strict() -> bool:
    # considera "activa" si eth-client o wifi-client están activos Y hay IP
    if _nm_active("eth-client") and _iface_has_ipv4(ETH_IF):
        return True
    # if _nm_active("wifi-client") and _iface_has_ipv4(WIFI_IF):
    #     return True
    return False

def _wifi_connected_strict() -> bool:
    # activa si wifi-client está activo y tiene IPv4
    if _nm_active("wifi-client") and _iface_has_ipv4(WIFI_IF):
        return True
    return False

def _lte_connected_strict() -> bool:
    # 1) perfil activo lte-client
    if _nm_active("lte-client"):
        return True
    # 2) fallback: MM conectado
    mid = get_modem_index()
    if mid and modem_is_connected(mid):
        return True
    return False

def _apply_failover_policy(cfg):
    failover  = cfg.getboolean("General","failover", fallback=True)
    preferred = cfg.get("General","preferred", fallback="").strip().lower()
    if failover or not preferred:
        return
    lte_sec = _lte_section(cfg)
    roles = {
        "ethernet": ("eth-client", cfg.getboolean("Ethernet","internet",fallback=False)),
        "wifi":     ("wifi-client", cfg.getboolean("WiFi","internet",fallback=False)),
        "lte":      ("lte-client",  cfg.getboolean(lte_sec,"internet",fallback=False)),
    }
    for key, (name, has_inet) in roles.items():
        if has_inet and key != preferred:
            nm_modify(name, "ipv4.never-default", "yes")
            # nm_modify(name, "-ipv4.route-metric")


def read_tach_config():
    cfg_path = Path(os.getenv("CONFIG_FILE", "/data/config.ini"))
    config = configparser.ConfigParser()
    config.read(cfg_path)
    section = config["Tachometer"] if "Tachometer" in config else {}
    enabled = section.get("enabled", "false").strip().lower() == "true"

    # variables opcionales que quieras heredar al entorno
    env = {
        "TACH_PPR": section.get("ppr", "1"),
        "TACH_AVG_WINDOW_SEC": section.get("avg_window_sec", "2"),
        "TACH_LOG_PERIOD_SEC": section.get("log_period_sec", "10"),
        "TACH_KEEP_DAYS": section.get("keep_days", "14"),
        "TACH_BASENAME": section.get("basename", "tacho"),
        "TACH_DIR": section.get("dir", "/data/tacho"),
    }
    return enabled, env

def _check_blink_trigger():
    global _blink_until, _blink_leds, _blink_freq
    try:
        if not os.path.exists(BLINK_FILE):
            return

        with open(BLINK_FILE, "r") as f:
            cfg = json.load(f)

        dur = float(cfg.get("duration", 5))
        _blink_freq = float(cfg.get("freq_hz", BLINK_FREQ_HZ_DEFAULT))

        leds = cfg.get("leds", ["lan", "lte"])
        _blink_leds = set(leds)

        # NEW: oneshot controla si se borra el archivo (default True para no romper tu comportamiento actual)
        oneshot = bool(cfg.get("oneshot", True))

        # Si duration <= 0 => blink "indefinido" mientras exista el archivo (modo persistente)
        if dur <= 0:
            _blink_until = float("inf")
        else:
            _blink_until = time.time() + max(0.5, dur)

        if oneshot:
            os.remove(BLINK_FILE)

        print(f"[LED][BLINK] leds={_blink_leds} dur={dur}s f={_blink_freq}Hz oneshot={oneshot}")

    except Exception as e:
        print(f"[LED][BLINK][ERR] {e}")



    
def start_tach_if_enabled():
    enabled, env_from_ini = read_tach_config()
    if not enabled:
        print("[TACH] Deshabilitado en config.ini")
        return None

    # Evitar duplicados
    try:
        out = subprocess.check_output(["pgrep", "-fa", "tach_logger.py"], text=True)
        if out.strip():
            print("[TACH] ya estaba corriendo:\n" + out)
            return None
    except Exception:
        pass

    cmd = ["/usr/bin/python3", "/opt/app/tach_logger.py"]
    env = os.environ.copy()
    env.update(env_from_ini)

    try:
        p = subprocess.Popen(cmd, env=env)
        print(f"[TACH] Iniciado (log cada {env['TACH_LOG_PERIOD_SEC']}s, win={env['TACH_AVG_WINDOW_SEC']}s, keep={env['TACH_KEEP_DAYS']}d)")
        return p
    except Exception as e:
        print(f"[TACH][ERR] no pude iniciar tachometer: {e}")
        return None


# --- LED manager (compatible v1/v2; pensado para v1 de Debian/Torizon) ---

try:
    import gpiod
except Exception:
    gpiod = None

def _is_v2():
    return bool(gpiod) and hasattr(gpiod, "RequestConfig") and hasattr(gpiod, "LineSettings")

def _chip_open(chip_path: str):
    # API vieja: gpiod.chip(); API nueva: gpiod.Chip()
    if hasattr(gpiod, "chip"):
        return gpiod.chip(chip_path)
    return gpiod.Chip(chip_path)

class _Led:
    def __init__(self, chip_path: str, line_offset: int, active_low: bool, name: str):
        if gpiod is None:
            raise RuntimeError("python3-libgpiod no disponible en la imagen")

        self._active_low = bool(active_low)
        self._name = name
        self._v2 = _is_v2()

        if self._v2:
            # -------- libgpiod v2 --------
            self._chip = _chip_open(chip_path)
            req = gpiod.RequestConfig()
            req.consumer = f"widnode-{name}"
            req.line_offsets = [line_offset]

            settings = gpiod.LineSettings()
            settings.direction = gpiod.LineDirection.OUTPUT
            settings.active_low = self._active_low

            self._lines = self._chip.request_lines(req, settings)
            # apaga por defecto (0 lógico; active_low lo invierte)
            self._lines.set_values([0])
        else:
            # -------- libgpiod v1 (API antigua) --------
            self._chip = _chip_open(chip_path)
            self._line = self._chip.get_line(line_offset)

            # solicitar la línea como salida (solo posicionales; sin kwargs)
            DIR_OUT = getattr(gpiod, "LINE_REQ_DIR_OUT", 1)
            self._line.request(f"widnode-{name}", DIR_OUT)

            # valor inicial manual (sin default_vals)
            initial = 0 if not self._active_low else 1
            try:
                self._line.set_value(initial)
            except Exception:
                # bindings muy viejos usan set_values([v])
                if hasattr(self._line, "set_values"):
                    self._line.set_values([initial])
                else:
                    raise

    def set(self, on: bool):
        if self._v2:
            self._lines.set_values([1 if on else 0])
        else:
            val = 1 if on else 0
            if self._active_low:
                val = 0 if on else 1
            try:
                self._line.set_value(val)
            except Exception:
                if hasattr(self._line, "set_values"):
                    self._line.set_values([val])
                else:
                    raise

    def close(self):
        try:
            if self._v2:
                self._lines.release()
                if hasattr(self._chip, "close"):
                    self._chip.close()
            else:
                self._line.release()
                if hasattr(self._chip, "close"):
                    self._chip.close()
        except Exception:
            pass

def _mk_led(prefix: str, name: str):
    mode = os.getenv(f"{prefix}_LED_MODE", "gpio").strip().lower()

    if mode == "pwm":
        pwmchip = os.getenv(f"{prefix}_PWMCHIP")
        pwmchan = os.getenv(f"{prefix}_PWMCHAN")
        if pwmchip is None or pwmchan is None:
            return None

        period_ns = int(os.getenv(f"{prefix}_PWM_PERIOD_NS", "20000000"))
        duty_pct  = float(os.getenv(f"{prefix}_PWM_DUTY_ON_PCT", "100"))
        invert    = os.getenv(f"{prefix}_PWM_INVERT", "0") == "1"
        return _PwmLed(int(pwmchip), int(pwmchan), name, period_ns, duty_pct, invert)

    # default: gpio
    chip = os.getenv(f"{prefix}_LED_CHIP", "/dev/gpiochip0")
    line = os.getenv(f"{prefix}_LED_LINE")
    if not line:
        return None

    per_led = os.getenv(f"{prefix}_LED_ACTIVE_LOW")
    if per_led is None:
        active_low = os.getenv("LED_ACTIVE_LOW", "false").lower() == "true"
    else:
        active_low = per_led.lower() == "true"

    return _Led(chip, int(line), active_low, name)

# --- fin LED manager ---
# --- PWM LED (sysfs) ---
class _PwmLed:
    """
    LED manejado por PWM usando sysfs:
      /sys/class/pwm/pwmchipX/pwmY/{period,duty_cycle,enable}
    Encendido = duty 100% (o el porcentaje configurado).
    Apagado   = duty 0%.
    """
    def __init__(self, pwmchip: int, channel: int, name: str,
                 period_ns: int = 20_000_000,  # 20ms (50Hz) - suficiente para LED
                 duty_on_pct: float = 100.0,
                 invert: bool = False):
        self._name = name
        self._chip = int(pwmchip)
        self._ch = int(channel)
        self._period = int(period_ns)
        self._duty_on = max(0.0, min(100.0, float(duty_on_pct)))
        self._invert = bool(invert)

        self._base = f"/sys/class/pwm/pwmchip{self._chip}"
        self._pwm = f"{self._base}/pwm{self._ch}"

        # Export (si no existe)
        if not os.path.exists(self._pwm):
            with open(f"{self._base}/export", "w") as f:
                f.write(str(self._ch))

        # Configurar periodo y arrancar apagado
        self._write("enable", "0")
        self._write("period", str(self._period))
        self.set(False)              # duty=0
        self._write("enable", "1")   # habilitar salida

    def _write(self, leaf: str, value: str):
        path = f"{self._pwm}/{leaf}"
        with open(path, "w") as f:
            f.write(value)

    def set(self, on: bool):
        on = bool(on)
        if self._invert:
            on = not on

        if on:
            duty = int(self._period * (self._duty_on / 100.0))
        else:
            duty = 0
        self._write("duty_cycle", str(duty))

    def close(self):
        try:
            self._write("enable", "0")
        except Exception:
            pass
        # opcional: unexport (yo lo dejaría comentado para evitar “flapping”)
        # try:
        #     with open(f"{self._base}/unexport", "w") as f:
        #         f.write(str(self._ch))
        # except Exception:
        #     pass

def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()

def get_modem_index() -> str | None:
    """
    Política:
    1) Si hay env LTE_MODEM_INDEX, úsalo.
    2) Si hay env LTE_MODEM_MATCH (regex), elegí el primero cuyo descriptor haga match.
    3) Si no, devolvé el primer índice que reporte `mmcli -L`.
    """
    forced = os.getenv("LTE_MODEM_INDEX")
    if forced and forced.isdigit():
        return forced

    try:
        listing = _run(["mmcli", "-L"])  # e.g. "/org/freedesktop/ModemManager1/Modem/2 ... Modem 2 ..."
    except Exception:
        return None

    # Intenta match por regex si se pidió
    pattern = os.getenv("LTE_MODEM_MATCH")
    if pattern:
        rx = re.compile(pattern, re.IGNORECASE)
        for line in listing.splitlines():
            if rx.search(line):
                m = re.search(r"Modem\s+(\d+)", line)
                if m:
                    return m.group(1)

    # Primer módem disponible
    m = re.search(r"Modem\s+(\d+)", listing)
    return m.group(1) if m else None

def modem_is_connected(mid: str) -> bool:
    try:
        out = _run(["mmcli", "-m", mid, "--simple-status"])
        return bool(re.search(r"\bstate:\s*connected\b", out, re.IGNORECASE))
    except Exception:
        return False

def check_restart_flag_and_exit_if_needed():
    if RESTART_FLAG.exists():
        try:
            RESTART_FLAG.unlink()
        except Exception:
            pass
        print("[CTRL] Restart solicitado por UI. Saliendo para que Compose reinicie el contenedor...")
        sys.stdout.flush()
        sys.exit(0)


def ble_disconnect_all(timeout_sec: int = 10) -> None:
    """
    Desconecta todos los dispositivos BLE conocidos (bluetoothctl) y hace power-cycle opcional.
    """
    try:
        subprocess.run(["bluetoothctl", "disconnect"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_sec)
        # opcional: power off/on si te es útil:
        # subprocess.run(["bluetoothctl", "power", "off"], check=False, timeout=5)
        # subprocess.run(["bluetoothctl", "power", "on"], check=False, timeout=5)
    except Exception:
        print("ble_disconnect_all falló")

def stop_app_gracefully(app_proc, timeout=7):
    if not app_proc or app_proc.poll() is not None:
        return
    print("[APP] Parando app (SIGTERM) …")
    try:
        os.killpg(os.getpgid(app_proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    # Espera graciosa
    t0 = time.time()
    while time.time() - t0 < timeout:
        if app_proc.poll() is not None:
            print("[APP] App salió graciosamente.")
            return
        time.sleep(0.2)
    print("[APP][WARN] Forzando KILL …")
    try:
        os.killpg(os.getpgid(app_proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass

# ---------- utils ----------
def sh(cmd):
    return subprocess.run(cmd, shell=True, check=False)

def out(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def gpioget_once():
    r = subprocess.run(["gpioget", GPIOCHIP, str(GPIOLINE)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[ERR] gpioget fallo: {r.stderr.strip()}")
        return None
    try:
        return int(r.stdout.strip())
    except Exception:
        return None

def gpioget_debounced():
    vals = []
    for _ in range(DEBOUNCE_SAMPLES):
        v = gpioget_once()
        if v is not None:
            vals.append(v)
        time.sleep(DEBOUNCE_DELAY)
    if not vals:
        return None
    return 1 if sum(vals) >= (len(vals)/2.0) else 0

def load_cfg():
    cfg = configparser.RawConfigParser()
    cfg.read(CONFIG_PATH)
    return cfg

def ping_ok(host="8.8.8.8", count=3):
    return sh(f"ping -c {count} {host} >/dev/null 2>&1").returncode == 0




# ---------- Helpers de red ----------
def netmask_to_prefix(mask):
    try:
        bits = "".join([bin(int(octet))[2:].zfill(8) for octet in mask.split(".")])
        return str(bits.count("1"))
    except Exception:
        return "24"




# ---------- Ethernet ----------
def eth_down():
    run(["nmcli","con","down","eth-client"])
    run(["nmcli","dev","disconnect", os.getenv("ETH_IF","ethernet0")])
    run(["ip","addr","flush","dev", os.getenv("ETH_IF","ethernet0")])
    _unset_autoconnect_and_delete("eth-client")
    # sh("nmcli con delete eth-client >/dev/null 2>&1 || true")

def eth_up(cfg):
    
    if not cfg.getboolean("Ethernet", "enabled", fallback=False):
        print("[ETH] disabled -> skip")
        eth_down()
        return False

    _nm_delete_all_by_name("eth-client")
    method = cfg.get("Ethernet","mode", fallback=cfg.get("Ethernet","method", fallback="dhcp")).strip().lower()
    address_cidr = cfg.get("Ethernet","address", fallback="").strip()
    ip_address   = cfg.get("Ethernet","ip_address", fallback="").strip()
    netmask      = cfg.get("Ethernet","netmask", fallback="255.255.255.0").strip()
    gateway      = cfg.get("Ethernet","gateway", fallback="").strip()
    dns          = cfg.get("Ethernet","dns",     fallback="").strip()
    if not address_cidr and ip_address:
        address_cidr = f"{ip_address}/{netmask_to_prefix(netmask)}"

    print(f"[ETH] Config: method={method} dev={ETH_IF}")
    sh(f"nmcli dev disconnect {ETH_IF} || true")
    sh("nmcli con down eth-client >/dev/null 2>&1 || true")

    sh(f"nmcli con add type ethernet ifname {ETH_IF} con-name eth-client")
    if method == "static":
        if not address_cidr:
            print("[ETH][ERR] Falta 'ip_address'/'address'")
            return False
        nm_modify("eth-client", "ipv4.method", "manual")
        nm_modify("eth-client", "ipv4.addresses", address_cidr)
        if gateway: nm_modify("eth-client", "ipv4.gateway", gateway)
        if dns:     nm_modify("eth-client", "ipv4.dns", dns)
    else:
        nm_modify("eth-client", "ipv4.method", "auto")

    nm_modify("eth-client", "connection.autoconnect", "yes")

    internet = cfg.getboolean("Ethernet","internet", fallback=False)
    metric = cfg.getint("Ethernet","metric", fallback=200)

    _apply_route_prefs("eth-client", internet, metric)
    _apply_lan_routes("eth-client", cfg.get("Ethernet", "lan_routes", fallback=""))

    # re-aplica cambios al perfil activo o levanta si aún no está

    rc = _reapply_or_up("eth-client", ETH_IF)
    if rc != 0:
        print("[ETH][ERR] Fallo al activar 'eth-client'")
        print(out(f"nmcli -t -f GENERAL.STATE,IP4.ADDRESS,IP4.GATEWAY device show {ETH_IF}").stdout)
        return False

    print("[ETH] Activo. Estado:")
    print(out(f"nmcli -t -f GENERAL.STATE,IP4.ADDRESS,IP4.GATEWAY device show {ETH_IF}").stdout)
    return True

# ---------- Wi-Fi ----------


def _wifi_connected(ifname: str) -> bool:
    if not ifname:
        return False
    # nmcli dice “connected (externally)” o “connected” cuando tiene link
    cmd = f"nmcli -t -f GENERAL.STATE dev show {ifname} 2>/dev/null | cut -d: -f2"
    try:
        out = subprocess.check_output(cmd, shell=True, text=True).strip()
        return out.startswith("100") or "connected" in out
    except Exception:
        return False

def _lan_connected() -> bool:
    return _eth_connected(ETH_IF) or _wifi_connected(os.getenv("WIFI_IF",""))

def _eth_connected(dev: str) -> bool:
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "DEVICE,STATE,TYPE", "dev"],
            text=True
        )
        for line in out.splitlines():
            dev0, state, typ = (line.split(":") + ["", "", ""])[:3]
            if dev0 == dev and typ.startswith("ethernet"):
                st = state.lower()
                if "connect" in st:  # connected / connecting / connected (externally)
                    return True
    except Exception:
        pass
    return False

def _lte_connected() -> bool:
    # 1) ModemManager
    try:
        out = subprocess.check_output(["mmcli", "-L"], text=True, stderr=subprocess.DEVNULL)
        m = re.search(r"Modem\s+(\d+)", out)
        if m:
            mid = m.group(1)
            s = subprocess.check_output(["mmcli", "-m", mid, "--simple-status"], text=True, stderr=subprocess.DEVNULL)
            if re.search(r"\bstate:\s*connected\b", s, re.IGNORECASE):
                return True
    except Exception:
        pass
    # 2) Fallback NetworkManager (TYPE=gsm)
    try:
        out = subprocess.check_output(
            ["nmcli", "-t", "-f", "DEVICE,STATE,TYPE", "dev"],
            text=True
        )
        for line in out.splitlines():
            _d, state, typ = (line.split(":") + ["", "", ""])[:3]
            if typ == "gsm" and "connect" in state.lower():
                return True
    except Exception:
        pass
    return False

def nm_modify(name: str, *props: str):
    args = ["nmcli", "con", "modify", name]
    # si alguna prop arranca con '-', terminamos el parsing de opciones
    if any(p.startswith("-") for p in props):
        args.append("--")
    args.extend(props)
    return run(args)

def _nm_delete_all_by_name(name: str):
    out = run_out(["nmcli", "-t", "-f", "NAME,UUID", "connection", "show"])
    to_del = [line.split(":")[1] for line in out.splitlines() if line.split(":")[0] == name and ":" in line]
    for u in to_del:
        run(["nmcli", "connection", "delete", "uuid", u])


def wifi_down():
    # Bajá la conexión solo si está activa (evita "not an active connection")
    if _nm_con_is_active("wifi-client"):
        run(["nmcli", "-w", "5", "con", "down", "wifi-client"])

    # Desconectá el device solo si está realmente activo (evita "device is not active")
    if _nm_dev_is_active(WIFI_IF):
        run(["nmcli", "-w", "5", "dev", "disconnect", WIFI_IF])

    # No mates wpa_supplicant salvo que estés fuera de NM; lo dejamos como "rescate" silencioso
    # run(["sh", "-lc", "command -v pkill >/dev/null && pkill -f wpa_supplicant || killall -q wpa_supplicant || true"])
    sh("sh -lc 'command -v pkill >/dev/null && pkill -f wpa_supplicant || killall -q wpa_supplicant || true'")

    # Limpiá IPs (sin ruido si ya no hay nada)
    run(["ip", "addr", "flush", "dev", WIFI_IF])
    _unset_autoconnect_and_delete("wifi-client")


def wifi_up(cfg):
    if not cfg.getboolean("WiFi", "enabled", fallback=False):
        print("[WiFi] disabled -> skip")
        wifi_down()
        return False

    ssid = cfg.get("WiFi", "ssid", fallback="").strip()
    psk  = cfg.get("WiFi", "password", fallback="").strip()
    if not ssid:
        print("[WiFi][ERR] SSID vacío")
        return False

    # Limpieza mínima (idempotente) sin duplicar acciones:
    wifi_down()  # ya chequea estados internamente
    _nm_delete_all_by_name("wifi-client")  # borra duplicados por NAME exacto

    # Asegurá que NM maneje la interfaz
    run(["nmcli", "dev", "set", WIFI_IF, "managed", "yes"])

    # Rescan para evitar "no network with SSID"
    run(["nmcli", "dev", "wifi", "rescan", "ifname", WIFI_IF])

    # Conectar. Usá lista de args (sin shell). Solo agregá 'password' si hay PSK.
    cmd = ["nmcli", "-w", "30", "dev", "wifi", "connect", ssid, "ifname", WIFI_IF, "name", "wifi-client"]
    if psk:
        cmd.extend(["password", psk])

    print(f"[WiFi] Conectando via: {' '.join(cmd)}")
    if run(cmd) != 0:
        print("[WiFi][ERR] Falló 'dev wifi connect'")
        print(run_out(["nmcli", "-t", "device", "status"]))
        return False

    # Política de ruteo (defaults razonables si internet=true y no hay métrica)
    internet = cfg.getboolean("WiFi", "internet", fallback=False)
    try:
        metric = cfg.getint("WiFi", "metric", fallback=None)
    except Exception:
        metric = None
    if internet and metric is None:
        metric = 300

    _apply_route_prefs("wifi-client", internet, metric)
    _apply_lan_routes("wifi-client", cfg.get("WiFi", "lan_routes", fallback=""))
    nm_modify("wifi-client", "connection.autoconnect", "yes")
    _reapply_or_up("wifi-client", WIFI_IF)

    print(f"[WiFi] Activo. Estado {WIFI_IF}:")
    print(run_out(["nmcli", "-t", "-f", "GENERAL.STATE,IP4.ADDRESS,IP4.GATEWAY", "device", "show", WIFI_IF]))
    return True


# ---------- LTE ----------
def lte_down():
    sh("nmcli con down lte-client >/dev/null 2>&1 || true")
    # sh("nmcli con delete lte-client >/dev/null 2>&1 || true")

def _lte_section(cfg):
    # Si existe LTE-4G, úsala; si no, LTE; si no, devolveme LTE para que falle claro
    return "LTE-4G" if cfg.has_section("LTE-4G") else "LTE"

def lte_up(cfg):
    section = _lte_section(cfg)

    if not cfg.getboolean(_lte_section(cfg), "enabled", fallback=False):
        print("[LTE] disabled"); sh("nmcli con down lte-client >/dev/null 2>&1 || true"); return False

    _nm_delete_all_by_name("lte-client")   # limpia duplicados antes del add

    
    apn  = cfg.get(section,"apn", fallback="").strip()
    user = cfg.get(section,"user", fallback="").strip()
    pw   = cfg.get(section,"password", fallback="").strip()
    pin  = cfg.get(section,"pin", fallback="").strip()

    internet = cfg.getboolean(section, "internet", fallback=True)
    metric = cfg.getint(section, "metric", fallback=100) if internet else None
    

    # módem
    modems = run_out(["mmcli","-L"]).strip()
    if "/Modem/" not in modems:
        print("[LTE][WARN] no hay módem (mmcli -L vacío)"); return False

    _nm_delete_all_by_name("lte-client")
    run(["nmcli","con","add","type","gsm","ifname","*","con-name","lte-client"])
    if apn:  nm_modify("lte-client","gsm.apn",apn)
    if user: nm_modify("lte-client","gsm.username",user)
    if pw:   nm_modify("lte-client","gsm.password",pw)
    nm_modify("lte-client","connection.autoconnect","yes")
    nm_modify("lte-client", "ipv4.ignore-auto-dns", "yes")
    nm_modify("lte-client", "ipv4.dns", "8.8.8.8 1.1.1.1")
    _apply_route_prefs("lte-client", internet, metric)
    # _reapply_or_up("lte-client", WIFI_IF)
    # rutas LAN si tenés
    # ...
    run(["nmcli","-w","40","con","up","lte-client"])
    return True

# ---------- App ----------
def _app_cmd_from_config():
    try:
        cfg = load_cfg()
        if cfg.has_section("APP"):
            cmd = cfg.get("APP","cmd", fallback="").strip()
            if cmd:
                return cmd
    except Exception:
        pass
    return os.getenv("APP_CMD", "python3 /opt/app/user_app.py").strip()

def run_user_app():
    # cmd = _app_cmd_from_config()
    # print(f"[APP] Lanzando: {cmd}")
    # return subprocess.Popen(cmd, shell=True)
    cmd = _app_cmd_from_config()
    print(f"[APP] Lanzando: {cmd}")
    # nuevo: grupo propio para matar toda la familia si hace falta
    return subprocess.Popen(cmd, shell=True, preexec_fn=os.setsid)

# ---------- Stacks ----------
def start_ap_stack():
    global ap_proc
    print("[STACK] Iniciando AP+UI")
    # 1) Cae todo lo "cliente"
    eth_down(); wifi_down(); lte_down()
    # 2) Evita que NM interfiera con el Wi-Fi mientras está el AP
    sh(f"nmcli dev set {WIFI_IF} managed no || true")
    # 3) (extra seguridad) limpia y sube uap0 antes de lanzar el stack
    sh("ip link set uap0 down || true")
    sh("ip addr flush dev uap0 || true")
    sh("ip link set uap0 up || true")
    # 4) Lanza el stack en su propio *process group* para poder matarlo en bloque
    ap_proc = subprocess.Popen(AP_CMD, shell=True, preexec_fn=os.setsid)
    time.sleep(2)
    return True

def stop_ap_stack():
    global ap_proc
    print("[STACK] Deteniendo AP+UI")
    try:
        if ap_proc and ap_proc.poll() is None:
            os.killpg(os.getpgid(ap_proc.pid), signal.SIGTERM)
            time.sleep(1)
            os.killpg(os.getpgid(ap_proc.pid), signal.SIGKILL)
    except Exception as e:
        print(f"[AP] WARN killpg: {e}")
    finally:
        ap_proc = None

    # Plan B: por si hostapd -B se despegó o dnsmasq dejó hijos
    # sh("sh -c 'command -v pkill >/dev/null && pkill -9 hostapd dnsmasq || killall -q hostapd dnsmasq || true'")
    # matar por separado para evitar "only one pattern can be provided"
    sh("sh -c 'command -v pkill >/dev/null && { pkill -9 -x hostapd || true; pkill -9 -x dnsmasq || true; } || { killall -q hostapd dnsmasq || true; }'")

    # Baja y limpia uap0
    sh("ip link set uap0 down || true")
    sh("ip addr flush dev uap0 || true")

    # Vuelve a dejar mlan0 manejado por NM
    sh(f"nmcli dev set {WIFI_IF} managed yes || true")

def _unset_prop_safe(name: str, prop: str):
    rc = nm_modify(name, f"-{prop}")   # <— QUITALO para route-metric
    if rc == 0:
        return
    rc = nm_modify(name, prop, "")
    if rc == 0:
        return
    if prop == "ipv4.route-metric":
        nm_modify(name, prop, "1000")

    
def _apply_route_prefs(name: str, internet: bool, metric: int | None):
    if internet:
        nm_modify(name, "ipv4.never-default", "no")
        # si no definiste métrica, poné una por defecto razonable
        nm_modify(name, "ipv4.route-metric", str(metric if metric is not None else 200))
    else:
        nm_modify(name, "ipv4.never-default", "yes")
        # NO toques ipv4.route-metric: queda -1 y está perfecto

def _apply_lan_routes(name: str, routes_csv: str | None):
    if not routes_csv:
        return
    for cidr in [r.strip() for r in routes_csv.split(",") if r.strip()]:
        # on-link route simple al prefijo
        sh(f"nmcli con modify {name} +ipv4.routes '{cidr}'")

def ble_power_cycle():
    # Si querés ser más drástico
    sh("bluetoothctl power off || true")
    time.sleep(1)
    sh("bluetoothctl power on || true")


def start_client_stack():
    cfg = load_cfg()
    print("[STACK] Cliente: levantar TODAS las conexiones habilitadas (prioridad de rutas LTE>ETH>WiFi)")

    # Asegura que perfiles existentes tengan las métricas correctas antes y después
    

    results = {"eth": False, "wifi": False, "lte": False}
    if cfg.getboolean("Ethernet", "enabled", fallback=False):
        results["eth"]  = bool(eth_up(cfg))
    else:
        print("[ETH] disabled -> skip")
        eth_down()  # <-- BAJAR si está deshabilitado

    if cfg.getboolean("WiFi", "enabled", fallback=False):
        results["wifi"] = bool(wifi_up(cfg))
    else:
        print("[WiFi] disabled -> skip")
        wifi_down()  # <-- BAJAR si está deshabilitado
        
    # Ojo: la sección puede ser LTE-4G o LTE (ya lo resolvés en _lte_section)
    if cfg.getboolean(_lte_section(cfg), "enabled", fallback=False):
        results["lte"]  = bool(lte_up(cfg))
    else:
        print("[LTE] disabled -> skip")

    # >>> AQUÍ aplicamos la política de failover/preferida <<<
    _apply_failover_policy(cfg)

    if not any(results.values()):
        print("[STACK][ERR] No hubo conectividad (ninguna interfaz levantó)")
        return False, None

    if ping_ok():
        print("[NET] Conectividad OK (ping 8.8.8.8).")
    else:
        print("[NET][WARN] Sin ICMP a 8.8.8.8.")

    # Mostrar default routes (comprobar que la de menor métrica gane)
    print(out("ip -4 route show default").stdout)

    proc = run_user_app()
    return True, proc

def stop_client_stack():
    global app_proc
    print("[STACK] Deteniendo Cliente (app + perfiles)")
    # if app_proc and app_proc.poll() is not None:
    #     app_proc = None
    # if app_proc and app_proc.poll() is None:
    #     app_proc.terminate()
    #     try:
    #         app_proc.wait(timeout=5)
    #     except subprocess.TimeoutExpired:
    #         print("[APP][WARN] kill()")
    #         app_proc.kill()
    #         app_proc.wait()
    if app_proc and app_proc.poll() is None:
        # terminar con gracia
        try:
            os.killpg(os.getpgid(app_proc.pid), signal.SIGTERM)
        except Exception:
            app_proc.terminate()
        # esperar corto y, si no sale, KILL al grupo
        t0 = time.time()
        while time.time() - t0 < 7:
            if app_proc.poll() is not None:
                break
            time.sleep(0.2)
        if app_proc.poll() is None:
            print("[APP][WARN] Forzando KILL …")
            try:
                os.killpg(os.getpgid(app_proc.pid), signal.SIGKILL)
            except Exception:
                app_proc.kill()
            app_proc.wait()    
    app_proc = None
    eth_down(); wifi_down(); lte_down()

def _boot_blink_all_leds(duration: float = 1.0, freq_hz: float = 4.0):
    """
    Blink de arranque: parpadea todos los LEDs (lan, lte, wifi, err)
    una vez al inicio del proceso supervisor.
    """
    global _blink_until, _blink_leds, _blink_freq

    _blink_leds = {"lan", "lte", "wifi", "err"}
    _blink_freq = freq_hz
    _blink_until = time.time() + duration

    print(f"[BOOT] Blink inicial de LEDs por {duration}s a {freq_hz}Hz")


# ---------- Señales ----------
def cleanup(*_):
        # liberar LEDs
    try:
        if '_led_eth' in globals() and _led_eth: _led_eth.close()
        if '_led_lte' in globals() and _led_lte: _led_lte.close()
        if '_led_wifi' in globals() and _led_wifi: _led_wifi.close()
        if '_led_err'  in globals() and _led_err:  _led_err.close()        
    except Exception:
        pass

    if tach_proc and tach_proc.poll() is None:
        tach_proc.terminate()

    # Si estás en AP, cerrá AP y devolvé mlan0 a NM
    if 'current_mode' in globals() and current_mode == "AP":
        stop_ap_stack()
        sh("nmcli dev set {WIFI_IF} managed yes || true")

    # Mantener cliente arriba al parar (por defecto)
    if not KEEP_CLIENT_ON_STOP:
        # Sólo si explícitamente querés bajar todo en stop
        stop_client_stack()

    sys.exit(0)

def main():
    global current_mode, ap_proc, app_proc, tach_proc
    global current_mode, ap_proc, app_proc

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print(f"[BOOT] ETH_IF={ETH_IF} WIFI_IF={WIFI_IF}")

    os.environ.setdefault("ETH_LED_CHIP", "/dev/gpiochip0")
    os.environ.setdefault("ETH_LED_LINE", "0")
    os.environ.setdefault("LTE_LED_CHIP", "/dev/gpiochip0")
    os.environ.setdefault("LTE_LED_LINE", "5")

    # WiFi = PWM_2  (X10-19)
    os.environ.setdefault("WIFI_LED_MODE", "pwm")
    os.environ.setdefault("WIFI_PWMCHIP", "1")     # <-- AJUSTÁ esto al pwmchip real
    os.environ.setdefault("WIFI_PWMCHAN", "0")     # <-- AJUSTÁ canal real (0/1)
    os.environ.setdefault("WIFI_PWM_PERIOD_NS", "20000000")
    os.environ.setdefault("WIFI_PWM_DUTY_ON_PCT", "100")

    # ERR = PWM_1 (X10-18)
    os.environ.setdefault("ERR_LED_MODE", "pwm")
    os.environ.setdefault("ERR_PWMCHIP", "0")      # <-- AJUSTÁ esto al pwmchip real
    os.environ.setdefault("ERR_PWMCHAN", "0")      # <-- AJUSTÁ canal real (0/1)
    os.environ.setdefault("ERR_PWM_PERIOD_NS", "20000000")
    os.environ.setdefault("ERR_PWM_DUTY_ON_PCT", "100")


    _led_eth = _mk_led("ETH", "eth") if gpiod else None
    _led_lte = _mk_led("LTE", "lte") if gpiod else None
    _led_wifi = _mk_led("WIFI", "wifi")
    _led_err  = _mk_led("ERR", "err") 


    # 🔴 Blink de arranque: todos los LEDs parpadean una vez al inicio
    _boot_blink_all_leds(duration=1.0, freq_hz=4.0)
    
    def _update_leds():
        global _last_eth, _last_lte, _last_wifi, _last_err
        # eth_on = _eth_connected(ETH_IF)
        lan_on = _lan_connected_strict()
        wifi_on = _wifi_connected_strict()
        lte_on = _lte_connected_strict()
        err_on  = False   # por defecto apagado; sólo se maneja vía blink
        now = time.time()
        blinking = now < _blink_until
        if blinking:
            # patrón: cuadrada a _blink_freq
            phase = int(now * _blink_freq) % 2 == 0  # True/False alternante
            if "lan" in _blink_leds:
                lan_on = phase
            if "lte" in _blink_leds:
                lte_on = phase
            if "wifi" in _blink_leds:
                wifi_on = phase
            if "err" in _blink_leds:
                err_on = phase

        if lan_on != _last_eth:
            print(f"[LED][LAN] -> {'ON' if lan_on else 'OFF'}")
            _last_eth = lan_on
        if lte_on != _last_lte:
            print(f"[LED][LTE] -> {'ON' if lte_on else 'OFF'}")
            _last_lte = lte_on
        if wifi_on != _last_wifi:
            print(f"[LED][WIFI] -> {'ON' if wifi_on else 'OFF'}")
            _last_wifi = wifi_on
        if err_on != _last_err:
            print(f"[LED][ERR] -> {'ON' if err_on else 'OFF'}")
            _last_err = err_on            
        try:
            if _led_eth: _led_eth.set(lan_on)
            if _led_lte: _led_lte.set(lte_on)
            if _led_wifi: _led_wifi.set(wifi_on)
            if _led_err:  _led_err.set(err_on)            
        except Exception:
            print("[LED][ERR] No se pudo actualizar LEDs")

    

    v0 = gpioget_debounced()
    if v0 is None:
        print("[FATAL] No puedo leer GPIO (arranque).")
        sys.exit(1)
    print(f"[GPIO] Estado inicial {GPIOCHIP} line {GPIOLINE} => {v0}")
    desired = "AP" if v0 == 1 else "CLIENT"

    if desired == "AP":
        start_ap_stack()
    else:
        ok, app_proc = start_client_stack()
        if not ok:
            print("[STACK] Cliente sin red: en espera…")
    current_mode = desired
    print(f"[MODE] Activo: {current_mode}")

    tach_proc = start_tach_if_enabled()

    while True:
        check_restart_flag_and_exit_if_needed()
        _check_blink_trigger()
        _update_leds()
        time.sleep(POLL_INTERVAL)
        v = gpioget_debounced()
        if v is None:
            continue
        want = "AP" if v == 1 else "CLIENT"

        # dentro del while True de main(), antes del sleep:
        if tach_proc and tach_proc.poll() is not None:
            print("[TACH][WARN] logger murió, reiniciando…")
            tach_proc = start_tach_if_enabled()

        # --- HOLD-OFF anti rebote ---
        now = time.time()
        global _last_switch_ts
        if want != current_mode:
            if (now - _last_switch_ts) < GPIO_HOLDOFF_S:
                # Ignoro cambios demasiado rápidos
                continue

            print(f"[MODE] Transición solicitada por GPIO: {current_mode} -> {want}")
            if want == "AP":
                # primero parar app; luego limpiar BLE; luego AP
                stop_client_stack()
                ble_disconnect_all()
                start_ap_stack()
            else:
                stop_ap_stack()
                ble_disconnect_all()
                ok, app_proc = start_client_stack()
                if not ok:
                    print("[STACK] Cliente sin red tras transición: en espera…")
            current_mode = want
            _last_switch_ts = now  # registro el último cambio

        if current_mode == "CLIENT" and app_proc and app_proc.poll() is not None:
            print(f"[APP][WARN] Proceso terminó rc={app_proc.returncode}. Reintentando en 5s…")
            app_proc = None
            time.sleep(5)
            _, app_proc = start_client_stack()

if __name__ == "__main__":
    main()
