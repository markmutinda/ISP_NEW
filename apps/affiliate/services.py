from django.db import IntegrityError

from .models import AffiliateAccount, AffiliateClick, AffiliateReferral


def record_affiliate_signup(*, referral_code, email, company_name="", company=None, lead=None, attribution_token=None):
    """Record attribution only; commission and payout values remain untouched."""
    code = (referral_code or "").strip().upper()
    email = (email or "").strip().lower()
    if not code or not email:
        return None

    affiliate = AffiliateAccount.objects.filter(referral_code=code, status="active").first()
    if not affiliate:
        return None

    click = None
    if attribution_token:
        click = AffiliateClick.objects.filter(
            attribution_token=attribution_token,
            affiliate=affiliate,
        ).first()
    try:
        referral, created = AffiliateReferral.objects.get_or_create(
            signup_email=email,
            defaults={
                "affiliate": affiliate,
                "click": click,
                "company": company,
                "lead": lead,
                "company_name": company_name,
                "currency": affiliate.currency,
            },
        )
        if not created and referral.affiliate_id == affiliate.id:
            changed = []
            for field, value in (("company", company), ("lead", lead), ("click", click), ("company_name", company_name)):
                if value and not getattr(referral, field):
                    setattr(referral, field, value)
                    changed.append(field)
            if changed:
                referral.save(update_fields=[*changed, "updated_at"])
    except IntegrityError:
        referral = AffiliateReferral.objects.filter(signup_email=email).first()
    return referral
