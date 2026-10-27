from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations, models


def convert_catalog_to_kes(apps, schema_editor):
    Product = apps.get_model("shop", "Product")
    Order = apps.get_model("shop", "Order")

    latest_rate = (
        Order.objects.filter(currency="USD", fx_rate__gt=0)
        .order_by("-created_at")
        .values_list("fx_rate", flat=True)
        .first()
    )
    rate = Decimal(str(latest_rate or "129"))

    for product in Product.objects.filter(currency="USD").iterator():
        product.price = (product.price * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if product.compare_at_price is not None:
            product.compare_at_price = (product.compare_at_price * rate).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        product.currency = "KES"
        product.save(update_fields=["price", "compare_at_price", "currency"])


class Migration(migrations.Migration):
    dependencies = [("shop", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="product",
            name="currency",
            field=models.CharField(default="USD", max_length=3),
        ),
        migrations.RunPython(convert_catalog_to_kes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="product",
            name="currency",
            field=models.CharField(default="KES", max_length=3),
        ),
        migrations.AlterField(
            model_name="order",
            name="currency",
            field=models.CharField(default="KES", max_length=3),
        ),
    ]
