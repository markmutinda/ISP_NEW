import logging
from django.db.models import Q

logger = logging.getLogger(__name__)


def kick_pppoe_user(username, sessions=None) -> int:
    """
    Disconnect a user's live router session(s) so they redial and pick up fresh
    RADIUS attributes. Must run inside the correct tenant schema.

    sessions: optional list of (nas_ip, acct_session_id). If None, open radacct rows are used.
    Order: CoA Disconnect first, MikroTik API as fallback. No live session -> no-op.
    """
    from apps.radius.models import RadAcct
    from apps.radius.services.coa_service import CoAService
    from apps.network.models import Router
    from apps.network.integrations.mikrotik_api import MikrotikAPI

    if sessions is None:
        sessions = [
            (str(ip), sid) for ip, sid in
            RadAcct.objects.filter(username=username, acctstoptime__isnull=True)
            .values_list('nasipaddress', 'acctsessionid')
        ]
    if not sessions:
        return 0

    coa = CoAService()
    api_tried = set()
    kicked = 0

    for nas_ip, session_id in sessions:
        ok = False
        try:
            ok = coa.disconnect_user_via_coa(username, nas_ip, session_id)
        except Exception as e:
            logger.warning(f"[KICK] CoA error {username}@{nas_ip}: {e}")

        if not ok and nas_ip not in api_tried:
            api_tried.add(nas_ip)
            router = Router.objects.filter(
                Q(vpn_ip_address=nas_ip) | Q(ip_address=nas_ip)
            ).first()
            if router:
                try:
                    ok = bool(MikrotikAPI(router).remove_pppoe_active_user(username))
                except Exception as e:
                    logger.warning(f"[KICK] API fallback failed {username}@{nas_ip}: {e}")

        if ok:
            kicked += 1

    logger.info(f"[KICK] {username}: {kicked}/{len(sessions)} session(s) disconnected")
    return kicked