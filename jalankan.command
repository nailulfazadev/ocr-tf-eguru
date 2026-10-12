#!/bin/bash
# Mac: klik dua kali untuk menjalankan aplikasi (setelah pemasangan pertama).
cd "$(dirname "$0")"
source .venv/bin/activate && python main.py
