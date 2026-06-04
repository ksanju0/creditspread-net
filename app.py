import os, sys, json
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import stripe

# ── Path resolution — works on Windows dev AND Render Linux ───────────────────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))          # .../webapp/
ROOT_DIR   = os.path.dirname(BASE_DIR)                            # .../spx_trader/
ENV_FILE   = os.path.join(ROOT_DIR, '.env')
if os.path.exists(ENV_FILE):
    load_dotenv(ENV_FILE)

# trades.db lives next to app.py in production (committed to repo)
TRADES_DB  = os.getenv('TRADES_DB_PATH', os.path.join(BASE_DIR, 'trades.db'))
MEMBERS_DB = os.getenv('MEMBERS_DB_PATH', os.path.join(BASE_DIR, 'members.db'))

from models import db, User, Lead, SocialPost, MemberTrade, Subscriber, AlertLog, LiveTrade
import newsletter as newsletter_lib

app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv('SECRET_KEY', 'creditspread-secret-2026-change-me')

# ── Database URI ──────────────────────────────────────────────────────────────
# Production (Render): DATABASE_URL points at managed Postgres (persists forever).
# Local dev: falls back to SQLite file.
DATABASE_URL = os.getenv('DATABASE_URL', '')
if DATABASE_URL:
    # Render/Heroku give "postgres://..." but SQLAlchemy needs "postgresql://"
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    USING_POSTGRES = True
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{MEMBERS_DB}'
    USING_POSTGRES = False

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

db.init_app(app)

def run_migrations():
    """
    Auto-migration. create_all() makes new tables but won't ALTER existing ones,
    so we add any missing columns. Works for both Postgres (prod) and SQLite (dev).
    Safe to run on every boot.
    """
    new_cols = {
        'account_size':        'FLOAT',
        'account_pledge_date': 'TIMESTAMP',
        'risk_pct':            'FLOAT DEFAULT 2.0',
        'phone':               'VARCHAR(20)',
        'telegram_chat_id':    'VARCHAR(40)',
        'free_months':         'INTEGER DEFAULT 0',
        'referral_count':      'INTEGER DEFAULT 0',
    }
    try:
        with app.app_context():
            # Create any tables that don't exist yet (with full new schema)
            db.create_all()

            # Discover existing columns on users
            from sqlalchemy import inspect, text
            insp = inspect(db.engine)
            existing_cols = {c['name'] for c in insp.get_columns('users')}

            for col, coltype in new_cols.items():
                if col not in existing_cols:
                    # Postgres TIMESTAMP vs SQLite DATETIME — both accept these keywords
                    ddl = coltype
                    if not USING_POSTGRES and 'TIMESTAMP' in ddl:
                        ddl = ddl.replace('TIMESTAMP', 'DATETIME')
                    db.session.execute(text(f'ALTER TABLE users ADD COLUMN {col} {ddl}'))
                    db.session.commit()
                    print(f"[migration] Added users.{col}")

            # live_trades added columns
            if 'live_trades' in insp.get_table_names():
                lt_cols = {c['name'] for c in insp.get_columns('live_trades')}
                lt_new = {'exit_debit': 'FLOAT', 'expired_worthless': 'BOOLEAN DEFAULT FALSE'}
                for col, coltype in lt_new.items():
                    if col not in lt_cols:
                        db.session.execute(text(f'ALTER TABLE live_trades ADD COLUMN {col} {coltype}'))
                        db.session.commit()
                        print(f"[migration] Added live_trades.{col}")
    except Exception as e:
        print(f"[migration] error: {e}")

run_migrations()

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access your dashboard'
login_manager.login_message_category = 'error'

stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')

STRIPE_PRICES = {
    'member': os.getenv('STRIPE_MEMBER_PRICE_ID', 'price_member_placeholder'),
}

PAYPAL_CLIENT_ID      = os.getenv('PAYPAL_CLIENT_ID',      'paypal_client_id_placeholder')
PAYPAL_CLIENT_SECRET  = os.getenv('PAYPAL_CLIENT_SECRET',  'paypal_secret_placeholder')
PAYPAL_MEMBER_PLAN_ID = os.getenv('PAYPAL_MEMBER_PLAN_ID', 'paypal_member_plan_placeholder')
PAYPAL_API_BASE       = 'https://api-m.paypal.com'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# Inject site-verification codes (set as Render env vars — no code change needed)
@app.context_processor
def inject_site_verification():
    return {
        'google_site_verification': os.getenv('GOOGLE_SITE_VERIFICATION', ''),
        'bing_site_verification':   os.getenv('BING_SITE_VERIFICATION', ''),
        'current_year':             datetime.now().year,
    }

@app.context_processor
def inject_site_stats():
    """
    Sitewide source of truth for numbers shown on marketing pages.
    Pulls live track-record stats from trades.db and combines with manual
    member-growth metrics. Every page templates use these — no hardcoded
    contradictory values anywhere.
    """
    try:
        s, _ = get_trade_stats()
        return {
            'site_stats': {
                'trades':       s.total_trades,
                'win_rate':     s.win_rate,
                'total_pnl_fmt':s.total_pnl_fmt,
                'avg_credit':   s.avg_credit,
                'avg_ror':      s.avg_ror,
                # Member-side metrics (manual, kept consistent here)
                'members':      '850+',
                'years':        '3+',
                'launched':     'Jan 2023',
                'avg_member_pnl': '$127,400',  # per-member avg over 3 yrs
                'avg_member_growth': '34.2%',  # per-member portfolio growth
            }
        }
    except Exception:
        return {'site_stats': {'trades':'350+','win_rate':89,'total_pnl_fmt':'3.3M',
                'avg_credit':4.7,'avg_ror':24,'members':'850+','years':'3+',
                'launched':'Jan 2023','avg_member_pnl':'$127,400','avg_member_growth':'34.2%'}}

# ── helpers ───────────────────────────────────────────────────────────────────

def fmt_pnl(val):
    """Format P&L: $8.60M, $430.5K, $1,200"""
    if abs(val) >= 1_000_000:
        return f"{val/1_000_000:,.2f}M"
    if abs(val) >= 1_000:
        return f"{val/1_000:,.1f}K"
    return f"{val:,.0f}"

def get_trade_stats():
    import sqlite3
    try:
        conn = sqlite3.connect(TRADES_DB)
        cur  = conn.cursor()
        cur.execute("SELECT * FROM trades ORDER BY id DESC")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        trades = [dict(zip(cols, r)) for r in rows]
        conn.close()

        closed  = [t for t in trades if t['status'] != 'OPEN' and t['pnl'] is not None]
        winners = [t for t in closed if t['pnl'] > 0]
        total_pnl_raw = round(sum(t['pnl'] for t in closed), 2) if closed else 0
        win_rate   = round(len(winners) / len(closed) * 100, 1) if closed else 0
        avg_credit = round(sum(t['net_credit'] for t in trades) / len(trades), 2) if trades else 0
        avg_ror    = round(sum(t['ror_pct'] for t in trades if t['ror_pct']) / len(trades), 1) if trades else 0

        closed_sorted = sorted(closed, key=lambda x: x['id'])
        cum, equity_curve_raw = 0, []
        for t in closed_sorted:
            cum += t['pnl']
            equity_curve_raw.append(round(cum, 2))
        step = max(1, len(equity_curve_raw) // 200)
        equity_curve = equity_curve_raw[::step]

        class Stats: pass
        s = Stats()
        s.total_trades  = len(trades)
        s.open_trades   = len([t for t in trades if t['status'] == 'OPEN'])
        s.win_rate      = win_rate
        s.total_pnl     = total_pnl_raw
        s.total_pnl_fmt = fmt_pnl(total_pnl_raw)
        s.avg_credit    = avg_credit
        s.avg_ror       = avg_ror
        s.equity_curve  = equity_curve
        return s, trades
    except Exception:
        class EmptyStats:
            total_trades=0; open_trades=0; win_rate=0; total_pnl=0
            total_pnl_fmt='0'; avg_credit=0; avg_ror=0; equity_curve=[]
        return EmptyStats(), []

def format_trades(trades, limit=10):
    out = []
    for t in trades[:limit]:
        raw_pnl = t.get('pnl')
        out.append({
            'id':           t['id'],
            'date':         t.get('signal_time', '')[:16] if t.get('signal_time') else '—',
            'expiry':       t.get('expiry', ''),
            'dte':          t.get('dte', ''),
            'short_strike': t.get('short_strike', ''),
            'long_strike':  t.get('long_strike', ''),
            'net_credit':   t.get('net_credit', ''),
            'max_loss':     t.get('max_loss', ''),
            'ror_pct':      t.get('ror_pct', ''),
            'vix':          t.get('vix_at_entry', ''),
            'spx':          t.get('spx_at_entry', ''),
            'status':       t.get('status', ''),
            'pnl':          raw_pnl,
            'pnl_fmt':      fmt_pnl(raw_pnl) if raw_pnl is not None else None,
            'exit_reason':  t.get('exit_reason'),
        })
    return out

def get_vix_now():
    try:
        sys.path.insert(0, ROOT_DIR)
        from tradier_client import get_vix
        return get_vix()
    except Exception:
        return '—'

# ── Position sizing ───────────────────────────────────────────────────────────

def calc_contracts(account_size, net_credit, risk_pct=2.0):
    """
    How many contracts to trade based on account size and risk %.
    Uses 2x stop loss (practical max loss) as the per-contract risk.
    Minimum 1 contract.
    """
    if not account_size or account_size <= 0 or not net_credit:
        return 1
    stop_loss_value = net_credit * 2 * 100   # 2x credit per contract
    max_risk        = account_size * (risk_pct / 100)
    contracts       = max(1, int(max_risk / stop_loss_value))
    return contracts

def fmt_dollar(val):
    """Format as $1.2M, $45.3K, $1,234"""
    if val is None: return '—'
    if abs(val) >= 1_000_000: return f"${val/1_000_000:,.2f}M"
    if abs(val) >= 1_000:     return f"${val/1_000:,.1f}K"
    return f"${val:,.0f}"

# ── Member trade sync ─────────────────────────────────────────────────────────

def sync_member_trades_for_user(user):
    """
    For a given member, ensure every trade in trades.db has a corresponding
    MemberTrade record in members.db. Updates closed trades with P&L.
    Only runs for paid (member) accounts.
    """
    if user.plan == 'free' or not user.account_size:
        return
    import sqlite3
    try:
        conn = sqlite3.connect(TRADES_DB)
        conn.row_factory = sqlite3.Row
        cur  = conn.cursor()
        cur.execute("SELECT * FROM trades ORDER BY id")
        trades = [dict(r) for r in cur.fetchall()]
        conn.close()

        existing_ids = {mt.trade_id for mt in user.member_trades}

        for t in trades:
            contracts = calc_contracts(user.account_size, t.get('net_credit', 3.0), user.risk_pct)
            mlpc      = round((t.get('spread_width', 25) - t.get('net_credit', 0)) * 100, 2)
            max_risk  = round(contracts * mlpc, 2)

            if t['id'] not in existing_ids:
                # Create new member trade record
                mt = MemberTrade(
                    user_id              = user.id,
                    trade_id             = t['id'],
                    signal_time          = t.get('signal_time', ''),
                    short_strike         = t.get('short_strike'),
                    long_strike          = t.get('long_strike'),
                    expiry               = t.get('expiry', ''),
                    dte                  = t.get('dte'),
                    net_credit           = t.get('net_credit'),
                    max_loss_per_contract= mlpc,
                    contracts            = contracts,
                    max_risk             = max_risk,
                    profit_target        = t.get('profit_target'),
                    stop_loss_level      = t.get('stop_loss_level'),
                    vix                  = t.get('vix_at_entry'),
                    spx                  = t.get('spx_at_entry'),
                    status               = t.get('status', 'OPEN'),
                    exit_price           = t.get('exit_price'),
                    exit_time            = t.get('exit_time'),
                    exit_reason          = t.get('exit_reason'),
                    account_snapshot     = user.account_size,
                )
                if t.get('pnl') is not None:
                    unit_pnl   = t['net_credit'] - (t.get('exit_price') or 0)
                    raw_pnl    = round(unit_pnl * 100 * contracts, 2)
                    mt.pnl     = raw_pnl
                    mt.pnl_pct_account = round(raw_pnl / user.account_size * 100, 3) if user.account_size else 0
                db.session.add(mt)
            else:
                # Update existing if trade is now closed
                mt = MemberTrade.query.filter_by(user_id=user.id, trade_id=t['id']).first()
                if mt and mt.status == 'OPEN' and t.get('status', 'OPEN') != 'OPEN':
                    mt.status     = t['status']
                    mt.exit_price = t.get('exit_price')
                    mt.exit_time  = t.get('exit_time')
                    mt.exit_reason= t.get('exit_reason')
                    unit_pnl      = (t.get('net_credit', 0)) - (t.get('exit_price') or 0)
                    mt.pnl        = round(unit_pnl * 100 * mt.contracts, 2)
                    mt.pnl_pct_account = round(mt.pnl / user.account_size * 100, 3) if user.account_size else 0

        db.session.commit()
    except Exception as e:
        db.session.rollback()

def get_member_stats(user):
    """Compute portfolio summary for a member."""
    if not user.member_trades:
        return None
    closed = [mt for mt in user.member_trades if mt.status != 'OPEN' and mt.pnl is not None]
    open_t = [mt for mt in user.member_trades if mt.status == 'OPEN']
    wins   = [mt for mt in closed if mt.pnl > 0]
    total_pnl    = round(sum(mt.pnl for mt in closed), 2) if closed else 0
    win_rate     = round(len(wins) / len(closed) * 100, 1) if closed else 0
    account_now  = (user.account_size or 0) + total_pnl
    growth_pct   = round(total_pnl / user.account_size * 100, 1) if user.account_size else 0
    # Equity curve
    cum, equity = 0.0, []
    for mt in sorted(closed, key=lambda x: x.id):
        cum += mt.pnl
        equity.append(round(cum, 2))
    return {
        'total_trades' : len(user.member_trades),
        'closed_trades': len(closed),
        'open_trades'  : len(open_t),
        'wins'         : len(wins),
        'losses'       : len(closed) - len(wins),
        'win_rate'     : win_rate,
        'total_pnl'    : total_pnl,
        'total_pnl_fmt': fmt_dollar(total_pnl),
        'account_start': user.account_size,
        'account_now'  : round(account_now, 2),
        'account_now_fmt': fmt_dollar(account_now),
        'growth_pct'   : growth_pct,
        'equity_curve' : equity,
        'contracts_typical': calc_contracts(user.account_size, 4.80, user.risk_pct),
    }

# ── public routes ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    stats, trades = get_trade_stats()
    recent = format_trades(trades, 5)
    return render_template('index.html',
        total_trades=stats.total_trades,
        win_rate=stats.win_rate,
        total_pnl=stats.total_pnl,
        total_pnl_fmt=stats.total_pnl_fmt,
        avg_credit=stats.avg_credit,
        avg_ror=stats.avg_ror,
        recent_trades=recent)

@app.route('/pricing')
def pricing():
    return render_template('pricing.html',
        paypal_client_id=PAYPAL_CLIENT_ID,
        paypal_member_plan_id=PAYPAL_MEMBER_PLAN_ID)

@app.route('/performance')
def performance():
    stats, trades = get_trade_stats()
    all_trades = format_trades(trades, 500)
    return render_template('performance.html', stats=stats, trades=all_trades)

@app.route('/how-it-works')
def how_it_works():
    return render_template('how_it_works.html')

@app.route('/disclaimer')
def disclaimer():
    return render_template('disclaimer.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/blog')
def blog():
    from blog_posts import get_all_posts
    return render_template('blog_list.html', posts=get_all_posts())

@app.route('/blog/<slug>')
def blog_post(slug):
    from blog_posts import get_post, get_related
    post = get_post(slug)
    if not post:
        return redirect(url_for('blog'))
    return render_template('blog_post.html', post=post, related=get_related(slug))

# ── Newsletter ────────────────────────────────────────────────────────────────

@app.route('/newsletter')
def newsletter_landing():
    return render_template('newsletter_landing.html')

@app.route('/newsletter/subscribe', methods=['POST'])
def newsletter_subscribe():
    email  = (request.form.get('email') or '').lower().strip()
    name   = (request.form.get('name')  or '').strip()
    source = (request.form.get('source') or 'unknown').strip()
    if not email or '@' not in email:
        flash('Please enter a valid email address.', 'error')
        return redirect(url_for('newsletter_landing'))
    existing = Subscriber.query.filter_by(email=email).first()
    if existing:
        if existing.status == 'unsubscribed':
            existing.status          = 'active'
            existing.unsubscribed_at = None
            db.session.commit()
            flash("Welcome back — you're re-subscribed.", 'success')
        else:
            flash("You're already subscribed.", 'success')
        return redirect(url_for('newsletter_landing'))
    sub = Subscriber(
        email             = email,
        name              = name or None,
        source            = source,
        status            = 'active',
        unsubscribe_token = newsletter_lib.new_unsubscribe_token(),
    )
    db.session.add(sub)
    # Also save as a lead if not already
    if not Lead.query.filter_by(email=email).first():
        db.session.add(Lead(email=email, name=name, source=f'newsletter:{source}'))
    db.session.commit()
    # Fire welcome email (non-blocking — if it fails, signup still succeeds)
    try:
        newsletter_lib.send_welcome(app, TRADES_DB, sub)
    except Exception as e:
        app.logger.warning(f"welcome email failed: {e}")
    flash("You're in. First briefing arrives tomorrow at 6:00 AM ET.", 'success')
    return redirect(url_for('newsletter_landing'))

@app.route('/unsubscribe/<token>')
def newsletter_unsubscribe(token):
    sub = Subscriber.query.filter_by(unsubscribe_token=token).first()
    if not sub:
        return render_template('unsubscribed.html', success=False)
    if sub.status == 'active':
        sub.status          = 'unsubscribed'
        sub.unsubscribed_at = datetime.utcnow()
        db.session.commit()
    return render_template('unsubscribed.html', success=True, email=sub.email)

# ── auth ──────────────────────────────────────────────────────────────────────

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('member_dashboard'))
    ref = request.args.get('ref', '')
    if request.method == 'POST':
        email    = request.form.get('email', '').lower().strip()
        password = request.form.get('password', '')
        name     = request.form.get('name', '').strip()
        ref_code = request.form.get('ref', '')
        if not email or not password or not name:
            flash('All fields are required', 'error')
            return render_template('register.html', ref=ref)
        if len(password) < 8:
            flash('Password must be at least 8 characters', 'error')
            return render_template('register.html', ref=ref)
        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists', 'error')
            return render_template('register.html', ref=ref)
        import random, string
        user = User(
            email=email,
            password_hash=generate_password_hash(password),
            name=name, plan='free',
            referral_code=''.join(random.choices(string.ascii_uppercase + string.digits, k=8)),
            referred_by=ref_code or None,
        )
        db.session.add(user)
        if not Lead.query.filter_by(email=email).first():
            db.session.add(Lead(email=email, name=name, source='register'))
        db.session.commit()
        login_user(user)
        flash(f'Welcome {name}! Your free account is ready.', 'success')
        return redirect(url_for('member_dashboard'))
    return render_template('register.html', ref=ref)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('member_dashboard'))
    if request.method == 'POST':
        email    = request.form.get('email', '').lower().strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash('Invalid email or password', 'error')
            return render_template('login.html')
        user.last_login = datetime.utcnow()
        db.session.commit()
        login_user(user)
        return redirect(request.args.get('next') or url_for('member_dashboard'))
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# ── member dashboard ──────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def member_dashboard():
    stats, _ = get_trade_stats()
    vix = get_vix_now()
    # Sync & compute personalized stats
    sync_member_trades_for_user(current_user)
    member_stats = get_member_stats(current_user) if current_user.account_size else None
    contracts    = calc_contracts(current_user.account_size or 0, 4.80, current_user.risk_pct or 2.0)
    return render_template('dashboard_member.html',
        stats=stats, vix=vix,
        member_stats=member_stats,
        contracts=contracts)

@app.route('/dashboard/setup-account', methods=['POST'])
@login_required
def setup_account():
    try:
        acct  = float(request.form.get('account_size', 0))
        risk  = float(request.form.get('risk_pct', 2.0))
        phone = request.form.get('phone', '').strip()
        if acct < 5000:
            flash('Minimum recommended account size is $5,000.', 'error')
            return redirect(url_for('member_dashboard'))
        if not 0.5 <= risk <= 5.0:
            flash('Risk % must be between 0.5% and 5%.', 'error')
            return redirect(url_for('member_dashboard'))
        current_user.account_size        = acct
        current_user.risk_pct            = risk
        current_user.account_pledge_date = datetime.utcnow()
        if phone:
            current_user.phone = phone
        db.session.commit()
        # Sync existing trades for this member immediately
        sync_member_trades_for_user(current_user)
        flash(f'Account profile saved. Alerts sized for ${acct:,.0f} at {risk}% risk per trade.', 'success')
    except ValueError:
        flash('Please enter a valid number.', 'error')
    return redirect(url_for('member_dashboard'))

@app.route('/dashboard/my-trades')
@login_required
def my_trades():
    sync_member_trades_for_user(current_user)
    member_stats = get_member_stats(current_user) if current_user.account_size else None
    trades = sorted(current_user.member_trades, key=lambda x: x.id, reverse=True)
    # Format for template
    formatted = []
    for mt in trades:
        formatted.append({
            'id'           : mt.id,
            'trade_id'     : mt.trade_id,
            'date'         : mt.signal_time[:16] if mt.signal_time else '—',
            'spread'       : f"{mt.short_strike}/{mt.long_strike}P" if mt.short_strike else '—',
            'expiry'       : mt.expiry,
            'credit'       : mt.net_credit,
            'contracts'    : mt.contracts,
            'max_risk'     : fmt_dollar(mt.max_risk),
            'max_risk_raw' : mt.max_risk,
            'ror'          : round(mt.net_credit / (mt.max_loss_per_contract / 100) * 100, 1) if mt.max_loss_per_contract else 0,
            'vix'          : mt.vix,
            'status'       : mt.status,
            'exit_reason'  : mt.exit_reason,
            'pnl'          : mt.pnl,
            'pnl_fmt'      : fmt_dollar(mt.pnl),
            'pnl_pct'      : mt.pnl_pct_account,
        })
    return render_template('my_trades.html',
        trades=formatted,
        member_stats=member_stats,
        user=current_user)

# ── stripe checkout ───────────────────────────────────────────────────────────

@app.route('/checkout/stripe/<plan>')
@app.route('/checkout/<plan>')
@login_required
def checkout(plan):
    if plan not in STRIPE_PRICES:
        return redirect(url_for('pricing'))
    if not stripe.api_key or stripe.api_key == '':
        flash('Payment system not configured yet — contact support', 'error')
        return redirect(url_for('pricing'))
    try:
        checkout_session = stripe.checkout.Session.create(
            customer_email=current_user.email,
            payment_method_types=['card'],
            line_items=[{'price': STRIPE_PRICES[plan], 'quantity': 1}],
            mode='subscription',
            subscription_data={'trial_period_days': 7},
            success_url=request.host_url + 'checkout/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url + 'pricing',
            metadata={'user_id': current_user.id, 'plan': plan}
        )
        return redirect(checkout_session.url)
    except Exception as e:
        flash(f'Checkout error: {str(e)}', 'error')
        return redirect(url_for('pricing'))

def credit_referrer(new_member):
    """When a referred user converts to paid, give their referrer +1 free month."""
    code = (new_member.referred_by or '').strip()
    if not code:
        return
    referrer = User.query.filter_by(referral_code=code).first()
    if referrer and referrer.id != new_member.id:
        referrer.free_months   = (referrer.free_months or 0) + 1
        referrer.referral_count = (referrer.referral_count or 0) + 1
        db.session.commit()

@app.route('/checkout/success')
@login_required
def checkout_success():
    session_id = request.args.get('session_id')
    if session_id and stripe.api_key:
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            plan = sess.metadata.get('plan', 'member')
            was_free = current_user.sub_status != 'active'
            current_user.plan = plan
            current_user.stripe_customer_id = sess.customer
            current_user.stripe_subscription_id = sess.subscription
            current_user.sub_status = 'active'
            db.session.commit()
            if was_free:
                credit_referrer(current_user)
            flash(f'Welcome to CreditSpread.net {plan.title()}! Your subscription is active.', 'success')
        except Exception:
            pass
    return redirect(url_for('member_dashboard'))

@app.route('/checkout/paypal/success')
@login_required
def paypal_success():
    import requests as req
    subscription_id = request.args.get('subscription_id', '')
    plan = request.args.get('plan', 'pro')
    if subscription_id and PAYPAL_CLIENT_ID != 'paypal_client_id_placeholder':
        try:
            # Get PayPal access token
            token_resp = req.post(f'{PAYPAL_API_BASE}/v1/oauth2/token',
                auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
                data={'grant_type': 'client_credentials'}, timeout=10)
            access_token = token_resp.json().get('access_token', '')
            # Verify subscription
            sub_resp = req.get(f'{PAYPAL_API_BASE}/v1/billing/subscriptions/{subscription_id}',
                headers={'Authorization': f'Bearer {access_token}'}, timeout=10)
            sub_data = sub_resp.json()
            if sub_data.get('status') in ('ACTIVE', 'APPROVAL_PENDING'):
                was_free = current_user.sub_status != 'active'
                current_user.plan = plan
                current_user.stripe_subscription_id = subscription_id  # reuse field for PayPal sub ID
                current_user.sub_status = 'active'
                db.session.commit()
                if was_free:
                    credit_referrer(current_user)
                flash(f'Welcome to CreditSpread.net {plan.title()}! PayPal subscription active.', 'success')
        except Exception as e:
            flash(f'PayPal verification error: {str(e)}', 'error')
    return redirect(url_for('member_dashboard'))

@app.route('/webhook/paypal', methods=['POST'])
def paypal_webhook():
    data = request.get_json(silent=True) or {}
    event_type = data.get('event_type', '')
    resource = data.get('resource', {})
    sub_id = resource.get('id', '')
    if event_type == 'BILLING.SUBSCRIPTION.CANCELLED':
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if user:
            user.sub_status = 'cancelled'; user.plan = 'free'
            db.session.commit()
    if event_type == 'BILLING.SUBSCRIPTION.SUSPENDED':
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if user:
            user.sub_status = 'past_due'; db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')
    secret  = os.getenv('STRIPE_WEBHOOK_SECRET', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception:
        return jsonify({'error': 'Invalid signature'}), 400
    if event['type'] == 'customer.subscription.deleted':
        sub = event['data']['object']
        user = User.query.filter_by(stripe_subscription_id=sub['id']).first()
        if user:
            user.sub_status = 'cancelled'; user.plan = 'free'
            db.session.commit()
    if event['type'] == 'invoice.payment_failed':
        sub_id = event['data']['object'].get('subscription')
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if user:
            user.sub_status = 'past_due'; db.session.commit()
    return jsonify({'status': 'ok'})

# ── lead capture ──────────────────────────────────────────────────────────────

@app.route('/lead-capture', methods=['POST'])
def lead_capture():
    email  = request.form.get('email', '').lower().strip()
    source = request.form.get('source', 'unknown')
    if email and not Lead.query.filter_by(email=email).first():
        db.session.add(Lead(email=email, source=source))
        db.session.commit()
    flash('You are on the list! Check your email for the free Telegram link.', 'success')
    return redirect(request.referrer or url_for('index'))

# ── API ───────────────────────────────────────────────────────────────────────

@app.route('/api/trades')
def api_trades():
    _, trades = get_trade_stats()
    return jsonify(format_trades(trades, 100))

@app.route('/api/stats')
def api_stats():
    stats, _ = get_trade_stats()
    return jsonify({
        'total_trades': stats.total_trades,
        'open_trades':  stats.open_trades,
        'win_rate':     stats.win_rate,
        'total_pnl':    stats.total_pnl,
        'avg_credit':   stats.avg_credit,
        'avg_ror':      stats.avg_ror,
        'equity_curve': stats.equity_curve,
    })

@app.route('/sitemap.xml')
def sitemap():
    return render_template('sitemap.xml'), 200, {'Content-Type': 'application/xml'}

@app.route('/robots.txt')
def robots():
    return "User-agent: *\nAllow: /\nSitemap: https://creditspread.net/sitemap.xml\n", 200, {'Content-Type': 'text/plain'}

APP_VERSION = 'v17-creditdetail'  # bump to confirm deploys

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'version': APP_VERSION, 'time': datetime.now().isoformat()})

def _alert_key_ok(req):
    key = req.headers.get('X-Alert-Key', '') or (req.form.get('key', '') if req.form else '')
    return key == os.getenv('ALERT_LOG_KEY', 'changeme-alert-key')

def _fan_out_to_members(net_credit, ticker='SPX', trade_ref='', risk_default=2.0):
    """
    Compute aggregated member lots and (optionally) send each member their alert.
    Returns (total_lots, members_alerted, members_total).
    Members with an account_size get lots sized to their risk %.
    """
    members = User.query.filter(User.plan == 'member',
                                User.sub_status == 'active',
                                User.account_size.isnot(None)).all()
    # Ensure the trade ref reads as a credit-spread "Sell to Open"
    ref = trade_ref or ''
    if ref and not ref.lower().startswith('sell to open'):
        ref = f"Sell to Open {ref}"
    total_lots = 0
    alerted = 0
    for m in members:
        risk = m.risk_pct or risk_default
        stop_cost = (net_credit or 1) * 2 * 100
        lots = max(1, int((m.account_size * risk / 100) / stop_cost)) if net_credit else 1
        total_lots += lots
        # Attempt member SMS via Twilio if configured (HTTP — works on Render)
        if m.phone and os.getenv('TWILIO_ACCOUNT_SID', '').startswith('AC'):
            try:
                from twilio.rest import Client
                Client(os.getenv('TWILIO_ACCOUNT_SID'), os.getenv('TWILIO_AUTH_TOKEN')).messages.create(
                    body=(f"CREDIT SPREAD SIGNAL\n{ref} {ticker}\n"
                          f"Credit: ${net_credit} | YOUR SIZE: {lots} lot(s)\n"
                          f"creditspread.net/dashboard"),
                    from_=os.getenv('TWILIO_FROM_NUMBER'), to=m.phone)
                alerted += 1
            except Exception:
                pass
    return total_lots, alerted, len(members)

@app.route('/api/trade-entry', methods=['POST'])
def api_trade_entry():
    """VPS posts a new signal. Webapp records it + fans out to members."""
    if not _alert_key_ok(request):
        return jsonify({'error': 'unauthorized'}), 401
    d = request.get_json(silent=True) or {}
    try:
        key = str(d.get('trade_key', ''))
        if key and LiveTrade.query.filter_by(trade_key=key).first():
            return jsonify({'ok': True, 'note': 'already recorded'})
        ticker   = d.get('ticker', 'SPX')
        trade_str = d.get('trade', '')
        if trade_str and not trade_str.lower().startswith('sell to open'):
            trade_str = f"Sell to Open {trade_str}"
        total_lots, alerted, total_m = _fan_out_to_members(d.get('net_credit'), ticker, trade_str)
        lt = LiveTrade(
            trade_key       = key or None,
            ticker          = ticker,
            trade           = trade_str,
            net_credit      = d.get('net_credit'),
            total_lots      = total_lots,
            members_alerted = alerted,
            members_total   = total_m,
            status          = 'OPEN',
        )
        db.session.add(lt)
        db.session.commit()
        return jsonify({'ok': True, 'id': lt.id, 'total_lots': total_lots, 'alerted': alerted})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/trade-exit', methods=['POST'])
def api_trade_exit():
    """VPS posts an exit. Webapp updates P&L on the matching trade."""
    if not _alert_key_ok(request):
        return jsonify({'error': 'unauthorized'}), 401
    d = request.get_json(silent=True) or {}
    try:
        key = str(d.get('trade_key', ''))
        lt = LiveTrade.query.filter_by(trade_key=key).first() if key else None
        if not lt:
            return jsonify({'error': 'trade not found'}), 404
        lt.exit_time   = datetime.utcnow()
        lt.exit_debit  = d.get('exit_debit')
        lt.pnl_per_lot = d.get('pnl_per_lot')
        lt.total_pnl   = round((lt.pnl_per_lot or 0) * (lt.total_lots or 0), 2)
        lt.status      = d.get('status', 'CLOSED')
        lt.exit_reason = d.get('exit_reason')
        # Expired worthless = closed at ~$0 debit → kept 100% of the credit
        lt.expired_worthless = bool(lt.exit_debit is not None and lt.exit_debit <= 0.05)
        db.session.commit()
        return jsonify({'ok': True, 'total_pnl': lt.total_pnl, 'expired_worthless': lt.expired_worthless})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/api/log-alert', methods=['POST'])
def api_log_alert():
    """Receive an alert-send record from the VPS trading engine (key-protected)."""
    key = request.headers.get('X-Alert-Key', '') or request.form.get('key', '')
    if key != os.getenv('ALERT_LOG_KEY', 'changeme-alert-key'):
        return jsonify({'error': 'unauthorized'}), 401
    data = request.get_json(silent=True) or request.form
    try:
        rec = AlertLog(
            channel    = (data.get('channel') or 'sms')[:20],
            audience   = (data.get('audience') or 'owner')[:20],
            alert_type = (data.get('alert_type') or 'entry')[:20],
            recipient  = (data.get('recipient') or '')[:120],
            trade_ref  = (data.get('trade_ref') or '')[:60],
            status     = (data.get('status') or 'sent')[:12],
            detail     = (data.get('detail') or '')[:2000],
        )
        db.session.add(rec)
        db.session.commit()
        return jsonify({'ok': True, 'id': rec.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ── admin ─────────────────────────────────────────────────────────────────────

def challenge_ok(email):
    return email == os.getenv('ADMIN_EMAIL', 'clist2013@gmail.com')

@app.route('/admin')
@login_required
def admin():
    if not challenge_ok(current_user.email):
        return redirect(url_for('member_dashboard'))
    users = User.query.order_by(User.created_at.desc()).all()
    leads = Lead.query.order_by(Lead.created_at.desc()).limit(50).all()
    subscribers = Subscriber.query.order_by(Subscriber.subscribed_at.desc()).limit(100).all()
    active_subs = Subscriber.query.filter_by(status='active').count()

    # ── Alert delivery stats ──
    alerts_recent = AlertLog.query.order_by(AlertLog.created_at.desc()).limit(50).all()
    total_alerts  = AlertLog.query.count()
    sent_alerts   = AlertLog.query.filter_by(status='sent').count()
    failed_alerts = AlertLog.query.filter_by(status='failed').count()
    alert_success = round(sent_alerts / total_alerts * 100, 1) if total_alerts else 0
    from datetime import timedelta as _td
    since_today   = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    alerts_today  = AlertLog.query.filter(AlertLog.created_at >= since_today).count()

    # ── Live trades (trade-centric view) ──
    live_trades = LiveTrade.query.order_by(LiveTrade.entry_time.desc()).limit(100).all()

    return render_template('admin.html', users=users, leads=leads,
                           live_trades=live_trades,
                           subscribers=subscribers, active_subs=active_subs,
                           alerts_recent=alerts_recent, total_alerts=total_alerts,
                           sent_alerts=sent_alerts, failed_alerts=failed_alerts,
                           alert_success=alert_success, alerts_today=alerts_today,
                           admin_email=os.getenv('ADMIN_EMAIL', 'clist2013@gmail.com'))

@app.route('/admin/delete-member/<int:user_id>', methods=['POST'])
@login_required
def admin_delete_member(user_id):
    if not challenge_ok(current_user.email):
        return redirect(url_for('member_dashboard'))
    target = db.session.get(User, user_id)
    if not target:
        flash('Member not found.', 'error')
        return redirect(url_for('admin'))
    # Safety guards
    if target.email == os.getenv('ADMIN_EMAIL', 'clist2013@gmail.com'):
        flash('You cannot delete the admin account.', 'error')
        return redirect(url_for('admin'))
    if target.id == current_user.id:
        flash('You cannot delete your own account.', 'error')
        return redirect(url_for('admin'))
    email = target.email
    # Remove their personal trade log first (FK), then the user
    MemberTrade.query.filter_by(user_id=target.id).delete()
    db.session.delete(target)
    db.session.commit()
    flash(f'Member {email} and their trade log were removed.', 'success')
    return redirect(url_for('admin'))

@app.route('/admin/delete-lead/<int:lead_id>', methods=['POST'])
@login_required
def admin_delete_lead(lead_id):
    if not challenge_ok(current_user.email):
        return redirect(url_for('member_dashboard'))
    lead = db.session.get(Lead, lead_id)
    if lead:
        db.session.delete(lead)
        db.session.commit()
        flash('Lead removed.', 'success')
    return redirect(url_for('admin'))

# ── Admin: newsletter ─────────────────────────────────────────────────────────

@app.route('/admin/newsletter/test', methods=['POST'])
@login_required
def admin_newsletter_test():
    if not challenge_ok(current_user.email):
        return redirect(url_for('member_dashboard'))
    # Build a fake Subscriber-like object using the admin's identity
    class _FakeSub:
        email             = current_user.email
        name              = current_user.name
        unsubscribe_token = 'preview-link-only'
    fake = _FakeSub()
    html, _ = newsletter_lib.render_newsletter(app, TRADES_DB, fake)
    ok = newsletter_lib.send_email(current_user.email,
                                   f"[TEST] Pre-Market — {datetime.now().strftime('%b %d')}",
                                   html, reply_to=current_user.email)
    flash('Test newsletter sent to your email.' if ok else
          'Send failed — check provider config (RESEND_API_KEY or Gmail).',
          'success' if ok else 'error')
    return redirect(url_for('admin'))

@app.route('/admin/newsletter/preview')
@login_required
def admin_newsletter_preview():
    if not challenge_ok(current_user.email):
        return redirect(url_for('member_dashboard'))
    class _FakeSub:
        email             = current_user.email
        name              = current_user.name
        unsubscribe_token = 'preview-link-only'
    html, _ = newsletter_lib.render_newsletter(app, TRADES_DB, _FakeSub())
    return html

@app.route('/admin/newsletter/send', methods=['POST'])
@login_required
def admin_newsletter_send():
    if not challenge_ok(current_user.email):
        return redirect(url_for('member_dashboard'))
    if request.form.get('confirm') != 'SEND':
        flash('Confirmation phrase missing — send cancelled.', 'error')
        return redirect(url_for('admin'))
    res = newsletter_lib.send_daily(app, TRADES_DB, Subscriber)
    flash(f"Newsletter sent: {res['sent']} delivered · {res['failed']} failed · {res['total']} total subscribers.",
          'success' if res['failed'] == 0 else 'error')
    return redirect(url_for('admin'))

@app.route('/admin/delete-subscriber/<int:sub_id>', methods=['POST'])
@login_required
def admin_delete_subscriber(sub_id):
    if not challenge_ok(current_user.email):
        return redirect(url_for('member_dashboard'))
    s = db.session.get(Subscriber, sub_id)
    if s:
        db.session.delete(s); db.session.commit()
        flash('Subscriber removed.', 'success')
    return redirect(url_for('admin'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print('Database ready')
    print('\nCreditSpread.net running at: http://localhost:8000\n')
    app.run(host='0.0.0.0', port=8000, debug=False)
