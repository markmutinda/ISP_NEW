#!/bin/bash
# ================================================================
# NETILY FRONTEND — DigitalOcean Droplet Setup Script
# ================================================================
# Run this once on the droplet as root:
#   bash ~/netily_cloud/scripts/setup-frontend-do.sh
#
# Prerequisites:
#   - ~/netily_cloud/ exists (ISP_NEW repo, prod-backend branch)
#   - Docker + docker compose installed
#   - DigitalOcean DNS is managing netily.co.ke
#   - DO_TOKEN env var is set (your DigitalOcean API token)
# ================================================================

set -e

FRONTEND_REPO="https://github.com/ojpierre/netily-frontend.git"
FRONTEND_DIR="$HOME/netily_frontend"
COMPOSE_DIR="$HOME/netily_cloud/docker"
DO_TOKEN="${DO_TOKEN:-}"

echo ""
echo "========================================="
echo "  NETILY FRONTEND — DO SETUP"
echo "========================================="
echo ""

# ── 1. Clone / update frontend repo ───────────────────────────
if [ -d "$FRONTEND_DIR/.git" ]; then
  echo "[1/5] Updating frontend repo..."
  cd "$FRONTEND_DIR"
  git fetch origin
  git reset --hard origin/prod-frontend
else
  echo "[1/5] Cloning frontend repo..."
  git clone -b prod-frontend "$FRONTEND_REPO" "$FRONTEND_DIR"
fi

# ── 2. Wildcard SSL cert via DNS-01 ───────────────────────────
echo ""
echo "[2/5] Setting up wildcard SSL cert for *.netily.co.ke ..."

# Check if cert already exists
if [ -f "/etc/letsencrypt/live/netily.co.ke/fullchain.pem" ]; then
  echo "  ✓ Cert already exists. Skipping."
else
  if [ -z "$DO_TOKEN" ]; then
    echo ""
    echo "  ⚠  DO_TOKEN not set. Skipping wildcard cert."
    echo "  Set it with: export DO_TOKEN=your_digitalocean_api_token"
    echo "  Then re-run this script, or manually run:"
    echo "    apt install -y python3-certbot-dns-digitalocean"
    echo "    certbot certonly --dns-digitalocean \\"
    echo "      --dns-digitalocean-credentials /root/.secrets/do-credentials.ini \\"
    echo "      -d '*.netily.co.ke' -d 'netily.co.ke' \\"
    echo "      --agree-tos --non-interactive --email admin@netily.co.ke"
  else
    echo "  Installing certbot DigitalOcean plugin..."
    apt-get install -y -q python3-certbot-dns-digitalocean

    echo "  Writing DO credentials..."
    mkdir -p /root/.secrets
    cat > /root/.secrets/do-credentials.ini <<CREDS
dns_digitalocean_token = ${DO_TOKEN}
CREDS
    chmod 600 /root/.secrets/do-credentials.ini

    echo "  Requesting wildcard cert..."
    certbot certonly \
      --dns-digitalocean \
      --dns-digitalocean-credentials /root/.secrets/do-credentials.ini \
      --dns-digitalocean-propagation-seconds 30 \
      -d "*.netily.co.ke" \
      -d "netily.co.ke" \
      --agree-tos \
      --non-interactive \
      --email admin@netily.co.ke

    echo "  ✓ Wildcard cert issued."
  fi
fi

# ── 3. Verify docker volumes exist for certs ──────────────────
echo ""
echo "[3/5] Checking certbot Docker volumes..."

# The certbot container mounts /etc/letsencrypt into certbot_certs volume.
# We need the host cert to be visible there too.
# Strategy: mount the host cert dir into nginx via a bind mount override
# (handled in docker-compose.yml via certbot_certs volume → where certbot writes)

if [ -f "/etc/letsencrypt/live/netily.co.ke/fullchain.pem" ]; then
  echo "  Symlinking host certs into certbot volume path..."
  # nginx reads from /etc/letsencrypt inside its container (via certbot_certs volume)
  # That volume is managed by the certbot container.
  # Since we issued the cert on the HOST (not via the certbot container),
  # we copy it into the volume.
  CERT_VOL=$(docker volume inspect docker_certbot_certs --format '{{.Mountpoint}}' 2>/dev/null || echo "")
  if [ -n "$CERT_VOL" ]; then
    mkdir -p "$CERT_VOL/live/netily.co.ke"
    cp -f /etc/letsencrypt/live/netily.co.ke/fullchain.pem  "$CERT_VOL/live/netily.co.ke/"
    cp -f /etc/letsencrypt/live/netily.co.ke/privkey.pem    "$CERT_VOL/live/netily.co.ke/"
    cp -f /etc/letsencrypt/live/netily.co.ke/chain.pem      "$CERT_VOL/live/netily.co.ke/" 2>/dev/null || true
    echo "  ✓ Certs copied into certbot_certs Docker volume."
  else
    echo "  ⚠  certbot_certs volume not found yet (containers not started). Will copy after first docker compose up."
  fi
fi

# ── 4. Build and start frontend container ─────────────────────
echo ""
echo "[4/5] Building and starting frontend container..."
cd "$COMPOSE_DIR"
docker compose up -d --build frontend nginx

echo ""
echo "[5/5] Verifying containers..."
docker compose ps frontend nginx

echo ""
echo "========================================="
echo "  SETUP COMPLETE"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Update DNS on DigitalOcean:"
echo "     A  *.netily.co.ke  → 68.183.45.64"
echo "     A  www.netily.co.ke → 68.183.45.64  (or CNAME → netily.co.ke)"
echo "     A  netily.co.ke    → 68.183.45.64"
echo ""
echo "  2. Remove domain from Vercel:"
echo "     vercel domains rm www.netily.co.ke  (from your local machine)"
echo ""
echo "  3. Test:"
echo "     curl -sk https://www.netily.co.ke/ | head -c 200"
echo "     curl -sk https://pink4.netily.co.ke/admin | head -c 200"
echo ""
echo "  4. Set up cert auto-renewal cron:"
echo "     echo '0 3 * * * certbot renew --quiet && \\"
echo "       cp /etc/letsencrypt/live/netily.co.ke/*.pem \$(docker volume inspect docker_certbot_certs --format \"{{.Mountpoint}}\")/live/netily.co.ke/ && \\"
echo "       docker exec netily_nginx nginx -s reload' | crontab -"
echo ""
