from datetime import timedelta
import uuid

from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
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

        # ADDED: Max 50 vouchers per generation
        if quantity > 50:
            return Response(
                {'error': 'Maximum 50 vouchers can be generated per request'},
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


# ADD THIS NEW VIEW CLASS
class HotspotVoucherDetailView(APIView):
    """
    PATCH /api/v1/hotspot/admin/vouchers/{id}/  — edit expiry date
    DELETE /api/v1/hotspot/admin/vouchers/{id}/ — delete voucher
    """
    permission_classes = [permissions.IsAuthenticated, IsCompanyStaff]

    def get_object(self, pk):
        try:
            return Voucher.objects.select_related('batch').get(
                pk=pk, batch__hotspot_plan__isnull=False
            )
        except Voucher.DoesNotExist:
            return None

    def patch(self, request, pk):
        voucher = self.get_object(pk)
        if not voucher:
            return Response({'error': 'Voucher not found'}, status=status.HTTP_404_NOT_FOUND)

        expires_at = request.data.get('expires_at')
        if not expires_at:
            return Response({'error': 'expires_at is required'}, status=status.HTTP_400_BAD_REQUEST)

        parsed = parse_datetime(expires_at)
        if not parsed:
            return Response({'error': 'Invalid date format'}, status=status.HTTP_400_BAD_REQUEST)

        voucher.valid_to = parsed
        # Re-evaluate status based on new date
        if parsed > timezone.now() and voucher.status == 'EXPIRED':
            voucher.status = 'ACTIVE'
        voucher.save(update_fields=['valid_to', 'status', 'updated_at'])

        return Response({
            'id': voucher.id,
            'code': voucher.code,
            'expires_at': voucher.valid_to,
            'status': voucher.status,
        })

    def delete(self, request, pk):
        voucher = self.get_object(pk)
        if not voucher:
            return Response({'error': 'Voucher not found'}, status=status.HTTP_404_NOT_FOUND)

        if voucher.status == 'USED':
            return Response(
                {'error': 'Cannot delete a used voucher'},
                status=status.HTTP_400_BAD_REQUEST
            )

        voucher.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)