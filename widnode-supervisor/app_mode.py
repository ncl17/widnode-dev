#!/usr/bin/env python3
import os, sys, subprocess, signal, configparser, pathlib

IFACE = os.getenv("AP_IFACE", "uap0")
AP_IP_CIDR = os.getenv("AP_IP_CIDR", "10.10.10.1/24")
CONFIG_FILE = os.getenv("CONFIG_FILE", "/data/config.ini")

def sh(cmd:list, check=True):
    print("[AP]", " ".join(cmd)); sys.stdout.flush()
    return subprocess.run(cmd, check=check)

def read_channel(default="6"):
    cfg = configparser.ConfigParser()
    try:
        if pathlib.Path(CONFIG_FILE).exists():
            cfg.read(CONFIG_FILE)
            return cfg.get("AP", "channel", fallback=default)
    except Exception as e:
        print(f"[AP][WARN] canal por defecto ({default}): {e}")
    return default

def main():
    ssid = os.getenv("AP_SSID", "WIDNODE_AP")
    psk  = os.getenv("AP_PSK",  "widnode1234")
    cc   = os.getenv("COUNTRY_CODE", "AR")
    ch   = read_channel("6")

    print(f"[AP] AP en {IFACE} SSID={ssid} CH={ch} CC={cc}")
    # configurar uap0
    sh(["rfkill","unblock","all"], check=False)
    sh(["ip","link","set",IFACE,"down"], check=False)
    sh(["ip","addr","flush","dev",IFACE], check=False)
    sh(["ip","link","set",IFACE,"up"])
    sh(["ip","addr","add",AP_IP_CIDR,"dev",IFACE], check=False)

    # hostapd
    open("/tmp/hostapd.conf","w").write(f"""interface={IFACE}
            driver=nl80211
            ssid={ssid}
            country_code={cc}
            ieee80211d=1
            hw_mode=g
            channel={ch}
            wmm_enabled=1
            ieee80211n=1
            auth_algs=1
            ignore_broadcast_ssid=0
            wpa=2
            wpa_key_mgmt=WPA-PSK
            rsn_pairwise=CCMP
            wpa_passphrase={psk}
            """)
    sh(["hostapd","-B","/tmp/hostapd.conf"])

    # dnsmasq
    open("/tmp/dnsmasq.conf","w").write(f"""interface={IFACE}
bind-interfaces
dhcp-range=10.10.10.10,10.10.10.200,255.255.255.0,12h
dhcp-option=3,10.10.10.1
dhcp-option=6,10.10.10.1
log-queries
log-dhcp
""")

    # UI
    ui = None
    if pathlib.Path("/opt/ui/app.py").exists():
        ui = ["/opt/venv/bin/python","/opt/ui/app.py"]
    elif pathlib.Path("/opt/ui/main.py").exists():
        ui = ["/opt/venv/bin/python","/opt/ui/main.py"]

    ui_proc = None
    if ui:
        print("[AP] Lanzando UI:", " ".join(ui)); sys.stdout.flush()
        ui_proc = subprocess.Popen(ui, cwd="/opt/ui",
                                   stdout=open("/tmp/ui.log","a"),
                                   stderr=subprocess.STDOUT)
    else:
        print("[AP][WARN] No encontré UI en /opt/ui/")

    def cleanup(sig=None, frame=None):
        print("[AP] limpiando…"); sys.stdout.flush()
        try: sh(["pkill","-f","hostapd"], check=False)
        except: pass
        try: sh(["ip","link","set",IFACE,"down"], check=False)
        except: pass
        if ui_proc:
            try: ui_proc.terminate()
            except: pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    print("[AP] Listo. Abrí http://10.10.10.1:8080")
    sh(["dnsmasq","--conf-file=/tmp/dnsmasq.conf","--no-daemon"])

if __name__ == "__main__":
    main()
