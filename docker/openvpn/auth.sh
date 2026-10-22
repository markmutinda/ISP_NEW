#!/bin/sh
# OpenVPN auth-user-pass-verify (via-env): $username / $password come from env
[ -n "${username:-}" ] && [ -n "${password:-}" ] || exit 1
code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' -X POST \
  "${AUTH_URL:-http://web:8000/api/v1/vpn/openvpn-auth/}" \
  -H "X-VPN-Webhook-Secret: ${OPENVPN_AUTH_TOKEN}" \
  --data-urlencode "username=${username}" \
  --data-urlencode "password=${password}")
[ "$code" = "200" ] && exit 0
exit 1