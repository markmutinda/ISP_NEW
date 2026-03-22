from django.utils import timezone

from apps.radius.models import RadReply
from apps.radius.tasks import disconnect_user_immediately


class FUPRadiusService:
    RATE_LIMIT_ATTR = 'Mikrotik-Rate-Limit'

    def build_rate_limit(self, down_mbps: int, up_mbps: int) -> str:
        down_kbps = int(down_mbps) * 1000
        up_kbps = int(up_mbps) * 1000
        return f'{up_kbps}k/{down_kbps}k'

    def apply_throttle(self, username: str, down_mbps: int, up_mbps: int):
        value = self.build_rate_limit(down_mbps, up_mbps)

        RadReply.objects.update_or_create(
            username=username,
            attribute=self.RATE_LIMIT_ATTR,
            defaults={'op': ':=', 'value': value},
        )

        disconnect_user_immediately.delay(username=username)

        return value

    def release_throttle(self, username: str, original_down_mbps: int, original_up_mbps: int):
        value = self.build_rate_limit(original_down_mbps, original_up_mbps)

        RadReply.objects.update_or_create(
            username=username,
            attribute=self.RATE_LIMIT_ATTR,
            defaults={'op': ':=', 'value': value},
        )

        disconnect_user_immediately.delay(username=username)
        return value