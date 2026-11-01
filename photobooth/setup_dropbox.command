#!/bin/bash
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python3 ]; then echo "Run start.command once first."; read -p "Press Enter"; exit 1; fi
.venv/bin/python3 dropbox_setup.py
