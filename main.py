#!/usr/bin/env python3
"""
Entrypoint for Docker deployment.

Runs the live polling loop continuously, sending Telegram alerts
when edge thresholds are crossed.
"""

import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INTELLIGENCE_API_KEY = os.getenv("intelligence_api_key")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
SCRIPTS_DIR = Path(__file__).parent / "scripts"
DATA_DIR = Path(__file__).parent / "data"


def ensure_traders():
    traders_file = DATA_DIR / "top_mlb_traders.json"
    if traders_file.exists():
        return
    print("No trader data found. Running discovery pipeline (may take a few minutes)...")
    sys.path.insert(0, str(SCRIPTS_DIR))
    from find_top_mlb_traders import main as discover
    discover()
    print("Trader discovery complete.")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not INTELLIGENCE_API_KEY:
        print("Error: Missing credentials! Check TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and intelligence_api_key.")
        sys.exit(1)

    sys.path.insert(0, str(SCRIPTS_DIR))
    ensure_traders()

    print(f"Sentiment bot starting — polling every {POLL_INTERVAL}s")
    while True:
        try:
            from poll_live import main as poll
            poll()
        except Exception as e:
            print(f"Poll cycle failed: {e}")
        print(f"Sleeping {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
