#!/usr/bin/env python3
"""
Entrypoint for Docker deployment.

Runs the live polling loop continuously, sending Telegram alerts
when edge thresholds are crossed.
"""

import os
import sys
import time
import subprocess
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INTELLIGENCE_API_KEY = os.getenv("intelligence_api_key")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
BASE_DIR = Path(__file__).parent
SCRIPTS_DIR = BASE_DIR / "scripts"
DATA_DIR = BASE_DIR / "data"


def bootstrap():
    """Ensure runtime data exists before starting the poll loop."""
    traders_file = DATA_DIR / "top_mlb_traders.json"
    if traders_file.exists():
        return

    print("No trader data found. Running trader discovery pipeline...")
    print("This may take several minutes on first run.")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "find_top_mlb_traders.py")],
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    print(result.stdout[-3000:] if result.stdout else "")
    if result.returncode != 0:
        print(f"Trader discovery failed (exit {result.returncode}):")
        print(result.stderr[-2000:] if result.stderr else "")
        sys.exit(1)
    print("Trader discovery complete.")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not INTELLIGENCE_API_KEY:
        print("Error: Missing credentials! Check TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and intelligence_api_key.")
        sys.exit(1)

    sys.path.insert(0, str(SCRIPTS_DIR))
    bootstrap()

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
