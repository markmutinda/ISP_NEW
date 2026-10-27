from django.core.management.base import BaseCommand

from apps.shop.models import Category, Product


class Command(BaseCommand):
    help = "Create sample shop data (idempotent)."

    def handle(self, *args, **options):
        routers, _ = Category.objects.get_or_create(slug="routers-gateways", defaults={"name": "Routers & Gateways"})
        cabling, _ = Category.objects.get_or_create(slug="cabling-infrastructure", defaults={"name": "Cabling & Infrastructure"})
        Product.objects.get_or_create(
            slug="mikrotik-ccr2004-16g-2s",
            defaults=dict(name="MikroTik Cloud Core Router CCR2004-16G-2S+PC", category=routers, price=63855,
                          image="/shop-assets/products/mikrotik_router.jpg", stock_quantity=25,
                          sizes=[{"size": "Standard 1U Rackmount", "available": True}],
                          colors=[{"name": "Matte Black", "available": True}], warranty="1-Year Warranty"))
        Product.objects.get_or_create(
            slug="netily-cat6a-outdoor-305m",
            defaults=dict(name="Netily UltraGrade Cat6A Outdoor 305m Drum", category=cabling, price=27735,
                          image="/shop-assets/products/cat6a_cable.jpg", stock_quantity=60,
                          sizes=[{"size": "305m Wooden Spool (23AWG)", "available": True}],
                          colors=[{"name": "Black UV-Resistant PE", "available": True}]))
        self.stdout.write(self.style.SUCCESS("Shop sample data ready."))
