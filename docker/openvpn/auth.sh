#!/bin/sh
# Works with via-env (username/password env vars) and via-file ($1 = file)
LOG=/tmp/ovpn-auth.log

if [ -z "${username:-}" ] && [ -n "${1:-}" ] && [ -r "$1" ]; then
  username=$(sed -n 1p "$1")
  password=$(sed -n 2p "$1")
fi

# Config written by entrypoint (does not rely on inherited env)
[ -r /etc/openvpn/auth.conf ] && . /etc/openvpn/auth.conf

if [ -z "${username:-}" ] || [ -z "${password:-}" ]; then
  echo "$(date '+%F %T') EMPTY creds (user_set=${username:+yes} pass_set=${password:+yes})" >> "$LOG"
  exit 1
fi

code=$(/usr/bin/curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST \
  "${AUTH_URL:-http://web:8000/api/v1/vpn/openvpn-auth/}" \
  -H "X-VPN-Webhook-Secret: ${OPENVPN_AUTH_TOKEN:-}" \
  --data-urlencode "username=${username}" \
  --data-urlencode "password=${password}")

echo "$(date '+%F %T') user=${username} http=${code} token_len=${#OPENVPN_AUTH_TOKEN}" >> "$LOG"
[ "$code" = "200" ] && exit 0
exit 1