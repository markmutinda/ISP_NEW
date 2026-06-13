"""Seed default SMS templates for a tenant."""

DEFAULTS = [
    ('hotspot_welcome',          'Hotspot — Session Active',
     'WiFi Active! Code: {access_code}. Plan: {plan_name} ({duration}). Valid till {expiry_time}. Speed: {speed}. Enjoy!',
     ['{access_code}', '{plan_name}', '{duration}', '{expiry_time}', '{speed}']),

    ('hotspot_new_subscription', 'Hotspot — Payment Initiated',
     'Hi! Your WiFi payment of KES {amount} for {plan_name} is being processed. '
     'You\'ll receive your access code shortly. Ref: {session_id}',
     ['{amount}', '{plan_name}', '{session_id}']),

    ('hotspot_expiry',           'Hotspot — Expiry Warning',
     'Your WiFi session ({access_code}) expires in {minutes} minute(s). Buy another plan to stay connected.',
     ['{access_code}', '{minutes}']),

    ('hotspot_session_expired',  'Hotspot — Session Expired',
     'Your WiFi session has ended. Visit the portal to buy a new plan and reconnect. Thank you!',
     []),

    ('hotspot_payment_failed',   'Hotspot — Payment Failed',
     'WiFi payment failed. Please try again from the portal. No amount was deducted.',
     []),

    # ── PPPOE / STATIC TEMPLATES (REDUCED SET) ─────────────────────────────
    ('pppoe_welcome',            'PPPoE — Welcome',
     'Welcome {customer_name}! Your internet service is now active. Username: {username} / Password: {password}. '
     'Contact support if you need help.',
     ['{customer_name}', '{username}', '{password}']),

    # MERGED: payment confirmation + renewal confirmation into one template
    ('pppoe_payment',            'PPPoE — Payment / Renewal Confirmation',
     'Hi {customer_name}, payment of KES {amount} received for {plan_name}. '
     'Your subscription is now valid until {expiry_full}. Thank you!',
     ['{customer_name}', '{amount}', '{plan_name}', '{expiry_full}',
      '{expiry_display}', '{reference}', '{customer_account}']),

    ('pppoe_expiry_reminder',    'PPPoE — Expiry Reminder',
     'Hi {customer_name}, your internet subscription ({plan_name}) expires in {days} day(s). '
     'Please renew to avoid interruption.',
     ['{customer_name}', '{plan_name}', '{days}', '{expiry_date}', '{expiry_time}',
      '{expiry_display}', '{expiry_full}', '{amount_due}', '{customer_account}']),

    # NEW: fires once when subscription actually lapses
    ('pppoe_expiry_notification', 'PPPoE — Subscription Expired',
     'Hi {customer_name}, your internet subscription ({plan_name}) has expired. '
     'Please renew to restore your connection.',
     ['{customer_name}', '{plan_name}', '{customer_account}', '{amount_due}']),
]


def seed_default_templates():
    """Idempotent: create system templates if they don't exist yet."""
    from apps.messaging.models import SMSTemplate
    for event_type, name, content, variables in DEFAULTS:
        SMSTemplate.objects.get_or_create(
            event_type=event_type,
            is_system=True,
            defaults={
                'name': name,
                'content': content,
                'variables': variables,
                'is_active': True,
            }
        )