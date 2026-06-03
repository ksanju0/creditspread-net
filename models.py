from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id                     = db.Column(db.Integer, primary_key=True)
    email                  = db.Column(db.String(120), unique=True, nullable=False)
    password_hash          = db.Column(db.String(256))
    name                   = db.Column(db.String(100))
    plan                   = db.Column(db.String(20), default='free')
    stripe_customer_id     = db.Column(db.String(100))
    stripe_subscription_id = db.Column(db.String(100))
    sub_status             = db.Column(db.String(20), default='inactive')
    sub_end_date           = db.Column(db.DateTime)
    created_at             = db.Column(db.DateTime, default=datetime.utcnow)
    last_login             = db.Column(db.DateTime)
    referral_code          = db.Column(db.String(20))
    referred_by            = db.Column(db.String(20))
    free_months            = db.Column(db.Integer, default=0)   # earned via referrals
    referral_count         = db.Column(db.Integer, default=0)   # successful conversions
    # ── Personalized alert fields ─────────────────────────────────────────────
    account_size           = db.Column(db.Float,   nullable=True)   # pledged trading capital
    account_pledge_date    = db.Column(db.DateTime, nullable=True)  # when they set it
    risk_pct               = db.Column(db.Float,   default=2.0)     # % of account per trade
    phone                  = db.Column(db.String(20), nullable=True)
    telegram_chat_id       = db.Column(db.String(40), nullable=True)
    # ── Relationships ─────────────────────────────────────────────────────────
    member_trades = db.relationship('MemberTrade', backref='user', lazy=True)

class MemberTrade(db.Model):
    """Personal trade log for each member — tracks every alert against their account size."""
    __tablename__ = 'member_trades'
    id                   = db.Column(db.Integer, primary_key=True)
    user_id              = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    trade_id             = db.Column(db.Integer, nullable=False)   # ref to trades.db
    signal_time          = db.Column(db.String(30))
    short_strike         = db.Column(db.Float)
    long_strike          = db.Column(db.Float)
    expiry               = db.Column(db.String(20))
    dte                  = db.Column(db.Integer)
    net_credit           = db.Column(db.Float)
    max_loss_per_contract= db.Column(db.Float)
    contracts            = db.Column(db.Integer, default=1)
    max_risk             = db.Column(db.Float)    # contracts × max_loss_per_contract
    profit_target        = db.Column(db.Float)
    stop_loss_level      = db.Column(db.Float)
    vix                  = db.Column(db.Float)
    spx                  = db.Column(db.Float)
    status               = db.Column(db.String(20), default='OPEN')
    exit_price           = db.Column(db.Float,  nullable=True)
    exit_time            = db.Column(db.String(30), nullable=True)
    exit_reason          = db.Column(db.String(50), nullable=True)
    pnl                  = db.Column(db.Float,  nullable=True)     # contracts × unit_pnl × 100
    pnl_pct_account      = db.Column(db.Float,  nullable=True)     # pnl / account_size × 100
    account_snapshot     = db.Column(db.Float,  nullable=True)     # account_size at trade time
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)

class Lead(db.Model):
    __tablename__ = 'leads'
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(120), unique=True)
    name       = db.Column(db.String(100))
    source     = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    converted  = db.Column(db.Boolean, default=False)

class Subscriber(db.Model):
    """Newsletter subscriber — separate from generic Lead capture."""
    __tablename__ = 'subscribers'
    id                = db.Column(db.Integer, primary_key=True)
    email             = db.Column(db.String(120), unique=True, nullable=False, index=True)
    name              = db.Column(db.String(100))
    source            = db.Column(db.String(50))           # homepage, /newsletter, register
    status            = db.Column(db.String(20), default='active')   # active / unsubscribed / bounced
    unsubscribe_token = db.Column(db.String(40), unique=True)
    subscribed_at     = db.Column(db.DateTime, default=datetime.utcnow)
    unsubscribed_at   = db.Column(db.DateTime, nullable=True)
    last_sent_at      = db.Column(db.DateTime, nullable=True)
    send_count        = db.Column(db.Integer, default=0)

class SocialPost(db.Model):
    __tablename__ = 'social_posts'
    id         = db.Column(db.Integer, primary_key=True)
    platform   = db.Column(db.String(20))
    content    = db.Column(db.Text)
    posted_at  = db.Column(db.DateTime, default=datetime.utcnow)
    post_id    = db.Column(db.String(100))
    trade_id   = db.Column(db.Integer)
    post_type  = db.Column(db.String(30))
