#!/bin/bash
# ================================================================
# NETILY CLOUD CONTROLLER — DigitalOcean Deployment Script
# ================================================================
# Usage:
#   1. SSH into your droplet:  ssh root@<DROPLET_IP>
#   2. Clone the repo:         git clone <REPO_URL> netily_cloud && cd netily_cloud
#   3. Copy & edit .env:       cp .env.example .env && nano .env
#   4. Run this script:        chmod +x deploy.sh && ./deploy.sh
# ================================================================

set -e  # Exit on error

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}╔════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   NETILY CLOUD CONTROLLER — DEPLOYMENT SCRIPT     ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════╝${NC}"

# ── Pre-checks ─────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo -e "${RED}ERROR: .env file not found!${NC}"
    echo "Run: cp .env.example .env && nano .env"
    exit 1
fi

if ! command -v docker &> /dev/null; then
    echo -e "${RED}ERROR: Docker not installed!${NC}"
    exit 1
fi

if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo -e "${RED}ERROR: Docker Compose not installed!${NC}"
    exit 1
fi

# Detect docker-compose command (v1 vs v2)
if docker compose version &> /dev/null 2>&1; then
    DC="docker compose"
else
    DC="docker-compose"
fi

# env-file flag so docker compose can interpolate ${DB_NAME} etc.
ENV_FLAG="--env-file ../.env"

# ── Step 1: Swap Space (critical for 1GB droplet) ─────────────
echo -e "\n${YELLOW}[1/7] Setting up swap space...${NC}"
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # Tune swappiness for small servers
    sysctl vm.swappiness=10
    echo 'vm.swappiness=10' >> /etc/sysctl.conf
    echo -e "${GREEN}  ✓ 2GB swap created${NC}"
else
    echo -e "${GREEN}  ✓ Swap already exists${NC}"
fi
free -h | grep -i swap

# ── Step 2: System prep ───────────────────────────────────────
echo -e "\n${YELLOW}[2/7] Updating system packages...${NC}"
apt-get update -qq && apt-get install -y -qq git curl > /dev/null
echo -e "${GREEN}  ✓ System updated${NC}"

echo -e "\n${YELLOW}[2.5/7] Cleaning stale migration artifacts...${NC}"
git clean -fd apps/*/migrations 2>/dev/null || true
find apps -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find apps -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete 2>/dev/null || true
echo -e "${GREEN}  ✓ Migration folders cleaned${NC}"

# ── Step 3: Ensure Docker socket permissions ──────────────────
echo -e "\n${YELLOW}[3/7] Checking Docker socket...${NC}"
chmod 666 /var/run/docker.sock 2>/dev/null || true
echo -e "${GREEN}  ✓ Docker socket ready${NC}"

# ── Step 4: Build & Start ─────────────────────────────────────
echo -e "\n${YELLOW}[4/7] Building application containers...${NC}"
echo -e "  This will take 3-8 minutes on first run..."
cd docker
$DC $ENV_FLAG down --remove-orphans 2>/dev/null || true
$DC $ENV_FLAG build --no-cache web celery-worker celery-beat frontend radius

echo -e "\n${YELLOW}  Waiting for database to be ready...${NC}"
sleep 15  # Give postgres time to initialise

# Verify containers are up
echo ""
$DC $ENV_FLAG ps
echo ""

# ── Step 5: Collect static files ──────────────────────────────
echo -e "\n${YELLOW}[5/7] Collecting static files...${NC}"
$DC $ENV_FLAG run --rm web python manage.py collectstatic --noinput || true
echo -e "${GREEN}  ✓ Static files collected${NC}"

# ── Step 6: Run migrations ────────────────────────────────────
echo -e "\n${YELLOW}[6/7] Preparing and running multi-tenant migrations...${NC}"
$DC $ENV_FLAG run --rm web python manage.py prepare_migrations
$DC $ENV_FLAG run --rm web python manage.py migrate_schemas_resilient --shared
echo -e "${GREEN}  ✓ Shared schema migrated${NC}"
$DC $ENV_FLAG run --rm web python manage.py migrate_schemas_resilient --tenant
$DC $ENV_FLAG run --rm web python manage.py seed_plans
echo -e "${GREEN}  ✓ Tenant schemas migrated${NC}"
$DC $ENV_FLAG up -d
$DC $ENV_FLAG restart nginx
$DC $ENV_FLAG restart network-healer

# ── Step 7: Summary ───────────────────────────────────────────
cd ..
DROPLET_IP=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_DROPLET_IP")

echo -e "\n${GREEN}╔════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              DEPLOYMENT COMPLETE! 🚀               ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  API:       ${GREEN}http://${DROPLET_IP}/api/v1/${NC}"
echo -e "  Admin:     ${GREEN}http://${DROPLET_IP}/admin/${NC}"
echo -e "  Swagger:   ${GREEN}http://${DROPLET_IP}/swagger/${NC}"
echo -e "  Health:    ${GREEN}http://${DROPLET_IP}/health/${NC}"
echo ""
echo -e "${YELLOW}NEXT STEPS:${NC}"
echo "  1. Create superadmin:"
echo "     cd docker && $DC exec web python manage.py createsuperuser"
echo ""
echo "  2. Set up SSL (after DNS points to this IP):"
echo "     $DC run --rm certbot certonly --webroot --webroot-path=/var/www/certbot -d YOUR_DOMAIN.com --email YOUR_EMAIL --agree-tos"
echo "     Then uncomment the HTTPS block in docker/nginx/nginx.conf"
echo "     $DC restart nginx"
echo ""
echo "  3. Update Vercel env var:"
echo "     NEXT_PUBLIC_API_URL=http://${DROPLET_IP}"
echo ""
echo "  4. View logs:"
echo "     cd docker && $DC logs -f web"
echo ""
