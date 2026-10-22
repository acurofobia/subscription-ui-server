"""Synthetic graphs: order is encoded in references, never group or array order."""
import copy
import json
import unittest
from provisioning import adapt_xray_subscription, SubscriptionAdaptationError

UUID = 'bf000d23-0752-40b4-affe-68f7707a9661'
KEY = 'A' * 43


def graph(order=('n1', 'n2', 'n3', 'n4')):
    nodes = [dict(tag=tag, protocol='vless', settings={'vnext': [{'address': tag + '.example.org', 'port': 443,
             'users': [{'id': UUID, 'encryption': 'none', 'flow': 'xtls-rprx-vision'}]}]},
             streamSettings={'network': 'raw', 'security': 'reality', 'realitySettings': {
                 'serverName': 'example.org', 'fingerprint': 'chrome', 'publicKey': KEY, 'shortId': 'abcd'}}) for tag in order]
    loops = [dict(tag='proxy' if i == 0 else 'loop' + str(i), protocol='loopback', settings={'inboundTag': 'in' + str(i)}) for i in range(4)]
    balancers = [dict(tag='bal' + str(i), selector=[tag], fallbackTag='loop' + str(i + 1) if i < 3 else 'blocked') for i, tag in enumerate(order)]
    rules = [dict(type='field', inboundTag=['in' + str(i)], balancerTag='bal' + str(i)) for i in range(4)]
    return {'outbounds': nodes + loops + [{'tag': 'blocked', 'protocol': 'blackhole'}], 'routing': {'rules': rules, 'balancers': balancers}}


class AdapterTests(unittest.TestCase):
    def adapt(self, config):
        return adapt_xray_subscription(json.dumps(config).encode())

    def test_adapts_each_group_without_group_name_mapping(self):
        for quartet in [('n1', 'n2', 'n3', 'n4'), ('n5', 'n6', 'n7', 'n8')]:
            for i in range(4):
                order = quartet[i:] + quartet[:i]
                doc = self.adapt(graph(order))
                self.assertEqual(doc['failover']['primary'], order[0])
                self.assertEqual(doc['failover']['backups'], list(order[1:]))
                self.assertEqual([n['tag'] for n in doc['outbounds']], list(order))
                self.assertEqual(doc['outbounds'][0]['uuid'], UUID)
                self.assertEqual(doc['outbounds'][0]['tls']['reality']['public_key'], KEY)
                self.assertEqual(doc['outbounds'][0]['server_port'], 443)
                self.assertEqual(doc['failover']['failureThreshold'], 2)

    def test_follows_graph_after_array_reordering(self):
        config = graph()
        expected = self.adapt(config)
        config['outbounds'].reverse()
        config['routing']['balancers'].reverse()
        config['routing']['rules'].reverse()
        self.assertEqual(self.adapt([{'outbounds': [graph()['outbounds'][0]]}, config]), expected)
        invalid = graph(); invalid['routing']['balancers'][0]['fallbackTag'] = 'missing'
        self.assertEqual(self.adapt([invalid, config]), expected)

    def test_accepts_generated_field_defaults_and_single_target_round_robin(self):
        config = graph()
        for rule in config['routing']['rules']:
            rule.pop('type')
            rule['network'] = 'tcp,udp'
        config['routing']['rules'].append({'network':'tcp,udp','balancerTag':'bal0'})
        for balancer in config['routing']['balancers']:
            balancer['strategy'] = {'type':'roundRobin','settings':{}}
        for node in config['outbounds'][:4]:
            node['streamSettings']['network'] = 'tcp'
            node['streamSettings']['tcpSettings'] = {}
            node['streamSettings']['realitySettings'].pop('shortId')
        doc = self.adapt(config)
        self.assertEqual(doc['failover']['primary'], 'n1')
        self.assertEqual(doc['failover']['backups'], ['n2','n3','n4'])
        self.assertEqual(doc['outbounds'][0]['tls']['reality']['short_id'], '')
        for network in ['tcp', 'udp', 'tcp,udp,other']:
            bad = copy.deepcopy(config);bad['routing']['rules'][0]['network'] = network
            with self.assertRaises(SubscriptionAdaptationError): self.adapt(bad)
        for strategy in [{'type':'unknown'}, {'type':'roundRobin','settings':{'unexpected':True}}]:
            bad=copy.deepcopy(config);bad['routing']['balancers'][0]['strategy']=strategy
            with self.assertRaises(SubscriptionAdaptationError): self.adapt(bad)

    def test_rejects_ambiguous_or_unsafe_graph(self):
        cases = [[graph(), graph()]]
        for change in ['cycle', 'missing', 'duplicate', 'prefix', 'three', 'five', 'freedom', 'reserved', 'duplicate_rule']:
            c = graph()
            if change == 'cycle': c['routing']['balancers'][3]['fallbackTag'] = 'proxy'
            if change == 'missing': c['routing']['balancers'][0]['fallbackTag'] = 'absent'
            if change == 'duplicate': c['outbounds'].append(copy.deepcopy(c['outbounds'][0]))
            if change == 'prefix': c['routing']['balancers'][0]['selector'] = ['n']
            if change == 'three': c['routing']['balancers'][2]['fallbackTag'] = 'blocked'
            if change == 'five': c['routing']['balancers'][3]['fallbackTag'] = 'loop1'
            if change == 'freedom': c['outbounds'][-1]['protocol'] = 'freedom'
            if change == 'reserved': c['outbounds'][0]['tag'] = 'direct'; c['routing']['balancers'][0]['selector'] = ['direct']
            if change == 'duplicate_rule': c['routing']['rules'].append(copy.deepcopy(c['routing']['rules'][0]))
            cases.append(c)
        for c in cases:
            with self.subTest(config=c), self.assertRaises(SubscriptionAdaptationError): self.adapt(c)

    def test_rejects_unsupported_transport_or_vless_options(self):
        for field, value in [('network', 'ws'), ('network', 'grpc'), ('network', 'xhttp'), ('network', 'kcp'), ('security', 'tls'), ('sockopt', {'tcpFastOpen': True})]:
            c = graph(); c['outbounds'][0]['streamSettings'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SubscriptionAdaptationError): self.adapt(c)
        for key, value in [('id', 'invalid'), ('id', UUID.replace('-', '')), ('id', 'urn:uuid:' + UUID),
                           ('encryption', 'aes'), ('flow', 'unknown')]:
            c = graph(); c['outbounds'][0]['settings']['vnext'][0]['users'][0][key] = value
            with self.assertRaises(SubscriptionAdaptationError): self.adapt(c)
        for port in [True, 0, 65536, 443.5]:
            c = graph(); c['outbounds'][0]['settings']['vnext'][0]['port'] = port
            with self.assertRaises(SubscriptionAdaptationError): self.adapt(c)
        for key, value in [('publicKey', ''), ('shortId', 'xyz'), ('shortId', 'a')]:
            c = graph(); c['outbounds'][0]['streamSettings']['realitySettings'][key] = value
            with self.assertRaises(SubscriptionAdaptationError): self.adapt(c)

    def test_rejects_large_invalid_and_duplicate_json_and_redacts_errors(self):
        for data in [b'[]', b'null', b'not json', b' ' * (8 * 1024 * 1024 + 1), b'{"outbounds":[],"outbounds":[]}']:
            with self.assertRaises(SubscriptionAdaptationError) as error:
                adapt_xray_subscription(data)
            self.assertNotIn(UUID, str(error.exception))
