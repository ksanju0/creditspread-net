"""
Manual weekly calendar for the Pre-Market newsletter.

Edit this file every Sunday for the upcoming week. Two dicts:

  CATALYSTS  — list of bullets shown in section "On the Calendar"
               keyed by 'YYYY-MM-DD'
  PLAYBOOKS  — one-paragraph analytical narrative for "Today's Playbook"
               keyed by 'YYYY-MM-DD'

If a date is missing, sensible defaults are returned automatically.
"""
from datetime import date

CATALYSTS = {
    # Example — uncomment + edit each Sunday:
    # '2025-06-13': [
    #     '8:30 AM ET — CPI release (cons +0.2% MoM)',
    #     '11:00 AM — Powell speaks at Bank of America Conf.',
    #     'After close — ORCL, ADBE earnings',
    # ],
}

PLAYBOOKS = {
    'default': (
        "Our system scans the SPX & QQQ chain at 9:45 AM ET. "
        "When VIX, delta, and time-of-day filters align, a signal fires. "
        "Members receive the exact strikes, credit, target, and stop — "
        "sized to their account — the moment the trade is placed in our accounts."
    ),
    # '2025-06-13': (
    #     "CPI is the entire session today. If the print lands in line, "
    #     "expect post-data IV crush — ideal for our 9:45 AM signal. A hot "
    #     "print (+0.3% or higher) and we sit out."
    # ),
}

def get_catalysts(d=None):
    d = d or date.today()
    return CATALYSTS.get(d.strftime('%Y-%m-%d'), [
        'No major scheduled catalysts today',
        'Standard 9:45 AM entry window in play',
    ])

def get_playbook(d=None):
    d = d or date.today()
    return PLAYBOOKS.get(d.strftime('%Y-%m-%d'), PLAYBOOKS['default'])
