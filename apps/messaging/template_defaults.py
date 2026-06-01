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

    ('hotspot_expired',          'Hotspot — Session Expired',
     'Your WiFi session has ended. Visit the portal to buy a new plan and reconnect. Thank you!',
     []),

    ('hotspot_payment_failed',   'Hotspot — Payment Failed',
     'WiFi payment failed. Please try again from the portal. No amount was deducted.',
     []),

    ('pppoe_welcome',            'PPPoE — Welcome',
     'Welcome {name}! Your internet service is now active. Username: {username} / Password: {password}. '
     'Contact support if you need help.',
     ['{name}', '{username}', '{password}']),

    ('pppoe_new_subscription',   'PPPoE — New Subscription',
     'Hi {name}, your {plan_name} subscription (KES {amount}) is now active. Expires: {expiry_date}. Enjoy!',
     ['{name}', '{plan_name}', '{amount}', '{expiry_date}']),

    ('pppoe_payment',            'PPPoE — Payment Confirmation',
     'Hi {name}, payment of KES {amount} received. Ref: {reference}. Thank you!',
     ['{name}', '{amount}', '{reference}']),

    ('pppoe_renewal',            'PPPoE — Renewal Confirmation',
     'Hi {name}, your {plan_name} subscription has been renewed. New expiry: {expiry_date}. Stay connected!',
     ['{name}', '{plan_name}', '{expiry_date}']),

    ('pppoe_expiry_reminder',    'PPPoE — Expiry Reminder',       # WAS 'pppoe_expiry'
     'Hi {name}, your internet subscription ({plan_name}) expires in {days} day(s). Please renew to avoid interruption.',
     ['{name}', '{plan_name}', '{days}']),

    ('pppoe_suspended',          'PPPoE — Service Suspended',
     'Hi {name}, your internet service has been suspended. Please contact support or make a payment to restore.',
     ['{name}']),

    ('pppoe_resumed',            'PPPoE — Service Restored',
     'Great news {name}! Your internet service has been restored. You should be connected now.',
     ['{name}']),

    ('pppoe_plan_changed',       'PPPoE — Plan Changed',
     'Hi {name}, your internet plan has been updated from {old_plan} to {new_plan}. Enjoy!',
     ['{name}', '{old_plan}', '{new_plan}']),
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