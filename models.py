from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    name          = db.Column(db.String(100))
    plan          = db.Column(db.String(20), default='free')   # free/starter/pro/elite
    stripe_customer_id    = db.Column(db.String(100))
    stripe_subscription_id= db.Column(db.String(100))
    sub_status    = db.Column(db.String(20), default='inactive')  # active/inactive/cancelled
    sub_end_date  = db.Column(db.DateTime)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    last_login    = db.Column(db.DateTime)
    referral_code = db.Column(db.String(20))
    referred_by   = db.Column(db.String(20))

class Lead(db.Model):
    __tablename__ = 'leads'
    id         = db.Column(db.Integer, primary_key=True)
    email      = db.Column(db.String(120), unique=True)
    name       = db.Column(db.String(100))
    source     = db.Column(db.String(50))   # landing/twitter/reddit/telegram
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    converted  = db.Column(db.Boolean, default=False)

class SocialPost(db.Model):
    __tablename__ = 'social_posts'
    id         = db.Column(db.Integer, primary_key=True)
    platform   = db.Column(db.String(20))
    content    = db.Column(db.Text)
    posted_at  = db.Column(db.DateTime, default=datetime.utcnow)
    post_id    = db.Column(db.String(100))
    trade_id   = db.Column(db.Integer)
    post_type  = db.Column(db.String(30))  # signal/close/weekly_recap
