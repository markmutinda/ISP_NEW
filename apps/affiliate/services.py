from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import AffiliateAccount, AffiliateClick, AffiliateReferral


def affiliate_lead_data(lead):
    """Return trusted affiliate attribution for a lead, if one exists."""
    if not lead:
        return None
    prefetched = getattr(lead, "_prefetched_objects_cache", {}).get("affiliatereferral_set")
    referral = (
        (prefetched[0] if prefetched else None)
        if prefetched is not None
        else lead.affiliatereferral_set.select_related("affiliate__user").first()
    )
    if not referral:
        return None
    affiliate = referral.affiliate
    affiliate_name = affiliate.user.get_full_name().strip() or affiliate.user.email
    return {
        "referral_id": referral.id,
        "referral_status": referral.status,
        "affiliate_id": affiliate.id,
        "affiliate_name": affiliate_name,
        "affiliate_email": affiliate.user.email,
        "referral_code": affiliate.referral_code,
    }


def record_affiliate_signup(*, referral_code, email, company_name="", company=None, lead=None, attribution_token=None):
    """
    Record a tracked signup without assigning money.

    A valid click token is treated as verified attribution. If the browser loses
    the token but still submits a valid affiliate code, keep the lead attached to
    that affiliate as pending review so superadmins can audit it instead of
    losing the relationship entirely.
    """
    code = (referral_code or "").strip().upper()
    email = (email or "").strip().lower()
    if not code or not email:
        return None

    affiliate = AffiliateAccount.objects.filter(referral_code=code, status="active").first()
    if not affiliate:
        return None
    if email == (affiliate.user.email or "").strip().lower():
        return None

    window_days = int(getattr(settings, "AFFILIATE_ATTRIBUTION_WINDOW_DAYS", 30))
    try:
        with transaction.atomic():
            click = None
            if attribution_token:
                try:
                    click = (
                        AffiliateClick.objects.select_for_update()
                        .filter(
                            attribution_token=attribution_token,
                            affiliate=affiliate,
                            created_at__gte=timezone.now() - timedelta(days=window_days),
                        )
                        .first()
                    )
                except (TypeError, ValueError, ValidationError):
                    click = None

            # One tracked click represents one prospective ISP conversion. This
            # prevents replaying a captured browser cookie for multiple accounts.
            if click and click.signups.exclude(signup_email__iexact=email).exists():
                return None

            existing = AffiliateReferral.objects.filter(signup_email__iexact=email).first()
            if existing and existing.affiliate_id != affiliate.id:
                return None

            if existing:
                referral, created = existing, False
            else:
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
            if not created:
                changed = []
                for field, value in (("company", company), ("lead", lead), ("click", click), ("company_name", company_name)):
                    if value and not getattr(referral, field):
                        setattr(referral, field, value)
                        changed.append(field)
                if changed:
                    referral.save(update_fields=[*changed, "updated_at"])
            return referral
    except (TypeError, ValueError, ValidationError):
        return None
    except IntegrityError:
        return AffiliateReferral.objects.filter(
            signup_email__iexact=email,
            affiliate=affiliate,
        ).first()
