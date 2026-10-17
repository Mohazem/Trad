from datetime import datetime
import json
import unittest
from unittest.mock import patch
from trading.ai_contracts import sample_packet
from trading.ai_pipeline import AdvisoryPipeline
from trading.ai_providers import OfflineMockProvider
from trading.model_routing import configured_roles
from tests.test_history import signal

class RoutingTests(unittest.TestCase):
    def test_disabled_and_missing_never_constructs_or_substitutes(self):
        factory=lambda m:self.fail('No model construction')
        self.assertIsNone(configured_roles({},[],factory))
        with self.assertRaises(ValueError):configured_roles({'TRAD_LLM_ROUTING_ENABLED':'1'},[],factory)

    def test_explicit_installed_fallback_and_role_selection(self):
        cfg={'TRAD_LLM_ROUTING_ENABLED':'1','TRAD_LLM_FALLBACK_MODEL':'qwen2.5:1.5b'}
        roles=configured_roles(cfg,[{'name':'qwen2.5:1.5b'}],lambda m:m)
        self.assertEqual(set(roles.values()),{'qwen2.5:1.5b'})
        roles=configured_roles({'TRAD_LLM_ROUTING_ENABLED':'1'},[{'name':'qwen3:8b'},{'name':'qwen3:14b'}],lambda m:m)
        self.assertEqual(roles['critic'],'qwen3:14b')

    def test_each_role_receives_only_validated_predecessors_and_metrics(self):
        seen=[]
        class RoleProvider(OfflineMockProvider):
            def __init__(self,name):self.model=name
            def complete(self,request,timeout):
                self.assert_role=request.role
                seen.append((self.model,request.role,list(json.loads(request.input_json)['previous_outputs'])))
                return super().complete(request,timeout)
        roles={r:RoleProvider(r) for r in ('analyst','critic','synthesizer')}
        packet=sample_packet();now=datetime.fromisoformat(packet['asof'])
        result=AdvisoryPipeline(role_providers=roles).evaluate(packet,now)
        self.assertEqual(result.status,'proposed')
        self.assertEqual([r[2] for r in seen],[[],['analyst'],['analyst','critic']])
        self.assertTrue(all(m['json_valid'] for m in result.role_metrics.values()))
        self.assertEqual(result.role_metrics['critic']['model'],'critic')

    def test_bad_quant_signal_no_provider_call(self):
        p=sample_packet();now=datetime.fromisoformat(p['asof'])
        with patch.object(OfflineMockProvider,'complete',side_effect=AssertionError('No call')):
            result=AdvisoryPipeline().evaluate(p,now,signals=[{'action':'BUY'}])
            self.assertEqual(result.code,'invalid_quant_context')

    def test_role_error_abstains_without_fallback(self):
        class Failed(OfflineMockProvider):
            def complete(self,*a):raise RuntimeError('secret')
        roles={'analyst':OfflineMockProvider(),'critic':Failed(),'synthesizer':Failed()}
        p=sample_packet();r=AdvisoryPipeline(role_providers=roles).evaluate(p,datetime.fromisoformat(p['asof']))
        self.assertEqual((r.code,r.calls),('provider_error',2))
        self.assertNotIn('secret',str(r))


class RoutedTimeoutTests(unittest.TestCase):
    def test_timeout_disables_whole_routed_pipeline(self):
        import time
        class Slow(OfflineMockProvider):
            def complete(self,request,timeout):
                time.sleep(.05)
                return super().complete(request,timeout)
        p=sample_packet();now=datetime.fromisoformat(p['asof'])
        roles={'analyst':OfflineMockProvider(),'critic':Slow(),'synthesizer':OfflineMockProvider()}
        pipeline=AdvisoryPipeline(role_providers=roles,timeout_seconds=.01)
        first=pipeline.evaluate(p,now)
        self.assertEqual((first.code,first.calls),('provider_timeout',2))
        self.assertEqual(pipeline.evaluate(p,now).code,'provider_disabled_after_timeout')

    def test_routed_invalid_json_has_no_retry(self):
        class Bad(OfflineMockProvider):
            def complete(self,*a):return '{bad'
        p=sample_packet();now=datetime.fromisoformat(p['asof'])
        roles={'analyst':Bad(),'critic':OfflineMockProvider(),'synthesizer':OfflineMockProvider()}
        result=AdvisoryPipeline(role_providers=roles).evaluate(p,now)
        self.assertEqual((result.code,result.calls),('invalid_model_output',1))
