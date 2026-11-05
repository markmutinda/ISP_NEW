from django.core.management.base import BaseCommand

from apps.shop.models import Category, Product


class Command(BaseCommand):
    help = "Create sample shop data (idempotent)."

    def handle(self, *args, **options):
        category_specs = [
            ("routers-gateways", "Routers & Gateways", "Core, edge and branch routing for PPPoE, hotspot and business networks.", "/shop-assets/products/mikrotik_router.jpg", ["MikroTik", "Ubiquiti", "TP-Link", "Huawei"]),
            ("wireless-access-points", "Wireless & Access Points", "Indoor Wi-Fi, outdoor coverage and managed access points.", "/shop-assets/products/ceiling-access-point.svg", ["Ubiquiti", "TP-Link", "MikroTik", "Grandstream"]),
            ("switches-poe", "Switches & PoE", "Managed switching, PoE distribution and high-speed uplinks.", "/shop-assets/products/poe_switch.jpg", ["MikroTik", "TP-Link", "Ubiquiti", "D-Link"]),
            ("fiber-optics-olt", "Fibre & Optics", "GPON, OLT, ONT, optics and field equipment for fibre rollouts.", "/academy/fiber-olt.webp", ["VSOL", "V-SOL", "Huawei", "Signal Fire"]),
            ("cctv-surveillance", "CCTV & Surveillance", "Cameras, recorders and complete surveillance kits for customer sites.", "/shop-assets/products/poe_switch.jpg", ["Hikvision", "Dahua", "IMOU", "Ezviz"]),
            ("power-protection", "Power & UPS", "Backup power, surge protection and reliable power for network sites.", "/shop-assets/products/network-ups.svg", ["APC", "MikroTik", "Dahua"]),
            ("structured-cabling", "Cables & Accessories", "Copper, fibre, patching, racks, mounting and installation consumables.", "/shop-assets/products/cat6a_cable.jpg", ["Netily", "Starlink", "Scoop"]),
            ("tools-test-equipment", "Tools & Test Equipment", "Splicing, testing and installation tools for field teams.", "/shop-assets/products/fusion_splicer.jpg", ["Signal Fire", "Grandlink"]),
            ("security-access-control", "Security & Access Control", "Access control, attendance and site-security equipment.", "", ["ZKTeco", "Hikvision"]),
            ("computers-noc", "Computers & NOC", "Workstations, laptops and displays for support desks and NOCs.", "", ["HP", "Dell", "Apple", "Lenovo"]),
        ]
        categories = {}
        for order, (slug, name, description, image, brands) in enumerate(category_specs, start=1):
            category, _ = Category.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "description": description,
                    "image": image,
                    "featured_brands": brands,
                    "sort_order": order * 10,
                    "is_active": True,
                },
            )
            categories[slug] = category
        routers = categories["routers-gateways"]
        cabling = categories["structured-cabling"]
        Product.objects.get_or_create(
            slug="mikrotik-ccr2004-16g-2s",
            defaults=dict(name="MikroTik Cloud Core Router CCR2004-16G-2S+PC", category=routers, price=63855,
                          brand="MikroTik", image="/shop-assets/products/mikrotik_router.jpg", stock_quantity=25,
                          sizes=[{"size": "Standard 1U Rackmount", "available": True}],
                          colors=[{"name": "Matte Black", "available": True}], warranty="1-Year Warranty"))
        Product.objects.get_or_create(
            slug="netily-cat6a-outdoor-305m",
            defaults=dict(name="Netily UltraGrade Cat6A Outdoor 305m Drum", category=cabling, price=27735,
                          brand="Netily", image="/shop-assets/products/cat6a_cable.jpg", stock_quantity=60,
                          sizes=[{"size": "305m Wooden Spool (23AWG)", "available": True}],
                          colors=[{"name": "Black UV-Resistant PE", "available": True}]))
        self.stdout.write(self.style.SUCCESS("Shop sample data ready."))
