#!/usr/bin/env bash
set -euo pipefail

echo "======================================"
echo " us-market-review Ubuntu 24.04 Installer"
echo "======================================"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "[1/10] Checking system..."

if [ -f /etc/os-release ]; then
  . /etc/os-release
  echo "Detected OS: ${PRETTY_NAME:-Unknown}"
  if [[ "${ID:-}" != "ubuntu" ]]; then
    echo "Warning: This script is designed for Ubuntu. Current OS ID: ${ID:-unknown}"
  fi
else
  echo "Warning: /etc/os-release not found. Continue anyway."
fi

echo "[2/10] Updating apt..."
sudo apt update

echo "[3/10] Installing system packages..."
sudo apt install -y git python3 python3-venv python3-pip fonts-noto-cjk curl

echo "[4/10] Creating required directories..."
mkdir -p logs
mkdir -p reports/markdown
mkdir -p reports/pdf
mkdir -p data/raw
mkdir -p data/processed

echo "[5/10] Creating Python virtual environment..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
else
  echo ".venv already exists, skip creating."
fi

echo "[6/10] Installing Python dependencies..."
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

echo "[7/10] Preparing .env..."
if [ ! -f ".env" ]; then
  if [ -f ".env.example" ]; then
    cp .env.example .env
    echo ".env created from .env.example"
  else
    echo "Warning: .env.example not found. Please create .env manually."
  fi
else
  echo ".env already exists, skip copying."
fi

echo "[8/10] Checking run_daily.sh..."
if [ -f "run_daily.sh" ]; then
  chmod +x run_daily.sh
  echo "run_daily.sh is ready."
else
  echo "ERROR: run_daily.sh not found."
  exit 1
fi

echo "[9/10] Checking test_send.py..."
if [ -f "test_send.py" ]; then
  echo "test_send.py found."
else
  echo "Warning: test_send.py not found. Push notification test may not be available."
fi

echo "[10/10] Installation complete."

echo ""
echo "======================================"
echo " Next steps"
echo "======================================"
echo ""
echo "1. Edit .env and fill Telegram or Feishu config:"
echo ""
echo "   nano .env"
echo ""
echo "2. After editing .env, test push notification:"
echo ""
echo "   source .venv/bin/activate"
echo "   python test_send.py"
echo ""
echo "3. Generate one report manually:"
echo ""
echo "   bash run_daily.sh"
echo ""
echo "4. View logs:"
echo ""
echo "   tail -n 100 logs/daily.log"
echo ""
echo "5. After manual run succeeds, set cron:"
echo ""
echo "   crontab -e"
echo ""
echo "   Add this line:"
echo ""
echo "   30 7 * * 2-6 cd $PROJECT_DIR && bash run_daily.sh >> logs/daily.log 2>&1"
echo ""
echo "This means: run every Tuesday to Saturday at 07:30 server time."
echo "Recommended timezone: Asia/Shanghai"
echo ""
echo "To set timezone:"
echo ""
echo "   sudo timedatectl set-timezone Asia/Shanghai"
echo ""
echo "======================================"
