# MLB Sentiment Bot

Tracks top MLB traders on Polymarket, computes sentiment signals per market, detects edge threshold crossings, and sends Telegram alerts when there's a high-confidence trading opportunity.

## Quick Start — Docker

```bash
git clone https://github.com/yxf9tv/mob-sentiment-bot.git
cd mob-sentiment-bot
```

```bash
docker build -t sentiment-bot .
docker run -d \
  -e intelligence_api_key=<key> \
  -e TELEGRAM_BOT_TOKEN=<token> \
  -e TELEGRAM_CHAT_ID=<id> \
  -e POLL_INTERVAL=300 \
  sentiment-bot
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `intelligence_api_key` | Yes | Prediction Market Intelligence API key |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token (from @BotFather) |
| `TELEGRAM_CHAT_ID` | Yes | Your Telegram user/chat ID |
| `POLL_INTERVAL` | No | Seconds between poll cycles (default: 300) |

## Local Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in your keys
python3 scripts/poll_live.py     # run one poll cycle
python3 scripts/server.py        # launch dashboard on :8000
python3 main.py                  # continuous polling loop
```

## Telegram Alerts

On each poll cycle, if a market crosses a volume-based edge threshold, the bot sends an alert:

```
⚾ NYY @ BOS (Moneyline)
moneyline → NYY wins

Volume:     $2,450
Traders:    12
Conviction: 0.72
Liq Depth:  0.68 ✅ liquidity aligns

Edge:       ML + vol >= $1,071 -> 74.4%
Confidence: HIGH
```

When resting orderbook liquidity disagrees with the trade consensus:

```
Liq Depth:  0.28 ⚠️ resting orders oppose consensus
```

## Edge Thresholds

Thresholds derived from backtested accuracy and ROI:

| Strategy | Market | Min Volume | Accuracy | ROI |
|---|---|---|---|---|
| ML High Vol | moneyline | $1,071 | 74.4% | 48.8% |
| Spread Mid Vol | spread | $107 | 73.1% | 46.2% |
| ML Mid Vol | moneyline | $107 | 64.0% | 27.9% |
| Total (Fade) | total | $0 | 57.3% | 14.6% |

## Deployment

Push to `master` → GitHub Actions deploys via SSH:

```yaml
# .github/workflows/deploy.yml
```
