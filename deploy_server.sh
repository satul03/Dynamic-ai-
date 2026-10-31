#!/usr/bin/env bash
# =============================================================================
# deploy_server.sh - Automated Cloud Server (VPS) Deployment Script
# Targets: Ubuntu 22.04 / 24.04 LTS (DigitalOcean, AWS EC2, Hetzner, Linode)
# Sets up: Python 3 venv, Playwright Chromium/Firefox, Systemd Services
# =============================================================================

set -e

echo "=========================================================="
echo "🚀 Starting RedBus Dynamic Pricing Platform Deployment"
echo "=========================================================="

# 1. Update system packages
echo "📦 Updating system packages..."
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv git curl ufw

# 2. Determine project directory
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
echo "📂 Working in directory: $APP_DIR"

# 3. Create virtual environment
if [ ! -d ".venv" ]; then
    echo "🐍 Creating Python virtual environment (.venv)..."
    python3 -m venv .venv
fi

# 4. Activate venv and install dependencies
echo "📦 Installing Python dependencies from requirements.txt..."
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Install Playwright browser binaries with Linux system dependencies
echo "🌐 Installing Playwright headless browsers (Chromium & Firefox)..."
python3 -m playwright install --with-deps chromium firefox

# 6. Setup Systemd Service for Streamlit Dashboard
echo "⚙️ Creating systemd service for Streamlit Dashboard..."
sudo bash -c "cat <<EOF > /etc/systemd/system/streamlit.service
[Unit]
Description=Streamlit Bus Pricing Intelligence Dashboard
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF"

# 7. Setup Systemd Service for 24x7 Scraper Worker
echo "⚙️ Creating systemd service for Scraper Background Worker..."
sudo bash -c "cat <<EOF > /etc/systemd/system/scraper-worker.service
[Unit]
Description=24x7 redBus Scraper & Dynamic Pricing Worker
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python worker.py
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF"

# 8. Reload systemd and start services
echo "🔄 Reloading systemd daemon and starting services..."
sudo systemctl daemon-reload
sudo systemctl enable streamlit.service
sudo systemctl restart streamlit.service

sudo systemctl enable scraper-worker.service
sudo systemctl restart scraper-worker.service

# 9. Configure firewall to allow port 8501
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow 8501/tcp comment "Streamlit Dashboard" || true
    sudo ufw allow 22/tcp comment "SSH" || true
fi

# 10. Fetch Server Public IP
PUBLIC_IP=$(curl -s https://ifconfig.me || curl -s https://api.ipify.org || echo "<YOUR_SERVER_IP>")

echo ""
echo "=========================================================="
echo "✅ DEPLOYMENT COMPLETE!"
echo "=========================================================="
echo "🌐 Dashboard URL: http://${PUBLIC_IP}:8501"
echo ""
echo "📊 Check service status anytime:"
echo "   sudo systemctl status streamlit"
echo "   sudo systemctl status scraper-worker"
echo ""
echo "📜 View live logs:"
echo "   sudo journalctl -u streamlit -f"
echo "   sudo journalctl -u scraper-worker -f"
echo "=========================================================="
