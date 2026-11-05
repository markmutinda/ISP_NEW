from django.db import migrations, models


def backfill_product_brands(apps, schema_editor):
    Product = apps.get_model("shop", "Product")
    for product in Product.objects.filter(brand="").iterator():
        specs = product.specs_sheet if isinstance(product.specs_sheet, dict) else {}
        brand = str(specs.get("Brand") or specs.get("brand") or "").strip()[:120]
        if brand:
            Product.objects.filter(pk=product.pk).update(brand=brand)


class Migration(migrations.Migration):
    dependencies = [("shop", "0003_shopsettings")]

    operations = [
        migrations.AddField(
            model_name="category",
            name="description",
            field=models.CharField(blank=True, default="", max_length=320),
        ),
        migrations.AddField(
            model_name="category",
            name="image",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="category",
            name="featured_brands",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="product",
            name="brand",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="product",
            name="is_trending",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_product_brands, migrations.RunPython.noop),
    ]
