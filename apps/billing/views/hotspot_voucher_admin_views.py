from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models.hotspot_models import HotspotPlan
from apps.billing.models.voucher_models import Voucher, VoucherBatch
from apps.core.permissions import IsCompanyStaff


class HotspotVoucherGenerateView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsCompanyStaff]

    def post(self, request):
        plan_id = request.data.get('plan_id')
        quantity = int(request.data.get('quantity') or 0)
        valid_days = int(request.data.get('valid_days') or 30)
        prefix = (request.data.get('prefix') or 'VCH').strip()[:10] or 'VCH'

        if not plan_id:
            return Response({'error': 'plan_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        if quantity <= 0:
            return Response({'error': 'quantity must be greater than 0'}, status=status.HTTP_400_BAD_REQUEST)
        if valid_days <= 0:
            return Response({'error': 'valid_days must be greater than 0'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            plan = HotspotPlan.objects.get(id=plan_id, is_active=True)
        except HotspotPlan.DoesNotExist:
            return Response({'error': 'Hotspot plan not found'}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        batch = VoucherBatch.objects.create(
            name=f"{plan.name} Voucher Batch",
            description=f"Auto-generated vouchers for hotspot plan: {plan.name}",
            voucher_type='PREPAID',
            face_value=plan.price,
            sale_price=plan.price,
            valid_from=now,
            valid_to=now + timedelta(days=valid_days),
            is_reusable=False,
            max_uses=1,
            quantity=quantity,
            status='ACTIVE',
            is_active=True,
            prefix=prefix,
            plan_restriction=True,
            hotspot_plan=plan,
            created_by=request.user,
        )

        vouchers = batch.generate_vouchers(quantity)

        return Response({
            'message': f'Generated {len(vouchers)} vouchers for plan {plan.name}',
            'batch': {
                'id': batch.id,
                'batch_number': batch.batch_number,
                'plan_id': str(plan.id),
                'plan_name': plan.name,
                'price': str(plan.price),
                'valid_to': batch.valid_to,
            },
            'vouchers': [
                {
                    'id': v.id,
                    'code': v.code,
                    'pin': v.pin,
                    'status': v.status,
                    'expires_at': v.valid_to,
                    'plan_name': plan.name,
                    'plan_id': str(plan.id),
                }
                for v in vouchers
            ]
        }, status=status.HTTP_201_CREATED)


class HotspotVoucherListView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsCompanyStaff]

    def get(self, request):
        status_filter = (request.query_params.get('status') or 'all').lower()
        plan_id = request.query_params.get('plan_id')

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

        data = [
            {
                'id': v.id,
                'code': v.code,
                'pin': v.pin,
                'status': v.status,
                'use_count': v.use_count,
                'is_valid': v.is_valid(),
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