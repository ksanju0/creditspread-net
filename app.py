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

from models import db, User, Lead, SocialPost, MemberTrade

app = Flask(__name__)
CORS(app)
app.secret_key = os.getenv('SECRET_KEY', 'creditspread-secret-2026-change-me')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{MEMBERS_DB}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

def run_migrations():
    """
    Lightweight auto-migration. SQLAlchemy create_all() won't ALTER existing
    tables, so we add any missing columns/tables here. Safe to run on every boot.
    """
    import sqlite3
    db_path = MEMBERS_DB
    try:
        conn = sqlite3.connect(db_path)
        cur  = conn.cursor()

        # Ensure tables exist (no-op if already there)
        with app.app_context():
            db.create_all()

        # Add any missing columns to users
        cur.execute("PRAGMA table_info(users)")
        existing_cols = {row[1] for row in cur.fetchall()}
        new_cols = {
            'account_size':        'FLOAT',
            'account_pledge_date': 'DATETIME',
            'risk_pct':            'FLOAT DEFAULT 2.0',
            'phone':               'VARCHAR(20)',
            'telegram_chat_id':    'VARCHAR(40)',
        }
        for col, coltype in new_cols.items():
            if col not in existing_cols:
                cur.execute(f"ALTER TABLE users ADD COLUMN {col} {coltype}")
                print(f"[migration] Added users.{col}")

        conn.commit()
        conn.close()
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

@app.route('/checkout/success')
@login_required
def checkout_success():
    session_id = request.args.get('session_id')
    if session_id and stripe.api_key:
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            plan = sess.metadata.get('plan', 'pro')
            current_user.plan = plan
            current_user.stripe_customer_id = sess.customer
            current_user.stripe_subscription_id = sess.subscription
            current_user.sub_status = 'active'
            db.session.commit()
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
                current_user.plan = plan
                current_user.stripe_subscription_id = subscription_id  # reuse field for PayPal sub ID
                current_user.sub_status = 'active'
                db.session.commit()
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

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'time': datetime.now().isoformat()})

# ── admin ─────────────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin():
    if current_user.email != os.getenv('ADMIN_EMAIL', 'clist2013@gmail.com'):
        return redirect(url_for('member_dashboard'))
    users = User.query.order_by(User.created_at.desc()).all()
    leads = Lead.query.order_by(Lead.created_at.desc()).limit(50).all()
    return render_template('admin.html', users=users, leads=leads)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print('Database ready')
    print('\nCreditSpread.net running at: http://localhost:8000\n')
    app.run(host='0.0.0.0', port=8000, debug=False)
