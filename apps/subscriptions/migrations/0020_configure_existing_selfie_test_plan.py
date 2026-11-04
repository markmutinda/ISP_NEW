from decimal import Decimal

from django.db import migrations


def configure_existing_test_plan(apps, schema_editor):
    NetilyPlan = apps.get_model("subscriptions", "NetilyPlan")
    test_plan = NetilyPlan.objects.filter(name__iexact="test", code__iexact="testing").first()
    if not test_plan:
        return
    NetilyPlan.objects.exclude(pk=test_plan.pk).filter(is_selfie_activation_plan=True).update(
        is_selfie_activation_plan=False
    )
    test_plan.activation_fee = Decimal("1.00")
    test_plan.is_selfie_activation_plan = True
    test_plan.save(update_fields=["activation_fee", "is_selfie_activation_plan", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("subscriptions", "0019_netilyplan_add_test_code")]
    operations = [migrations.RunPython(configure_existing_test_plan, migrations.RunPython.noop)]
