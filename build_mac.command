#!/bin/bash
# Mac: klik dua kali. Membuat aplikasi "Cek Bukti TF e-guru.id.app" (hanya perlu sekali per versi).
cd "$(dirname "$0")"
set -e
APP="Cek Bukti TF e-guru.id"
[ -d .venv ] || python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --windowed --name "$APP" --collect-data googleapiclient --collect-data certifi main.py
echo
echo "Selesai. Aplikasi ada di: $(pwd)/dist/$APP.app"
open dist
read -p "Tekan Enter untuk menutup..."
