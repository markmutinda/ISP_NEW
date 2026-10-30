from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):
    dependencies = [
        ('shop', '0002_kes_catalog_currency'),
    ]

    operations = [
        migrations.CreateModel(
            name='ShopSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('store_name', models.CharField(default='Netily Shop', max_length=120)),
                ('announcement_text', models.CharField(blank=True, default='', max_length=240)),
                ('hero_eyebrow', models.CharField(default='Netily Network Equipment', max_length=120)),
                ('hero_title', models.CharField(default='Build a network that is ready to grow', max_length=180)),
                ('hero_description', models.CharField(default='Carrier-grade routing, fibre, wireless, power, and installation equipment for modern ISPs.', max_length=500)),
                ('hero_image', models.CharField(default='/shop-assets/netily_hero_bg.jpg', max_length=500)),
                ('hero_primary_label', models.CharField(default='Browse equipment', max_length=80)),
                ('hero_primary_href', models.CharField(default='/shop/catalog', max_length=240)),
                ('hero_secondary_label', models.CharField(default='Ask before you buy', max_length=80)),
                ('hero_secondary_href', models.CharField(default='https://wa.me/254100034307', max_length=240)),
                ('support_email', models.EmailField(blank=True, default='', max_length=254)),
                ('support_phone', models.CharField(blank=True, default='', max_length=32)),
                ('whatsapp_number', models.CharField(blank=True, default='254100034307', max_length=32)),
                ('default_shipping_fee', models.DecimalField(decimal_places=2, default=0, max_digits=12, validators=[django.core.validators.MinValueValidator(0)])),
                ('free_shipping_threshold', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, validators=[django.core.validators.MinValueValidator(0)])),
                ('allow_checkout', models.BooleanField(default=True)),
                ('checkout_notice', models.CharField(blank=True, default='', max_length=300)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'verbose_name_plural': 'Shop settings'},
        ),
    ]
