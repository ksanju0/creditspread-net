"""
Social Media Auto-Poster
Posts every signal + close to X/Twitter automatically.
Requires: pip install tweepy
Get free API keys at: developer.twitter.com
"""
import os, sys, sqlite3, time, logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv('C:/Users/sanjivek/Documents/spx_trader/.env')
sys.path.insert(0, 'C:/Users/sanjivek/Documents/spx_trader')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("C:/Users/sanjivek/Documents/spx_trader/social.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("social")

# ── Twitter / X client ────────────────────────────────────────────────────────

def get_twitter_client():
    try:
        import tweepy
        client = tweepy.Client(
            consumer_key        = os.getenv('TWITTER_API_KEY'),
            consumer_secret     = os.getenv('TWITTER_API_SECRET'),
            access_token        = os.getenv('TWITTER_ACCESS_TOKEN'),
            access_token_secret = os.getenv('TWITTER_ACCESS_SECRET'),
        )
        return client
    except ImportError:
        log.warning("tweepy not installed — run: pip install tweepy")
        return None
    except Exception as e:
        log.error(f"Twitter client error: {e}")
        return None

def post_tweet(text):
    client = get_twitter_client()
    if not client:
        log.info(f"[MOCK TWEET]\n{text}\n")
        return "mock_id"
    try:
        r = client.create_tweet(text=text)
        tweet_id = r.data['id']
        log.info(f"Tweet posted: {tweet_id}")
        return tweet_id
    except Exception as e:
        log.error(f"Tweet failed: {e}")
        return None

# ── Post templates ────────────────────────────────────────────────────────────

def format_signal_tweet(trade):
    short  = trade['short_strike']
    long_s = trade['long_strike']
    expiry = trade['expiry']
    credit = trade['net_credit']
    ror    = trade['ror_pct']
    vix    = trade['vix_at_entry']
    spx    = trade['spx_at_entry']
    now    = datetime.now().strftime('%Y-%m-%d %H:%M ET')

    return (
        f"🟢 SPX CREDIT SPREAD SIGNAL\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Bull Put Spread\n"
        f"SELL {short}P / BUY {long_s}P\n"
        f"Expiry: {expiry}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Credit:   ${credit}\n"
        f"📊 ROR:      {ror}%\n"
        f"📈 SPX:      {spx}\n"
        f"⚡ VIX:      {vix}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ Signal: {now}\n"
        f"Traded live in our accounts · creditspread.net\n"
        f"#SPX #OptionsTrading #CreditSpreads #0DTE"
    )

def format_close_tweet(trade):
    short  = trade['short_strike']
    long_s = trade['long_strike']
    pnl    = trade['pnl']
    reason = trade['exit_reason']
    credit = trade['net_credit']
    won    = pnl > 0 if pnl else False
    emoji  = "✅" if won else "❌"
    result = "WINNER" if won else "STOPPED OUT"

    return (
        f"{emoji} SPX TRADE CLOSED — {result}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Spread: {short}/{long_s}P\n"
        f"Entry credit: ${credit}\n"
        f"Exit reason:  {reason}\n"
        f"P&L: {'$' + str(pnl) if pnl else '—'}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Track record: creditspread.net/performance\n"
        f"#SPX #OptionsTrading #CreditSpreads"
    )

def format_weekly_recap_tweet(stats):
    return (
        f"📊 CreditSpread.net WEEKLY RECAP\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Trades this week: {stats['week_trades']}\n"
        f"Winners:          {stats['week_wins']}\n"
        f"Win rate:         {stats['week_wr']}%\n"
        f"P&L:              ${stats['week_pnl']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"All-time win rate: {stats['total_wr']}%\n"
        f"Total P&L:         ${stats['total_pnl']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Full log → creditspread.net/performance\n"
        f"#SPX #OptionsTrading #TheTagang #WeeklyRecap"
    )

# ── Database helpers ──────────────────────────────────────────────────────────

def get_unposted_trades():
    conn = sqlite3.connect('C:/Users/sanjivek/Documents/spx_trader/trades.db')
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Get trades not yet posted to Twitter
    cur.execute("""
        SELECT * FROM trades
        WHERE status != 'OPEN'
        AND id NOT IN (
            SELECT CAST(trade_id AS INTEGER) FROM social_posts
            WHERE platform = 'twitter'
        )
        ORDER BY id DESC LIMIT 10
    """)
    trades = [dict(r) for r in cur.fetchall()]
    conn.close()
    return trades

def get_weekly_stats():
    conn = sqlite3.connect('C:/Users/sanjivek/Documents/spx_trader/trades.db')
    cur = conn.cursor()
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()

    cur.execute("SELECT * FROM trades WHERE signal_time >= ? AND status != 'OPEN' AND pnl IS NOT NULL", (week_ago,))
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    week_trades = [dict(zip(cols, r)) for r in rows]

    cur.execute("SELECT * FROM trades WHERE status != 'OPEN' AND pnl IS NOT NULL")
    all_rows = cur.fetchall()
    all_trades = [dict(zip(cols, r)) for r in all_rows]
    conn.close()

    week_wins = [t for t in week_trades if t['pnl'] > 0]
    all_wins  = [t for t in all_trades  if t['pnl'] > 0]

    return {
        'week_trades': len(week_trades),
        'week_wins':   len(week_wins),
        'week_wr':     round(len(week_wins) / len(week_trades) * 100, 1) if week_trades else 0,
        'week_pnl':    round(sum(t['pnl'] for t in week_trades), 2),
        'total_wr':    round(len(all_wins) / len(all_trades) * 100, 1) if all_trades else 0,
        'total_pnl':   round(sum(t['pnl'] for t in all_trades), 2),
    }

def save_post_record(platform, content, post_id, trade_id, post_type):
    try:
        import sys
        sys.path.insert(0, 'C:/Users/sanjivek/Documents/spx_trader/webapp')
        from app import app
        from models import db, SocialPost
        with app.app_context():
            db.session.add(SocialPost(
                platform=platform, content=content,
                post_id=str(post_id) if post_id else None,
                trade_id=trade_id, post_type=post_type
            ))
            db.session.commit()
    except Exception as e:
        log.warning(f"Could not save post record: {e}")

# ── Main loop ─────────────────────────────────────────────────────────────────

def run_social_poster():
    log.info("=" * 50)
    log.info("  SPX SOCIAL AUTO-POSTER STARTED")
    log.info("  Posts signals + closes to Twitter/X")
    log.info("=" * 50)

    last_weekly = None

    while True:
        try:
            # Post any unposted closed trades
            trades = get_unposted_trades()
            for trade in trades:
                if trade['exit_reason'] == 'PROFIT_TARGET' or trade['exit_reason'] == 'STOP_LOSS':
                    text = format_close_tweet(trade)
                    pid  = post_tweet(text)
                    save_post_record('twitter', text, pid, trade['id'], 'close')
                    time.sleep(2)

            # Weekly recap every Friday at 4:15 PM ET
            now = datetime.now()
            if now.weekday() == 4 and now.hour == 16 and now.minute == 15:
                if last_weekly != now.date():
                    stats = get_weekly_stats()
                    if stats['week_trades'] > 0:
                        text = format_weekly_recap_tweet(stats)
                        pid  = post_tweet(text)
                        save_post_record('twitter', text, pid, None, 'weekly_recap')
                        last_weekly = now.date()

        except Exception as e:
            log.error(f"Social poster error: {e}")

        time.sleep(300)  # check every 5 minutes

if __name__ == "__main__":
    # Quick test — post a mock signal
    mock_trade = {
        'short_strike': 5250, 'long_strike': 5225,
        'expiry': '2026-06-06', 'net_credit': 3.80,
        'ror_pct': 18.2, 'vix_at_entry': 14.8, 'spx_at_entry': 5423.5
    }
    print("Preview of signal tweet:\n")
    print(format_signal_tweet(mock_trade))
    print("\n" + "="*50)
    print("\nPreview of weekly recap tweet:\n")
    stats = get_weekly_stats()
    print(format_weekly_recap_tweet(stats))
