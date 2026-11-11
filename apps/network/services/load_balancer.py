"""Netily multi-WAN load balancing + failover (RouterOS v7). All config is tagged 'NLB:'."""
import hashlib
import ipaddress
import re
from functools import reduce
from math import gcd

from django.conf import settings

MASK = '********'
MIN_UPLINKS, MAX_UPLINKS = 2, 6
_PORT_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,32}$')
_MODES = ('dhcp', 'pppoe', 'static')


def _q(v):
    return str('' if v is None else v).replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')


def _ipv4(v):
    try:
        return str(ipaddress.IPv4Address(str(v).strip()))
    except ValueError:
        return None


# ───────────────────────── validation ─────────────────────────
def validate_uplinks(raw, router, existing=None):
    """Returns (clean_uplinks, errors). Keeps stored PPPoE passwords when the UI sends blank/mask."""
    if not isinstance(raw, list) or not MIN_UPLINKS <= len(raw) <= MAX_UPLINKS:
        return [], [f'Configure between {MIN_UPLINKS} and {MAX_UPLINKS} uplinks.']

    errors, clean = [], []
    bridged = set(router.hotspot_interfaces or [])
    old = {u.get('port'): u for u in (existing or [])}
    seen_port, seen_mon, seen_slug = set(), set(), set()

    for i, u in enumerate(raw, 1):
        t = f'ISP {i}'
        if not isinstance(u, dict):
            errors.append(f'{t}: invalid entry.')
            continue
        name = str(u.get('name') or '').strip()[:24]
        mode = u.get('mode')
        port = str(u.get('port') or '').strip()
        mon = _ipv4(u.get('monitor_ip'))
        row = {'name': name, 'mode': mode, 'port': port, 'monitor_ip': mon,
               'vlan_id': None, 'weight': u.get('weight')}

        if not name:
            errors.append(f'{t}: ISP name is required.')
        if mode not in _MODES:
            errors.append(f'{t}: choose DHCP, PPPoE or Static.')
        if not _PORT_RE.match(port):
            errors.append(f'{t}: invalid Ethernet port.')
        elif port in bridged:
            errors.append(f'{t}: {port} is a hotspot bridge port - pick another port.')
        elif port in seen_port:
            errors.append(f'{t}: {port} is already used by another ISP.')
        seen_port.add(port)
        if mon is None:
            errors.append(f'{t}: monitor IP is invalid.')
        elif mon in seen_mon:
            errors.append(f'{t}: monitor IP must be different for every ISP.')
        seen_mon.add(mon)

        vlan = u.get('vlan_id')
        if vlan not in (None, ''):
            try:
                vlan = int(vlan)
                if not 1 <= vlan <= 4094:
                    raise ValueError
                row['vlan_id'] = vlan
            except (TypeError, ValueError):
                errors.append(f'{t}: VLAN ID must be 1-4094.')

        if mode == 'pppoe':
            user = str(u.get('pppoe_username') or '').strip()
            pwd = str(u.get('pppoe_password') or '')
            if not pwd or pwd == MASK:
                pwd = (old.get(port) or {}).get('pppoe_password', '')
            if not user or not pwd:
                errors.append(f'{t}: PPPoE username and password are required.')
            row.update(pppoe_username=user, pppoe_password=pwd)
        elif mode == 'static':
            addr, gw = _ipv4(u.get('address')), _ipv4(u.get('gateway'))
            try:
                prefix = int(u.get('prefix') or 32)
            except (TypeError, ValueError):
                prefix = 0
            if not addr or not gw or not 1 <= prefix <= 32:
                errors.append(f'{t}: static IP, prefix and gateway are required.')
            row.update(address=addr, gateway=gw, prefix=prefix)

        base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:10] or f'isp{i}'
        slug, n = base, 2
        while slug in seen_slug:
            slug, n = f'{base[:8]}-{n}', n + 1
        seen_slug.add(slug)
        row['slug'] = slug
        clean.append(row)

    try:
        ws = [int(r['weight']) for r in clean]
        if sum(ws) != 100 or any(w < 5 or w % 5 for w in ws):
            raise ValueError
        for r, w in zip(clean, ws):
            r['weight'] = w
    except (TypeError, ValueError):
        errors.append('Connection split must total 100% in steps of 5%, minimum 5% per ISP.')

    return clean, errors


def public_uplinks(uplinks):
    return [{**{k: v for k, v in u.items() if k != 'pppoe_password'},
             'has_password': bool(u.get('pppoe_password'))} for u in (uplinks or [])]


# ───────────────────────── script generation ─────────────────────────
def _slots(weights):
    g = reduce(gcd, weights)
    w = [x // g for x in weights]
    slots, r = [], 0
    for i, x in enumerate(w):
        for _ in range(x):
            slots.append((i, r))
            r += 1
    return sum(w), slots


_CLEAN = [
    '/tool netwatch', '/ip firewall mangle', '/ip firewall nat', '/ip route', '/routing table',
    '/ip dhcp-client', '/interface pppoe-client', '/ip address', '/interface vlan',
    '/interface list member', '/ip firewall address-list', '/system script',
]


def _cleanup():
    return [f':do {{ {m} remove [find comment~"NLB:"] }} on-error={{}}' for m in _CLEAN]


def _no_pcc(router):
    from apps.network.services.ipam_calculator import calculate_mikrotik_hotspot_network
    m = calculate_mikrotik_hotspot_network(router.hotspot_base_ip, router.hotspot_subnet_cidr)
    ents = [f"{m['network']}/{router.hotspot_subnet_cidr}", router.pppoe_pool,
            getattr(settings, 'VPN_NETWORK_CIDR', '10.8.0.0/16'),
            getattr(settings, 'OPENVPN_NETWORK_CIDR', '10.9.0.0/16')]
    return list(dict.fromkeys(e for e in ents if e))


def _body(router, ups, mask=False):
    n = len(ups)
    order = sorted(range(n), key=lambda i: (-ups[i]['weight'], i))
    rank = {i: r + 1 for r, i in enumerate(order)}
    total, slots = _slots([u['weight'] for u in ups])
    # v7 guard — tonum [:pick ver 0 [:find ver "."]] handles both "7.24.4" and bare "6".
    L = [':if ([:tonum [:pick [/system resource get version] 0 [:find [/system resource get version] "."]]] < 7) '
         'do={ :error "Netily load balancing requires RouterOS v7" }',
         ':put ">>> Netily Load Balancer: applying..."']
    L += _cleanup()

    # resolved interface per uplink + gateway spec
    for i, u in enumerate(ups):
        u['rif'] = u['port']
    L.append(':do { /interface list add name=WAN } on-error={}')

    for i, u in enumerate(ups):
        s, base = u['slug'], u['port']
        L.append(f':do {{ /ip dhcp-client remove [find interface="{base}"] }} on-error={{}}')
        if u['vlan_id']:
            u['rif'] = f'vl{u["vlan_id"]}-{i + 1}'
            L.append(f'/interface vlan add name={u["rif"]} interface={base} vlan-id={u["vlan_id"]} comment="NLB:vlan:{s}"')
        if u['mode'] == 'pppoe':
            pw = MASK if mask else _q(u['pppoe_password'])
            pname = f'wan-{s}'
            L.append(f'/interface pppoe-client add name={pname} interface={u["rif"]} user="{_q(u["pppoe_username"])}" '
                     f'password="{pw}" add-default-route=no use-peer-dns=no disabled=no comment="NLB:pppoe:{s}"')
            u['rif'] = pname
        elif u['mode'] == 'static':
            net = f' network={u["gateway"]}' if u['prefix'] == 32 else ''
            L.append(f'/ip address add address={u["address"]}/{u["prefix"]}{net} interface={u["rif"]} comment="NLB:addr:{s}"')
        else:
            gw = f'($gw . "%{u["rif"]}")'
            L.append(
                f'/system script add name=NLB-sync-{s} comment="NLB:sync:{s}" source={{\n'
                f':local gw [/ip dhcp-client get [find comment="NLB:dhcp:{s}"] gateway]\n'
                f':if ($gw != "") do={{ ' 
                f'/ip route set [find comment="NLB:rt:{s}"] gateway={gw} check-gateway=ping; '
                f'/ip route set [find comment="NLB:mon:{s}"] gateway={gw} }}\n}}')
            L.append(f'/ip dhcp-client add interface={u["rif"]} add-default-route=no use-peer-dns=no use-peer-ntp=no '
                     f'script="/system script run NLB-sync-{s}" comment="NLB:dhcp:{s}"')
        L.append(f'/interface list member add list=WAN interface={u["rif"]} comment="NLB:wanlist:{s}"')
        L.append(f'/routing table add name=TO-{s} fib comment="NLB:table:{s}"')

    for e in _no_pcc(router):
        L.append(f'/ip firewall address-list add list=no-pcc address={e} comment="NLB:nopcc"')

    def gs(u):
        return f'{u["gateway"]}%{u["rif"]}' if u['mode'] == 'static' else u['rif']

    def chk(u):
        return ' check-gateway=ping' if u['mode'] == 'static' else ''

    # routes: monitor host route, main failover chain, per-uplink tables with failover
    for i, u in enumerate(ups):
        s = u['slug']
        L.append(f'/ip route add dst-address={u["monitor_ip"]}/32 gateway={gs(u)} comment="NLB:mon:{s}"')
        L.append(f'/ip route add dst-address=0.0.0.0/0 gateway={gs(u)} distance={rank[i]}{chk(u)} comment="NLB:rt:{s}"')
    for i, u in enumerate(ups):
        s = u['slug']
        others = [j for j in order if j != i]
        for dist, j in enumerate([i] + others, 1):
            x = ups[j]
            L.append(f'/ip route add dst-address=0.0.0.0/0 gateway={gs(x)} routing-table=TO-{s} distance={dist}'
                     f'{chk(x)} comment="NLB:rt:{x["slug"]}"')

    # mangle (order matters)
    for u in ups:
        s, r = u['slug'], u['rif']
        L.append(f'/ip firewall mangle add chain=prerouting in-interface={r} connection-mark=no-mark connection-state=new '
                 f'action=mark-connection new-connection-mark={s}-CONN passthrough=yes comment="NLB:conn:{s}"')
        L.append(f'/ip firewall mangle add chain=output connection-mark={s}-CONN dst-address-type=!local '
                 f'dst-address-list=!no-pcc action=mark-routing new-routing-mark=TO-{s} passthrough=no comment="NLB:out:{s}"')
    L.append('/ip firewall mangle add chain=prerouting hotspot=from-client,!auth action=return comment="NLB:skip-unauth"')
    L.append('/ip firewall mangle add chain=prerouting protocol=udp dst-port=53 hotspot=from-client action=return comment="NLB:skip-dns"')
    L.append('/ip firewall mangle add chain=prerouting protocol=tcp dst-port=53 hotspot=from-client action=return comment="NLB:skip-dns"')
    for u in ups:
        s = u['slug']
        L.append(f'/ip firewall mangle add chain=prerouting connection-mark={s}-CONN in-interface-list=!WAN '
                 f'action=mark-routing new-routing-mark=TO-{s} passthrough=no comment="NLB:reply:{s}"')
    for i, r in slots:
        s = ups[i]['slug']
        L.append(f'/ip firewall mangle add chain=prerouting in-interface-list=!WAN dst-address-type=!local '
                 f'dst-address-list=!no-pcc per-connection-classifier=both-addresses-and-ports:{total}/{r} '
                 f'action=mark-routing new-routing-mark=TO-{s} passthrough=no comment="NLB:pcc:{s}:{r}"')

    # NAT (placed above the unfiltered Netily-Masquerade when present)
    for u in ups:
        s, r = u['slug'], u['rif']
        rule = f'/ip firewall nat add chain=srcnat out-interface={r} action=masquerade comment="NLB:nat:{s}"'
        L.append(f':do {{ {rule} place-before=[/ip firewall nat find comment="Netily-Masquerade"] }} on-error={{ {rule} }}')

    # health: netwatch toggles every route that uses this uplink (host route stays so pings keep flowing)
    for u in ups:
        s = u['slug']
        L.append(f':do {{ /tool netwatch add host={u["monitor_ip"]} interval=10s timeout=3s comment="NLB:nw:{s}" '
                 f'down-script="/ip route disable [find comment=\\"NLB:rt:{s}\\"]" '
                 f'up-script="/ip route enable [find comment=\\"NLB:rt:{s}\\"]" }} '
                 f'on-error={{ :put "netwatch unavailable - using gateway ping only" }}')

    # DHCP uplinks: wait for lease, then sync gateway into routes
    for i, u in enumerate(ups):
        if u['mode'] == 'dhcp':
            s = u['slug']
            L.append(f':local w{i} 0')
            L.append(f':while ([:len [/ip dhcp-client find comment="NLB:dhcp:{s}" status=bound]] = 0 && $w{i} < 15) '
                     f'do={{ :delay 2s; :set w{i} ($w{i} + 1) }}')
            L.append(f':do {{ /system script run NLB-sync-{s} }} on-error={{}}')

    L.append(f':put ">>> Netily Load Balancer: {n} uplinks active ({total} PCC slots)."')
    return '\n'.join(L)


def _remove_body():
    return '\n'.join(_cleanup() + [':put ">>> Netily Load Balancer removed. Re-configure your WAN manually."'])


def script_for(router, cfg, base_url, mask=False):
    """Returns (script, hash). Hash is computed on the unmasked body; ack line is appended after."""
    def _lf(s):
        return s.replace('\r\n', '\n').replace('\r', '\n')

    ups = [dict(u) for u in cfg.uplinks]
    if cfg.is_enabled:
        body = _lf(_body(router, [dict(u) for u in ups]))
        shown = _lf(_body(router, ups, mask=True)) if mask else body
    else:
        body = shown = _lf(_remove_body())
    h = hashlib.sha1(body.encode()).hexdigest()
    ack = (f'\n:do {{ /tool fetch url="{base_url}/{router.auth_key}/{router.provision_slug}/lb-ack?h={h}" '
           f'keep-result=no check-certificate=no }} on-error={{}}')
    return shown + ack, h


def magic_link(router, base_url):
    url = f'{base_url}/{router.auth_key}/{router.provision_slug}/lb.rsc'
    return f'/tool fetch url="{url}" dst-path="netily-lb.rsc" check-certificate=no; :delay 2s; /import netily-lb.rsc'