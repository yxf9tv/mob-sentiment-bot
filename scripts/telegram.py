"""Send sentiment edge alerts to Telegram."""

import json, os, sys, time
from pathlib import Path
from dotenv import load_dotenv
import requests

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"
load_dotenv(BASE / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        print("[telegram] missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env", file=sys.stderr)
        return False
    try:
        resp = requests.post(
            API_URL,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=15,
        )
        return resp.ok
    except Exception as e:
        print(f"[telegram] send error: {e}", file=sys.stderr)
        return False


TEAM_MAP = {
    "nyy": "NYY", "bos": "BOS", "lad": "LAD", "sd": "SD",
    "hou": "HOU", "det": "DET", "tex": "TEX", "tor": "TOR",
    "chc": "CHC", "mil": "MIL", "atl": "ATL", "sf": "SF",
    "nym": "NYM", "phi": "PHI", "ari": "ARI", "tb": "TB",
    "oak": "OAK", "laa": "LAA", "sea": "SEA", "col": "COL",
    "min": "MIN", "kc": "KC", "cws": "CWS", "cle": "CLE",
    "cin": "CIN", "pit": "PIT", "stl": "STL", "mia": "MIA",
    "wsh": "WSH", "bal": "BAL",
}


def slug_to_readable(slug):
    parts = slug.split("-")
    if len(parts) < 4:
        return slug
    if parts[0] == "mlb" and len(parts) >= 5:
        t1, t2 = parts[1], parts[2]
        rest = parts[4:]
        mtype = "Moneyline"
        if "total" in rest:
            idx = rest.index("total")
            line = f" {rest[idx+1]}" if idx+1 < len(rest) else ""
            mtype = f"Total{line}"
        elif "spread" in rest:
            idx = rest.index("spread")
            side = rest[idx+1].title() if idx+1 < len(rest) else ""
            line = f" {rest[idx+2]}" if idx+2 < len(rest) else ""
            mtype = f"Spread {side}{line}"
        return f"{TEAM_MAP.get(t1,t1.upper())} @ {TEAM_MAP.get(t2,t2.upper())} ({mtype})"
    return slug


def format_alert(event):
    slug = event.get("slug", "")
    label = slug_to_readable(slug)
    market_type = event.get("market_type", "?")
    top_outcome = event.get("top_outcome", "?")
    volume = event.get("volume", 0)
    traders = event.get("unique_traders", 0)
    conviction = event.get("conviction", 0)
    depth_imb = event.get("depth_imbalance")
    game_date = event.get("game_date", "")
    desc = event.get("description", "")

    confidence = "HIGH" if "74.4" in desc or "73.1" in desc else "MEDIUM"

    lines = [
        f"⚾ *{label}*",
        f"`{market_type}` → *{top_outcome}*",
    ]
    if game_date:
        lines.append(f"`{game_date}`")
    lines.append("")
    lines.append(f"Volume:\t${volume:,.0f}")
    lines.append(f"Traders:\t{traders}")
    lines.append(f"Conviction:\t{conviction:.2f}")

    if depth_imb is not None:
        imb = round(depth_imb, 2)
        if imb < 0.4:
            tag = " ⚠️ *resting orders oppose consensus*"
        elif imb > 0.6:
            tag = " ✅ *liquidity aligns*"
        else:
            tag = " `neutral`"
        lines.append(f"Liq Depth:\t{imb:.2f}{tag}")

    lines.append("")
    lines.append(f"Edge:\t{desc}")
    lines.append(f"Confidence:\t*{confidence}*")

    return "\n".join(lines)


def notify_new_triggers(events):
    for ev in events:
        text = format_alert(ev)
        ok = send_message(text)
        time.sleep(0.3)
        if not ok:
            print(f"[telegram] failed to send alert for {ev.get('slug','?')}")


def send_test():
    return send_message("✅ *Sentiment2 Edge Alert — test successful*")
