from django.db import migrations, models


PREVIOUS_TEMPLATE = (
    "Hi {admin_name}, your Netily subscription for {company_name} ({plan_name}) "
    "is due in {days_left} day(s) on {expiry_date}. "
    "Please open Admin > Subscription and renew to keep your account active."
)

# Fits one 160-char SMS with the renew link for typical tenant names.
NEW_TEMPLATE = (
    "Hi {admin_name}, your Netily subscription for {company_name} "
    "is due on {expiry_date}. Renew now: {renew_link}"
)


def use_short_template(apps, schema_editor):
    SubscriptionReminderTemplate = apps.get_model('subscriptions', 'SubscriptionReminderTemplate')
    template = SubscriptionReminderTemplate.objects.filter(pk=1).first()
    # Only replace the untouched default; custom superadmin copy is kept.
    if template and template.content.strip() == PREVIOUS_TEMPLATE:
        template.content = NEW_TEMPLATE
        template.save(update_fields=['content', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0020_configure_existing_selfie_test_plan'),
    ]

    operations = [
        migrations.AlterField(
            model_name='subscriptionremindertemplate',
            name='content',
            field=models.TextField(default=NEW_TEMPLATE),
        ),
        migrations.RunPython(use_short_template, migrations.RunPython.noop),
    ]
