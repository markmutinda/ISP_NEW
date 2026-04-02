from datetime import timedelta
import uuid

from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models.hotspot_models import HotspotPlan
from apps.billing.models.voucher_models import Voucher, VoucherBatch
from apps.core.permissions import IsCompanyStaff


class HotspotVoucherGenerateView(APIView):
    """
    Generate hotspot vouchers tied to a selected hotspot plan.

    POST body:
    {
      "plan_id": "<uuid>",
      "quantity": 10,
      "valid_days": 30,              # optional if never_expires=false
      "never_expires": true,         # optional, default true
      "prefix": "VCH",               # optional
      "digits": 5                    # optional, default 5
    }
    """
    permission_classes = [permissions.IsAuthenticated, IsCompanyStaff]

    def post(self, request):
        plan_id = request.data.get('plan_id')
        quantity = int(request.data.get('quantity') or 0)
        prefix = (request.data.get('prefix') or 'VCH').strip()[:10] or 'VCH'
        digits = int(request.data.get('digits') or 5)

        # Validate plan_id UUID
        try:
            plan_uuid = uuid.UUID(str(plan_id))
        except (ValueError, TypeError, ValidationError):
            return Response(
                {'error': 'plan_id must be a valid hotspot plan UUID'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if quantity <= 0:
            return Response(
                {'error': 'quantity must be greater than 0'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if digits <= 0:
            return Response(
                {'error': 'digits must be greater than 0'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Fetch plan
        try:
            plan = HotspotPlan.objects.get(id=plan_uuid, is_active=True)
        except HotspotPlan.DoesNotExist:
            return Response(
                {'error': 'Hotspot plan not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        now = timezone.now()

        # Expiry handling
        never_expires = str(request.data.get('never_expires', 'true')).lower() == 'true'
        valid_days_str = request.data.get('valid_days')

        if never_expires:
            # effectively no short-term expiry
            voucher_valid_to = now + timedelta(days=3650)  # ~10 years
            valid_days = None
        else:
            if valid_days_str is None:
                valid_days = 30
            else:
                try:
                    valid_days = int(valid_days_str)
                except (ValueError, TypeError):
                    return Response(
                        {'error': 'valid_days must be a valid integer'},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            if valid_days <= 0:
                return Response(
                    {'error': 'valid_days must be greater than 0'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            voucher_valid_to = now + timedelta(days=valid_days)

        # Create batch with plan restriction
        # NOTE: plan_restriction/hotspot_plan belong to VoucherBatch, NOT Voucher
        batch = VoucherBatch.objects.create(
            name=f"{plan.name} Voucher Batch",
            description=(
                f"Auto-generated vouchers for hotspot plan: {plan.name}"
                + (" (never expires)" if never_expires else f" (valid for {valid_days} days)")
            ),
            voucher_type='PREPAID',
            face_value=plan.price,
            sale_price=plan.price,
            valid_from=now,
            valid_to=voucher_valid_to,
            is_reusable=False,
            max_uses=1,
            quantity=quantity,
            status='ACTIVE',
            is_active=True,
            prefix=prefix,
            length=min(50, len(prefix) + digits),  # keeps codes e.g. VCH12345
            charset='0123456789',
            plan_restriction=True,
            hotspot_plan=plan,
            created_by=request.user,
        )

        # Use model helper to generate vouchers safely/uniquely
        vouchers = batch.generate_vouchers(quantity)

        return Response({
            'message': f'Generated {len(vouchers)} vouchers for plan {plan.name}',
            'never_expires': never_expires,
            'valid_days': valid_days,
            'batch': {
                'id': batch.id,
                'batch_number': batch.batch_number,
                'plan_id': str(plan.id),
                'plan_name': plan.name,
                'price': str(plan.price),
                'valid_from': batch.valid_from,
                'valid_to': batch.valid_to,
            },
            'vouchers': [
                {
                    'id': v.id,
                    'code': v.code,
                    'pin': v.pin,  # frontend may ignore if you choose no-pin UX
                    'status': v.status,
                    'expires_at': v.valid_to,
                    'plan_name': plan.name,
                    'plan_id': str(plan.id),
                }
                for v in vouchers
            ]
        }, status=status.HTTP_201_CREATED)


class HotspotVoucherListView(APIView):
    """
    List hotspot vouchers for admin UI with used/unused filtering.

    GET params:
      - status: used | unused | all (default all)
      - plan_id: optional hotspot plan UUID
    """
    permission_classes = [permissions.IsAuthenticated, IsCompanyStaff]

    def get(self, request):
        status_filter = (request.query_params.get('status') or 'all').lower()
        plan_id = request.query_params.get('plan_id')

        # Validate UUID if plan_id supplied
        if plan_id:
            try:
                uuid.UUID(str(plan_id))
            except (ValueError, TypeError, ValidationError):
                return Response(
                    {'error': 'plan_id must be a valid UUID'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        qs = Voucher.objects.select_related('batch', 'batch__hotspot_plan').filter(
            batch__hotspot_plan__isnull=False
        ).order_by('-created_at')

        if plan_id:
            qs = qs.filter(batch__hotspot_plan_id=plan_id)

        if status_filter == 'used':
            qs = qs.filter(Q(status='USED') | Q(use_count__gte=1))
        elif status_filter == 'unused':
            qs = qs.filter(status='ACTIVE', use_count=0)

        summary = Voucher.objects.filter(batch__hotspot_plan__isnull=False).aggregate(
            total=Count('id'),
            used=Count('id', filter=Q(status='USED') | Q(use_count__gte=1)),
            unused=Count('id', filter=Q(status='ACTIVE', use_count=0)),
        )

        now = timezone.now()
        data = [
            {
                'id': v.id,
                'code': v.code,
                'pin': v.pin,
                'status': v.status,
                'use_count': v.use_count,
                'is_valid': v.is_valid(),
                'is_expired': bool(v.valid_to and v.valid_to < now),
                'expires_at': v.valid_to,
                'plan_id': str(v.batch.hotspot_plan_id) if v.batch.hotspot_plan_id else None,
                'plan_name': v.batch.hotspot_plan.name if v.batch.hotspot_plan_id else None,
                'batch_number': v.batch.batch_number,
            }
            for v in qs
        ]

        return Response({
            'summary': summary,
            'count': len(data),
            'results': data,
        })