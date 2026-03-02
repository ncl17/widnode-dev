#!/usr/bin/env bash
set -euo pipefail

: "${VPN_SERVER:?Falta VPN_SERVER}"
: "${VPN_IPSEC_PSK:?Falta VPN_IPSEC_PSK}"
: "${VPN_USER:?Falta VPN_USER}"
: "${VPN_PASSWORD:?Falta VPN_PASSWORD}"

VPN_NAME="${VPN_NAME:-myvpn}"
VPN_MTU="${VPN_MTU:-1280}"
VPN_MRU="${VPN_MRU:-1280}"

echo "==> Preparando configuración IPsec (Libreswan)"
cat >/etc/ipsec.conf <<IPSEC_CONF
version 2.0
config setup
  uniqueids=no
  protostack=netkey
  ikev1-policy=accept

conn shared
  left=%defaultroute
  right=${VPN_SERVER}
  encapsulation=yes
  authby=secret
  pfs=no
  rekey=no
  ikev2=never
  ike=aes256-sha2;modp2048,aes128-sha2;modp2048,aes256-sha1;modp2048,aes128-sha1;modp2048
  phase2alg=aes_gcm-null,aes128-sha1,aes256-sha1,aes256-sha2_512,aes128-sha2,aes256-sha2
  ikelifetime=24h
  salifetime=24h
  dpddelay=30
  dpdtimeout=300
  sha2-truncbug=no

conn l2tp-psk
  auto=add
  type=transport
  leftprotoport=17/1701
  rightprotoport=17/1701
  also=shared
IPSEC_CONF

# PSK
cat >/etc/ipsec.secrets <<IPSEC_SECRETS
%any %any : PSK "${VPN_IPSEC_PSK}"
IPSEC_SECRETS
chmod 600 /etc/ipsec.secrets

echo "==> Configurando xl2tpd (cliente L2TP)"
mkdir -p /etc/xl2tpd /var/run/xl2tpd
cat >/etc/xl2tpd/xl2tpd.conf <<XL2TPD_CONF
[global]
port = 1701

[lac ${VPN_NAME}]
lns = ${VPN_SERVER}
ppp debug = yes
pppoptfile = /etc/ppp/options.l2tpd.client
length bit = yes
XL2TPD_CONF

echo "==> Configurando PPP (cliente)"
cat >/etc/ppp/options.l2tpd.client <<PPP_OPTS
ipcp-accept-local
ipcp-accept-remote
refuse-eap
require-mschap-v2
noccp
noauth
mtu ${VPN_MTU}
mru ${VPN_MRU}
name ${VPN_USER}
password ${VPN_PASSWORD}
nodefaultroute
usepeerdns
lcp-echo-failure 4
lcp-echo-interval 30
connect-delay 5000
PPP_OPTS
chmod 600 /etc/ppp/options.l2tpd.client

# CHAP (usuario/clave)
cat >/etc/ppp/chap-secrets <<CHAP
"${VPN_USER}" * "${VPN_PASSWORD}" *
CHAP
chmod 600 /etc/ppp/chap-secrets

echo "==> Preparando NSS y run dirs de Libreswan"
mkdir -p /run/pluto /var/lib/ipsec/nss
# Inicializa la base NSS donde pluto la espera (sql:/var/lib/ipsec/nss)
ipsec initnss --nssdir /var/lib/ipsec/nss

echo "==> Lanzando pluto (Libreswan) sin systemd"
if [ -x /usr/lib/ipsec/pluto ]; then
  /usr/lib/ipsec/pluto --config /etc/ipsec.conf --nssdir sql:/var/lib/ipsec/nss &
else
  /usr/libexec/ipsec/pluto --config /etc/ipsec.conf --nssdir sql:/var/lib/ipsec/nss &
fi
sleep 2

echo "==> Añadiendo conexión l2tp-psk"
ipsec auto --add l2tp-psk || true

echo "==> Estableciendo SA IKEv1/ESP hacia ${VPN_SERVER}"
if ! ipsec auto --up l2tp-psk; then
  echo "WARN: reintentando IPsec en 3s..."
  sleep 3
  ipsec auto --up l2tp-psk
fi

echo "==> Iniciando xl2tpd"
touch /var/run/xl2tpd/l2tp-control
xl2tpd -D &

sleep 1
echo "==> Marcando llamada L2TP (${VPN_NAME})"
echo "c ${VPN_NAME}" > /var/run/xl2tpd/l2tp-control

echo "==> VPN en marcha (si todo fue bien). Manteniendo en foreground."
while true; do
  sleep 10
done
