from django.db import migrations, models


def exempt_demo_tenant(apps, schema_editor):
    Tenant = apps.get_model("core", "Tenant")
    Tenant.objects.filter(schema_name="demo").update(
        is_billing_exempt=True,
        status="active",
        subscription_expiry=None,
        next_billing_date=None,
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0035_remove_user_uniq_customer_phone_and_more")]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="is_billing_exempt",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Keep this tenant active without subscription expiry, invoices, or renewal reminders.",
            ),
        ),
        migrations.RunPython(exempt_demo_tenant, migrations.RunPython.noop),
    ]
