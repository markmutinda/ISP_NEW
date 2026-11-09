"""Seed default SMS templates for a tenant."""

DEFAULTS = [
    ('hotspot_welcome',
     'Hotspot — Session Active',
     'WiFi Active!\n{credentials}\nPlan: {plan_name} ({duration}). Expires: {expiry_time}. Speed: {speed}. Enjoy!',
     ['{credentials}', '{username}', '{password}', '{device_count}',
      '{access_code}', '{plan_name}', '{duration}', '{expiry_time}', '{speed}']),

    # ── PPPOE / STATIC TEMPLATES ────────────────────────────────────────
    ('pppoe_welcome',
     'PPPoE — Welcome',
     'Welcome {customer_name}! Your account number is {account_number}. '
     'Contact us on {phone_number} if you need help.',
     ['{customer_name}', '{account_number}', '{phone_number}']),

    ('pppoe_payment',
     'PPPoE — Payment / Renewal Confirmation',
     'Hi {customer_name}, payment of KES {amount} received for {plan_name}. '
     'Your subscription is now valid until {expiry_full}. Thank you!',
     ['{customer_name}', '{amount}', '{plan_name}', '{expiry_full}',
      '{expiry_display}', '{reference}', '{customer_account}']),

    ('pppoe_expiry_reminder',
     'PPPoE — Expiry Reminder',
     'Hi {customer_name}, your internet subscription ({plan_name}) expires in {days} day(s). '
     'Please renew to avoid interruption.',
     ['{customer_name}', '{plan_name}', '{days}', '{expiry_date}', '{expiry_time}',
      '{expiry_display}', '{expiry_full}', '{amount_due}', '{customer_account}']),

    ('pppoe_expiry_notification',
     'PPPoE — Subscription Expired',
     'Hi {customer_name}, your internet subscription ({plan_name}) has expired. '
     'Please renew to restore your connection.',
     ['{customer_name}', '{plan_name}', '{customer_account}', '{amount_due}']),
]

EVENT_SLOTS = [
    ('hotspot_welcome', 'Hotspot — Welcome / Session Active',
     'Sent when a hotspot session becomes active.'),
    ('pppoe_welcome', 'PPPoE — New Customer Welcome',
     'Sent when a new PPPoE / Static service is activated.'),
    ('pppoe_payment', 'PPPoE — Payment / Renewal Confirmation',
     'Sent when a payment or renewal is received.'),
    ('pppoe_expiry_reminder', 'PPPoE — Expiry Reminder',
     'Sent before a subscription expires.'),
    ('pppoe_expiry_notification', 'PPPoE — Subscription Expired',
     'Sent once when a subscription has expired.'),
]

# event_type -> (name, default content, variables)
DEFAULT_BY_EVENT = {ev: (name, content, variables) for ev, name, content, variables in DEFAULTS}


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