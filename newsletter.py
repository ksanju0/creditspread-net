"""
CreditSpread.net — Daily Pre-Market Newsletter

Generates and sends the 8-section morning briefing to active subscribers.

Provider: Resend (primary) → Gmail SMTP (fallback for dev).

ENV vars:
  RESEND_API_KEY        Resend API key
  NEWSLETTER_FROM       From email (e.g. alerts@creditspread.net)
  NEWSLETTER_FROM_NAME  Display name (default "CreditSpread Pre-Market")
  ADMIN_EMAIL           Reply-To + admin test recipient
"""
import os, sys, sqlite3, logging, secrets
from datetime import datetime, date
from flask import render_template

log = logging.getLogger("newsletter")

# ── Provider ──────────────────────────────────────────────────────────────────

def _env(key, default=''): return os.getenv(key, default)

def _from_header():
    name = _env('NEWSLETTER_FROM_NAME', 'CreditSpread Pre-Market')
    addr = _env('NEWSLETTER_FROM', _env('GMAIL_ADDRESS', 'noreply@creditspread.net'))
    return f'{name} <{addr}>'

def send_email(to, subject, html, text=None, reply_to=None):
    if not to: return False
    if _env('RESEND_API_KEY'):
        return _send_resend(to, subject, html, text, reply_to)
    return _send_gmail_fallback(to, subject, html, text, reply_to)

def _send_resend(to, subject, html, text=None, reply_to=None):
    try:
        import resend
        resend.api_key = _env('RESEND_API_KEY')
        params = {
            'from':    _from_header(),
            'to':      [to] if isinstance(to, str) else list(to),
            'subject': subject,
            'html':    html,
        }
        if text:     params['text'] = text
        if reply_to: params['reply_to'] = reply_to
        r = resend.Emails.send(params)
        log.info(f"Resend OK → {to} id={r.get('id') if isinstance(r,dict) else r}")
        return True
    except ImportError:
        log.warning("resend not installed; falling back to Gmail")
        return _send_gmail_fallback(to, subject, html, text, reply_to)
    except Exception as e:
        log.error(f"Resend failed {to}: {e}")
        return False

def _send_gmail_fallback(to, subject, html, text=None, reply_to=None):
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    gmail = _env('GMAIL_ADDRESS'); pw = _env('GMAIL_APP_PASSWORD')
    if not (gmail and pw):
        log.warning(f"No email provider configured. Would send to {to}: {subject}")
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = f'CreditSpread <{gmail}>'
        msg['To']      = to if isinstance(to, str) else ', '.join(to)
        if reply_to: msg['Reply-To'] = reply_to
        if text:     msg.attach(MIMEText(text, 'plain'))
        msg.attach(MIMEText(html, 'html'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(gmail, pw)
            s.send_message(msg)
        log.info(f"Gmail OK → {to}")
        return True
    except Exception as e:
        log.error(f"Gmail failed {to}: {e}")
        return False

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_pnl(v):
    if v is None: return '0'
    if abs(v) >= 1_000_000: return f"${v/1_000_000:,.2f}M"
    if abs(v) >= 1_000:     return f"${v/1_000:,.1f}K"
    return f"${v:,.0f}"

def new_unsubscribe_token():
    return secrets.token_urlsafe(24)

# ── Market snapshot — all numeric content from trades.db ──────────────────────

def _market_snapshot(db_path):
    out = {
        # Section 1 — The Open
        'vix': None, 'spx': None, 'spx_pct': None,
        # Section 3 — The Levels
        'spx_support': None, 'spx_resistance': None,
        'qqq_support': None, 'qqq_resistance': None,
        # Section 4 — Vol picture
        'vix_regime': 'normal', 'vix_narrative': '',
        # Section 5 — The Tape
        'yesterday': None,
        # Section 7 — Stats
        'stats': {'trades': 0, 'win_rate': 0, 'total_pnl_fmt': '0'},
    }
    try:
        conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Latest trade — provides VIX, SPX read
        cur.execute("SELECT vix_at_entry, spx_at_entry FROM trades WHERE spx_at_entry IS NOT NULL ORDER BY id DESC LIMIT 1")
        latest = cur.fetchone()
        if latest:
            out['vix'] = latest['vix_at_entry']
            out['spx'] = latest['spx_at_entry']

        # Section 3 — recent ~10-trade high/low gives meaningful S/R
        cur.execute("""
            SELECT MIN(spx_at_entry) AS lo, MAX(spx_at_entry) AS hi
            FROM trades
            WHERE spx_at_entry IS NOT NULL
              AND id > (SELECT COALESCE(MAX(id),0)-15 FROM trades)
        """)
        r = cur.fetchone()
        if r and r['lo'] and r['hi']:
            out['spx_support']    = int(r['lo'])
            out['spx_resistance'] = int(r['hi'])
            # QQQ proxy via long-term SPX/QQQ ratio ~ 11.4
            out['qqq_support']    = round(r['lo'] / 11.4, 1)
            out['qqq_resistance'] = round(r['hi'] / 11.4, 1)

        # Section 4 — VIX narrative
        v = out['vix']
        if v is not None:
            if v < 12:
                out['vix_regime'], out['vix_narrative'] = (
                    'extreme low',
                    f"VIX at {v} is exceptionally low — premium is thin. We typically sit out below 12.")
            elif v < 18:
                out['vix_regime'], out['vix_narrative'] = (
                    'low-normal',
                    f"VIX at {v} sits in our optimal premium-selling range. 25-point spreads at 0.10 delta carry solid credit.")
            elif v < 25:
                out['vix_regime'], out['vix_narrative'] = (
                    'normal',
                    f"VIX at {v} is normal. Standard 20–25 point spread width applies today.")
            elif v < 32:
                out['vix_regime'], out['vix_narrative'] = (
                    'elevated',
                    f"VIX at {v} is elevated. We tighten to 15–20 point spreads and reduce size.")
            else:
                out['vix_regime'], out['vix_narrative'] = (
                    'skip-zone',
                    f"VIX at {v} is in our skip zone. We don't trade above 32 — the edge breaks down.")
        else:
            out['vix_narrative'] = "Volatility regime will publish at the open."

        # Section 5 — most recent closed trade
        cur.execute("""
            SELECT signal_time, short_strike, long_strike, net_credit, pnl, exit_reason
            FROM trades WHERE status != 'OPEN' AND pnl IS NOT NULL
            ORDER BY id DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            won = row['pnl'] > 0
            out['yesterday'] = {
                'spread':  f"{row['short_strike']:.0f}/{row['long_strike']:.0f}P",
                'credit':  f"${row['net_credit']}",
                'pnl_fmt': (f"+${row['pnl']:,.0f}" if won else f"-${abs(row['pnl']):,.0f}"),
                'won':     won,
                'reason':  (row['exit_reason'] or '').replace('_', ' ').title(),
            }

        # Section 7 — aggregate
        cur.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS w,
                   SUM(pnl) AS p
            FROM trades WHERE status != 'OPEN' AND pnl IS NOT NULL
        """)
        agg = cur.fetchone()
        if agg and agg['n']:
            out['stats'] = {
                'trades':        agg['n'],
                'win_rate':      round(agg['w']/agg['n']*100, 1),
                'total_pnl_fmt': _fmt_pnl(agg['p'] or 0),
            }
        conn.close()
    except Exception as e:
        log.error(f"snapshot error: {e}")
    return out

# ── Context builder ───────────────────────────────────────────────────────────

def build_context(trades_db_path, subscriber=None):
    now = datetime.now()
    ctx = {
        'date_long':       now.strftime('%A, %B %d, %Y'),
        'date_short':      now.strftime('%b %d'),
        'weekday':         now.strftime('%A'),
        'subscriber':      subscriber,
        'base_url':        'https://creditspread.net',
        'unsubscribe_url': None,
    }
    if subscriber and getattr(subscriber, 'unsubscribe_token', None):
        ctx['unsubscribe_url'] = f"https://creditspread.net/unsubscribe/{subscriber.unsubscribe_token}"

    # Sections 1, 3, 4, 5, 7 from market snapshot
    ctx.update(_market_snapshot(trades_db_path))

    # Section 2 — Calendar (manual weekly file)
    try:
        from newsletter_calendar import get_catalysts, get_playbook
        ctx['catalysts'] = get_catalysts(now.date())
        ctx['playbook']  = get_playbook(now.date())
    except Exception as e:
        log.warning(f"calendar load failed: {e}")
        ctx['catalysts'] = ['No major scheduled catalysts today',
                            'Standard 9:45 AM entry window in play']
        ctx['playbook']  = ("Our system scans SPX & QQQ at 9:45 AM ET. "
                            "Signal fires when our volatility, delta, and "
                            "time-of-day filters align.")

    # Section 8 — Rotating lesson from blog
    try:
        from blog_posts import get_all_posts
        posts = get_all_posts()
        if posts:
            idx = now.timetuple().tm_yday % len(posts)
            p = posts[idx]
            ctx['lesson'] = {
                'title': p.get('title', ''),
                'excerpt': (p.get('meta_description') or p.get('excerpt', ''))[:180],
                'url': f"https://creditspread.net/blog/{p.get('slug','')}",
            }
    except Exception as e:
        log.warning(f"lesson load failed: {e}")
        ctx['lesson'] = None

    return ctx

# ── Render + send ─────────────────────────────────────────────────────────────

def render_newsletter(app, db_path, subscriber=None):
    ctx = build_context(db_path, subscriber)
    with app.app_context(), app.test_request_context():
        return render_template('newsletter_email.html', **ctx), ctx

def render_welcome(app, db_path, subscriber):
    ctx = build_context(db_path, subscriber)
    with app.app_context(), app.test_request_context():
        return render_template('newsletter_welcome.html', **ctx), ctx

def send_welcome(app, db_path, subscriber):
    html, ctx = render_welcome(app, db_path, subscriber)
    return send_email(subscriber.email,
                      "Welcome to CreditSpread Pre-Market",
                      html, text=_text_fallback(ctx),
                      reply_to=_env('ADMIN_EMAIL'))

def send_daily(app, db_path, Subscriber, dry_run=False):
    """Send today's newsletter to every active subscriber. Returns counts."""
    from datetime import datetime as _dt
    subject = f"Pre-Market — {datetime.now().strftime('%b %d')}"
    sent = failed = 0
    subs = Subscriber.query.filter_by(status='active').all()
    for s in subs:
        html, ctx = render_newsletter(app, db_path, s)
        if dry_run:
            sent += 1; continue
        ok = send_email(s.email, subject, html, text=_text_fallback(ctx),
                        reply_to=_env('ADMIN_EMAIL'))
        if ok:
            sent += 1
            s.last_sent_at = _dt.utcnow()
            s.send_count   = (s.send_count or 0) + 1
        else:
            failed += 1
    return {'sent': sent, 'failed': failed, 'total': len(subs)}

def _text_fallback(ctx):
    lines = [f"PRE-MARKET — {ctx['date_long']}", "", "CreditSpread.net daily briefing.", ""]
    if ctx.get('vix') is not None:
        lines += [f"VIX: {ctx['vix']}", ""]
    y = ctx.get('yesterday')
    if y:
        lines += [f"Yesterday: SPX {y['spread']} · Credit {y['credit']} · {y['pnl_fmt']} ({y['reason']})", ""]
    s = ctx.get('stats') or {}
    if s.get('trades'):
        lines += [f"Lifetime: {s['trades']} trades · {s['win_rate']}% win rate", ""]
    lines += [
        "Members get today's exact signal live at 9:45 AM ET.",
        "Start a 7-day free trial: https://creditspread.net/pricing",
        "",
        "Unsubscribe: " + (ctx.get('unsubscribe_url') or 'reply to this email'),
    ]
    return "\n".join(lines)
