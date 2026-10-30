from django.conf import settings
from django.utils.text import slugify
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from utils.phone import is_valid_phone_number, normalize_phone_number

from .models import Category, Order, OrderItem, Product, ShopSettings


# ───────────── Public (read) ─────────────
class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["name", "slug"]


class ShopSettingsSerializer(serializers.ModelSerializer):
    default_shipping_fee = serializers.SerializerMethodField()
    free_shipping_threshold = serializers.FloatField(read_only=True)

    class Meta:
        model = ShopSettings
        fields = [
            "store_name", "announcement_text", "hero_eyebrow", "hero_title",
            "hero_description", "hero_image", "hero_primary_label", "hero_primary_href",
            "hero_secondary_label", "hero_secondary_href", "support_email", "support_phone",
            "whatsapp_number", "default_shipping_fee", "free_shipping_threshold",
            "allow_checkout", "checkout_notice", "updated_at",
        ]

    def get_default_shipping_fee(self, obj):
        configured = obj.default_shipping_fee
        return float(configured if configured != 0 else settings.SHOP_SHIPPING_FEE_KES)


class ProductSerializer(serializers.ModelSerializer):
    """camelCase to match the frontend Product type."""
    id = serializers.CharField(source="slug", read_only=True)
    category = serializers.CharField(source="category.name", read_only=True)
    categorySlug = serializers.CharField(source="category.slug", read_only=True)
    price = serializers.FloatField(read_only=True)
    currency = serializers.CharField(read_only=True)
    compareAtPrice = serializers.FloatField(source="compare_at_price", read_only=True)
    hoverImage = serializers.SerializerMethodField()
    longDescription = serializers.CharField(source="long_description", read_only=True)
    specsSheet = serializers.JSONField(source="specs_sheet", read_only=True)
    madeIn = serializers.CharField(source="made_in", read_only=True)
    inStock = serializers.SerializerMethodField()
    isFeatured = serializers.BooleanField(source="is_featured", read_only=True)

    class Meta:
        model = Product
        fields = ["id", "name", "category", "categorySlug", "price", "currency", "compareAtPrice", "image",
                  "hoverImage", "longDescription", "details", "materials", "care", "specsSheet",
                  "warranty", "madeIn", "sizes", "colors", "inStock", "isFeatured"]
        read_only_fields = fields

    def get_hoverImage(self, obj):
        return obj.hover_image or obj.image

    def get_inStock(self, obj):
        return (not obj.track_stock) or obj.stock_quantity > 0


class OrderItemPublicSerializer(serializers.ModelSerializer):
    unit_price = serializers.FloatField()
    line_total = serializers.FloatField()

    class Meta:
        model = OrderItem
        fields = ["product_slug", "name", "image", "unit_price", "quantity",
                  "selected_storage", "selected_color", "line_total"]


class OrderPublicSerializer(serializers.ModelSerializer):
    items = OrderItemPublicSerializer(many=True, read_only=True)
    subtotal = serializers.FloatField()
    shipping_fee = serializers.FloatField()
    total = serializers.FloatField()

    class Meta:
        model = Order
        fields = ["reference", "status", "payment_status", "payment_method", "currency", "subtotal",
                  "shipping_fee", "total", "amount_kes", "mpesa_receipt", "payment_failure_reason",
                  "city", "country", "created_at", "paid_at", "items"]


# ───────────── Checkout (write) ─────────────
class CheckoutItemSerializer(serializers.Serializer):
    product_id = serializers.SlugField(max_length=160)
    quantity = serializers.IntegerField(min_value=1, max_value=1000)
    selected_storage = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    selected_color = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")


class CheckoutSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    company = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=32)
    address = serializers.CharField(max_length=255)
    apartment = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    city = serializers.CharField(max_length=100)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    zip_code = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    country = serializers.CharField(max_length=100, required=False, allow_blank=True, default="Kenya")
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    payment_method = serializers.ChoiceField(choices=["mpesa", "card"])
    mpesa_phone = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    items = CheckoutItemSerializer(many=True, allow_empty=False)

    def validate_email(self, value):
        return value.strip().lower()

    def validate_items(self, value):
        if len(value) > 50:
            raise serializers.ValidationError("Too many line items (max 50).")
        return value

    def validate(self, attrs):
        if attrs["payment_method"] == "card":
            raise serializers.ValidationError({"payment_method": "Card payments are not available yet. Please use M-Pesa."})
        msisdn = normalize_phone_number(attrs.get("mpesa_phone") or attrs["phone"])
        if not is_valid_phone_number(msisdn):
            raise serializers.ValidationError({"mpesa_phone": "Enter a valid Kenyan M-Pesa number, e.g. 0712345678."})
        attrs["mpesa_msisdn"] = msisdn
        return attrs


# ───────────── Admin ─────────────
def _clean_options(value, key):
    if not isinstance(value, list) or len(value) > 50:
        raise serializers.ValidationError("Provide a list of up to 50 options.")
    out = []
    for opt in value:
        if not isinstance(opt, dict) or not str(opt.get(key, "")).strip():
            raise serializers.ValidationError(f"Each option needs a '{key}'.")
        entry = {k: v for k, v in opt.items() if isinstance(v, (str, int, float, bool))}
        entry[key] = str(opt[key]).strip()[:120]
        entry["available"] = bool(opt.get("available", True))
        out.append(entry)
    return out


def _clean_str_list(value):
    if not isinstance(value, list) or len(value) > 100 or not all(isinstance(v, str) for v in value):
        raise serializers.ValidationError("Provide a list of text lines (max 100).")
    return [v.strip() for v in value if v.strip()]


def _unique_slug(name):
    base = slugify(name)[:150] or "product"
    slug, n = base, 2
    while Product.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


class AdminCategorySerializer(serializers.ModelSerializer):
    slug = serializers.SlugField(max_length=140, required=False,
                                 validators=[UniqueValidator(queryset=Category.objects.all())])

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "sort_order", "is_active"]

    def validate(self, attrs):
        if self.instance is None and not attrs.get("slug"):
            attrs["slug"] = slugify(attrs["name"])[:140] or "category"
        return attrs


class AdminShopSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = ShopSettings
        exclude = ["id"]
        read_only_fields = ["updated_at"]

    def validate(self, attrs):
        for field in ("hero_primary_href", "hero_secondary_href"):
            value = str(attrs.get(field, getattr(self.instance, field, "")) or "").strip()
            if value and not (value.startswith("/") or value.startswith("https://") or value.startswith("http://")):
                raise serializers.ValidationError({field: "Use an internal path or a full http(s) URL."})
        return attrs


class AdminProductSerializer(serializers.ModelSerializer):
    slug = serializers.SlugField(max_length=160, required=False,
                                 validators=[UniqueValidator(queryset=Product.objects.all())])
    category_name = serializers.CharField(source="category.name", read_only=True)

    class Meta:
        model = Product
        fields = "__all__"
        read_only_fields = ["created_at", "updated_at"]

    def validate_sizes(self, v):
        return _clean_options(v, "size")

    def validate_colors(self, v):
        return _clean_options(v, "name")

    def validate_details(self, v):
        return _clean_str_list(v)

    def validate_materials(self, v):
        return _clean_str_list(v)

    def validate_care(self, v):
        return _clean_str_list(v)

    def validate_specs_sheet(self, v):
        if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(x, (str, int, float)) for k, x in v.items()):
            raise serializers.ValidationError("Provide an object of text values.")
        return {k: str(x) for k, x in v.items()}

    def validate(self, attrs):
        if self.instance is None and not attrs.get("slug"):
            attrs["slug"] = _unique_slug(attrs["name"])
        return attrs


class AdminOrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ["id", "product", "product_slug", "name", "unit_price", "quantity",
                  "selected_storage", "selected_color", "line_total"]


class AdminOrderListSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        exclude = ["tracking_token"]


class AdminOrderDetailSerializer(AdminOrderListSerializer):
    items = AdminOrderItemSerializer(many=True, read_only=True)
