"""
Subscriptions App

This app handles Netily platform subscriptions - where ISP companies
pay Netily for access to the platform (Starter, Professional, Enterprise plans).

This is DIFFERENT from customer billing which happens in the tenant schema.
Subscription data lives in the PUBLIC schema as it's company-level.
"""
default_app_config = 'apps.subscriptions.apps.SubscriptionsConfig'
