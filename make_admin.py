"""
Grant a full member (active) account. Combined with adding the email to ADMIN_EMAIL,
this gives full admin + member access.

    python make_admin.py <email> [password]

If no password is given, a strong one is generated and printed once.
"""
import sys, os, secrets
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash
from app import app, db
from models import User

email = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
if not email:
    print("Usage: python make_admin.py <email> [password]"); sys.exit(1)
password = sys.argv[2] if len(sys.argv) > 2 else secrets.token_urlsafe(9)

with app.app_context():
    u = User.query.filter_by(email=email).first()
    if not u:
        u = User(email=email, name="Admin")
        db.session.add(u)
    u.password_hash = generate_password_hash(password)
    u.plan = "member"
    u.sub_status = "active"
    u.sub_end_date = datetime.utcnow() + timedelta(days=3650)
    db.session.commit()
    print("OK  email:", email, " password:", password, " plan: member (active)")
