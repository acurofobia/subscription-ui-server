"""Local native provisioning and the VPN four-node Xray graph adapter.

The graph traversal is ported from VPN without its Pydantic or panel dependency.
"""
import base64
import json
from pathlib import Path
import re


MAX_SUBSCRIPTION_BYTES = 8 * 1024 * 1024
UUID_PATTERN = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
FAILOVER_DEFAULTS = {'selector': 'vk-wg', 'intervalMilliseconds': 10000,
                     'timeoutMilliseconds': 5000, 'failureThreshold': 2,
                     'recoveryThreshold': 3}


class SubscriptionAdaptationError(ValueError):
    def __init__(self, code: str = 'invalid_upstream_subscription'):
        self.code = code
        super().__init__(code)


def _require(condition: bool) -> None:
    if not condition:
        raise SubscriptionAdaptationError()


def _keys(value: dict, allowed: set[str]) -> None:
    _require(isinstance(value, dict) and not value.keys() - allowed)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(c.isspace() or ord(c) < 32 for c in value)


def _reject_constant(value: str) -> None:
    raise ValueError('Non-finite JSON number')


def load_json(path: Path, maximum: int = MAX_SUBSCRIPTION_BYTES) -> object:
    """Read bounded strict JSON, never including file content in errors."""
    with path.open('rb') as source:
        payload = source.read(maximum + 1)
    _require(len(payload) <= maximum)
    document = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=_reject_constant)
    json.dumps(document, allow_nan=False)
    return document


def validate_provisioning(document: object) -> dict[str, object]:
    """Validate the native wire format; unavailable responses carry no keys."""
    _require(isinstance(document, dict))
    _keys(document, {'schemaVersion', 'kind', 'state', 'reason', 'outbounds', 'failover'})
    _require(type(document.get('schemaVersion')) is int and document['schemaVersion'] == 1)
    _require(document.get('kind') == 'vless-failover')
    _require(document.get('state') in ('available', 'restricted', 'unavailable'))
    reason = document.get('reason')
    _require(reason is None or isinstance(reason, str) and len(reason) <= 4096)
    nodes = document.get('outbounds')
    policy = document.get('failover')
    if document['state'] != 'available':
        _require(nodes == [] and policy is None and reason in ('not_allowed', 'traffic_limited'))
        return document
    _require(reason is None and isinstance(nodes, list) and len(nodes) == 4)
    tags = []
    for node in nodes:
        _keys(node, {'type', 'tag', 'server', 'server_port', 'uuid', 'flow', 'tls'})
        _require(node.get('type') == 'vless')
        tag = node.get('tag')
        _require(isinstance(tag, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', tag) is not None
                 and tag not in ('direct', 'vk-wg') and tag not in tags)
        tags.append(tag)
        _require(_text(node.get('server')))
        _require(type(node.get('server_port')) is int and 1 <= node['server_port'] <= 65535)
        _require(isinstance(node.get('uuid'), str) and UUID_PATTERN.fullmatch(node['uuid']) is not None)
        _require(node.get('flow', '') in ('', 'xtls-rprx-vision'))
        tls = node.get('tls')
        _keys(tls, {'enabled', 'server_name', 'utls', 'reality', 'alpn'})
        _require(tls.get('enabled') is True and _text(tls.get('server_name')))
        utls, reality = tls.get('utls'), tls.get('reality')
        _keys(utls, {'enabled', 'fingerprint'})
        _require(utls.get('enabled') is True and utls.get('fingerprint') in
                 ('chrome', 'firefox', 'edge', 'safari', 'ios', 'android', 'random', 'randomized', '360', 'qq'))
        _keys(reality, {'enabled', 'public_key', 'short_id'})
        key, short = reality.get('public_key'), reality.get('short_id')
        _require(reality.get('enabled') is True and isinstance(key, str)
                 and re.fullmatch(r'[A-Za-z0-9_-]{43}', key) is not None)
        _require(isinstance(short, str) and re.fullmatch(r'(?:[0-9a-fA-F]{2}){0,8}', short) is not None)
        if 'alpn' in tls:
            _require(isinstance(tls['alpn'], list) and all(_text(value) for value in tls['alpn']))
    _keys(policy, set(FAILOVER_DEFAULTS) | {'primary', 'backups'})
    for key, expected in FAILOVER_DEFAULTS.items():
        _require(type(policy.get(key)) is type(expected) and policy[key] == expected)
    primary, backups = policy.get('primary'), policy.get('backups')
    _require(isinstance(primary, str) and isinstance(backups, list) and len(backups) == 3)
    order = [primary, *backups]
    _require(all(isinstance(tag, str) for tag in order) and len(set(order)) == 4 and set(order) == set(tags))
    return document


def _node(node: dict) -> dict[str, object]:
    # Explicit non-routing metadata accepted from Xray's generated documents.
    _keys(node, {'tag', 'protocol', 'settings', 'streamSettings', 'mux'})
    _require(node.get('mux') in (None, {'enabled': False}))
    _keys(node['settings'], {'vnext'})
    peers = node['settings']['vnext']
    _require(isinstance(peers, list) and len(peers) == 1)
    peer = peers[0]
    _keys(peer, {'address', 'port', 'users'})
    _require(_text(peer['address']) and type(peer['port']) is int and 1 <= peer['port'] <= 65535)
    _require(isinstance(peer['users'], list) and len(peer['users']) == 1)
    user = peer['users'][0]
    _keys(user, {'id', 'encryption', 'flow', 'email', 'level'})
    _require(user.get('encryption') == 'none' and user.get('flow', '') in ('', 'xtls-rprx-vision'))
    _require(isinstance(user.get('id'), str) and UUID_PATTERN.fullmatch(user['id']) is not None)
    stream = node['streamSettings']
    _keys(stream, {'network', 'security', 'realitySettings', 'tcpSettings', 'rawSettings'})
    _require(stream.get('network') in ('raw', 'tcp') and stream.get('security') == 'reality')
    for key in ('tcpSettings', 'rawSettings'):
        if key in stream:
            _keys(stream[key], {'header'})
            _require(stream[key].get('header', {'type': 'none'}) == {'type': 'none'})
    reality = stream['realitySettings']
    _keys(reality, {'serverName', 'fingerprint', 'publicKey', 'shortId', 'show', 'spiderX', 'alpn'})
    _require(reality.get('show', False) is False and reality.get('spiderX', '') in ('', '/'))
    _require(_text(reality['serverName']) and reality.get('fingerprint') in
             ('chrome', 'firefox', 'edge', 'safari', 'ios', 'android', 'random', 'randomized', '360', 'qq'))
    key = reality['publicKey']
    _require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', key) is not None)
    _require(len(base64.urlsafe_b64decode(key + '=')) == 32)
    short = reality.get('shortId', '')
    _require(isinstance(short, str) and re.fullmatch(r'(?:[0-9a-fA-F]{2}){0,8}', short) is not None)
    tls = {'enabled': True, 'server_name': reality['serverName'], 'utls': {'enabled': True, 'fingerprint': reality['fingerprint']},
           'reality': {'enabled': True, 'public_key': key, 'short_id': short}}
    if 'alpn' in reality:
        _require(isinstance(reality['alpn'], list) and all(_text(v) for v in reality['alpn']))
        tls['alpn'] = reality['alpn']
    return dict(type='vless', tag=node['tag'], server=peer['address'], server_port=peer['port'], uuid=user['id'], flow=user.get('flow', ''), tls=tls)


def _graph(config: dict) -> dict[str, object]:
    outbounds = config['outbounds']
    _require(isinstance(outbounds, list) and all(isinstance(n, dict) and _text(n.get('tag')) for n in outbounds))
    nodes = {n['tag']: n for n in outbounds}
    _require(len(nodes) == len(outbounds))
    rules, balancers = config['routing']['rules'], config['routing']['balancers']
    _require(isinstance(rules, list) and isinstance(balancers, list))
    by_tag = {b['tag']: b for b in balancers}
    _require(len(by_tag) == len(balancers))
    current, seen, selected = 'proxy', set(), []
    while True:
        _require(current not in seen and current in nodes)
        seen.add(current)
        loop = nodes[current]
        if loop.get('protocol') == 'blackhole':
            _require(len(selected) == 4)
            break
        _require(loop.get('protocol') == 'loopback' and len(selected) < 4)
        inbound = loop['settings']['inboundTag']
        matching = [r for r in rules if inbound in r.get('inboundTag', [])]
        _require(len(matching) == 1)
        rule = matching[0]
        _keys(rule, {'type', 'inboundTag', 'balancerTag', 'network'})
        # Generated Xray 2.6 documents omit the default field type and explicitly
        # match both networks. A TCP-only/UDP-only chain cannot cover this mode.
        _require(rule.get('type', 'field') == 'field' and
                 rule.get('network') in (None, 'tcp,udp', 'udp,tcp'))
        balancer = by_tag[rule['balancerTag']]
        _keys(balancer, {'tag', 'selector', 'fallbackTag', 'strategy'})
        if 'strategy' in balancer:
            strategy = balancer['strategy']
            _keys(strategy, {'type', 'settings'})
            # With exactly one resolved candidate these strategies have the
            # same choice; only empty strategy settings are insignificant.
            _require(strategy.get('type') in ('leastPing', 'random', 'roundRobin') and
                     strategy.get('settings', {}) == {})
        prefixes = balancer['selector']
        _require(isinstance(prefixes, list) and bool(prefixes) and all(_text(p) for p in prefixes))
        targets = [n for tag, n in nodes.items() if any(tag.startswith(p) for p in prefixes)]
        _require(len(targets) == 1 and targets[0].get('protocol') == 'vless')
        node = targets[0]
        tag = node['tag']
        _require(tag not in ('direct', 'vk-wg') and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', tag) is not None
                 and tag not in [v['tag'] for v in selected])
        selected.append(_node(node))
        current = balancer['fallbackTag']
    return dict(schemaVersion=1, kind='vless-failover', state='available', reason=None, outbounds=selected,
                failover={**FAILOVER_DEFAULTS, 'primary': selected[0]['tag'], 'backups': [v['tag'] for v in selected[1:]]})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def adapt_xray_subscription(payload: bytes) -> dict[str, object]:
    try:
        _require(len(payload) <= MAX_SUBSCRIPTION_BYTES)
        document = json.loads(payload, object_pairs_hook=_unique, parse_constant=_reject_constant)
        json.dumps(document, allow_nan=False)
        configs = document if isinstance(document, list) else [document]
        candidates = [c for c in configs if isinstance(c, dict) and any(
            isinstance(n, dict) and n.get('tag') == 'proxy' and n.get('protocol') == 'loopback' for n in c.get('outbounds', []))]
        valid = []
        for candidate in candidates:
            try:
                valid.append(_graph(candidate))
            except (ValueError, TypeError, KeyError, AttributeError, SubscriptionAdaptationError):
                continue
        _require(len(valid) == 1)
        return valid[0]
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, SubscriptionAdaptationError):
        raise SubscriptionAdaptationError() from None
