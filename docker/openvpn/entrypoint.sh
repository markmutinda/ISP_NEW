#!/bin/sh
set -eu

PKI=/etc/openvpn/pki
CCD=/etc/openvpn/ccd
NETWORK="${OPENVPN_NETWORK:-10.9.0.0}"
NETMASK="${OPENVPN_NETMASK:-255.255.0.0}"
GATEWAY="${OPENVPN_GATEWAY_IP:-10.9.0.1}"
RADIUS_IP="${RADIUS_VPN_IP:-10.10.0.250}"

mkdir -p "$PKI" "$CCD" /dev/net
[ -c /dev/net/tun ] || mknod /dev/net/tun c 10 200
chmod 755 "$CCD"

# One-time self-signed server cert (routers use user/pass, no client certs)
if [ ! -s "$PKI/server.crt" ]; then
  echo "Generating server certificate + DH params..."
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
    -subj "/CN=netily-ovpn" -addext "extendedKeyUsage=serverAuth" \
    -keyout "$PKI/server.key" -out "$PKI/server.crt"
  openssl dhparam -dsaparam -out "$PKI/dh.pem" 2048
  chmod 600 "$PKI/server.key"
fi

# NAT: Django/RADIUS/HAProxy reach routers as the tunnel gateway (same trick as WG side)
iptables -P FORWARD ACCEPT
iptables -t nat -C POSTROUTING -o tun0 -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -o tun0 -j MASQUERADE
# Routers talk RADIUS to the tunnel gateway -> forward to FreeRADIUS container
iptables -t nat -C PREROUTING -i tun0 -p udp -m multiport --dports 1812,1813 \
  -j DNAT --to-destination "$RADIUS_IP" 2>/dev/null || \
  iptables -t nat -A PREROUTING -i tun0 -p udp -m multiport --dports 1812,1813 \
  -j DNAT --to-destination "$RADIUS_IP"

cat > /etc/openvpn/server.conf <<EOF
port 1194
proto tcp
dev tun
server $NETWORK $NETMASK
topology subnet
push "topology subnet"
push "route $GATEWAY 255.255.255.255"

ca $PKI/server.crt
cert $PKI/server.crt
key $PKI/server.key
dh $PKI/dh.pem

verify-client-cert none
username-as-common-name
auth-user-pass-verify /etc/openvpn/auth.sh via-env
script-security 3
client-config-dir $CCD
ccd-exclusive

cipher AES-256-CBC
data-ciphers AES-256-CBC:AES-256-GCM
data-ciphers-fallback AES-256-CBC
auth SHA1
tls-cipher "DEFAULT:@SECLEVEL=0"

keepalive 10 120
reneg-sec 0
max-clients 3000
persist-key
persist-tun
user nobody
group nogroup
verb 3
status /tmp/ovpn-status.log 10
EOF

# Write auth config readable by nobody (auth.sh sources this file)
printf 'AUTH_URL="%s"\nOPENVPN_AUTH_TOKEN="%s"\n' \
  "${AUTH_URL:-http://web:8000/api/v1/vpn/openvpn-auth/}" "${OPENVPN_AUTH_TOKEN:-}" \
  > /etc/openvpn/auth.conf
chown nobody:nogroup /etc/openvpn/auth.conf
chmod 400 /etc/openvpn/auth.conf

exec openvpn --config /etc/openvpn/server.conf