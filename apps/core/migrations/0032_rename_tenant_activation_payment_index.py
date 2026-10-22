from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0031_globalsystemsettings_payment_methods_pin"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="tenantactivationpayment",
            old_name="core_tenant_status_03d25c_idx",
            new_name="core_tenant_status_3e3ec2_idx",
        ),
    ]
