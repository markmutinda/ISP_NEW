from django.db import migrations, models


OLD_TEMPLATE = (
    "Hi {admin_name}, your Netily subscription for {company_name} ({plan_name}) "
    "is due in {days_left} day(s) on {expiry_date}. Amount due: KES {amount_due}. "
    "Please pay to avoid service interruption."
)

NEW_TEMPLATE = (
    "Hi {admin_name}, your Netily subscription for {company_name} ({plan_name}) "
    "invoice {invoice_number} is due in {days_left} day(s) on {expiry_date}. Amount due: KES {amount_due}. "
    "Please pay to avoid service interruption."
)


def upgrade_default_template(apps, schema_editor):
    SubscriptionReminderTemplate = apps.get_model('subscriptions', 'SubscriptionReminderTemplate')
    template = SubscriptionReminderTemplate.objects.filter(pk=1).first()
    if template and template.content == OLD_TEMPLATE:
        template.content = NEW_TEMPLATE
        template.save(update_fields=['content', 'updated_at'])


class Migration(migrations.Migration):

    dependencies = [
        ('subscriptions', '0012_subscription_invoice_reminder_delivery'),
    ]

    operations = [
        migrations.AlterField(
            model_name='subscriptionremindertemplate',
            name='content',
            field=models.TextField(default=NEW_TEMPLATE),
        ),
        migrations.RunPython(upgrade_default_template, migrations.RunPython.noop),
    ]
