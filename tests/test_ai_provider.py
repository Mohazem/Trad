from types import SimpleNamespace as NS
import unittest
from trading.ai_contracts import sample_packet
from trading.ai_pipeline import AdvisoryPipeline
from trading.ai_providers import OpenAIResponsesProvider, OfflineMockProvider, AgentRequest
from trading.ai_validation import output_schema, timestamp
from trading.models import canonical

NOW = timestamp(sample_packet()['asof'])

def response(text):
    return NS(status='completed', error=None, output=[NS(type='message', content=[NS(type='output_text', text=text)])], output_text=text)

class FakeClient:
    base_url = 'https://api.openai.com/v1/'
    def __init__(self, mutate=None):
        self.options, self.requests, self.mutate = [], [], mutate
        self.responses = self
    def with_options(self, **options):
        self.options.append(options)
        return self
    def create(self, **kwargs):
        self.requests.append(kwargs)
        role = kwargs['text']['format']['name'].removeprefix('trading_')
        request = AgentRequest(role, kwargs['instructions'], kwargs['input'][0]['content'], canonical(kwargs['text']['format']['schema']))
        result = response(OfflineMockProvider().complete(request, 1))
        return self.mutate(result) if self.mutate else result

class ProviderAdapterTests(unittest.TestCase):
    def test_shape_strict_schema_no_tools_no_storage_no_retries(self):
        client = FakeClient()
        result = AdvisoryPipeline(OpenAIResponsesProvider(client=client, model='test-model'), timeout_seconds=2).evaluate(sample_packet(), NOW)
        self.assertEqual(result.status, 'proposed')
        self.assertEqual(len(client.requests), 3)
        for request, options in zip(client.requests, client.options):
            self.assertEqual(options, {'timeout': 2, 'max_retries': 0})
            self.assertEqual(request['tools'], [])
            self.assertEqual(request['tool_choice'], 'none')
            self.assertFalse(request['store'])
            self.assertEqual(request['max_output_tokens'], 2048)
            self.assertTrue(request['text']['format']['strict'])
            self.assertEqual(request['text']['format']['type'], 'json_schema')
            self.assertEqual(request['input'][0]['role'], 'user')
            self.assertEqual(set(request), {'model','instructions','input','text','tools','tool_choice','store','max_output_tokens'})
    def test_refusal_incomplete_error_tools_and_empty_abstain(self):
        def replace_attrs(**changes):
            def change(obj):
                for key, value in changes.items(): setattr(obj, key, value)
                return obj
            return change
        mutations = [replace_attrs(status='incomplete'), replace_attrs(status='failed'), replace_attrs(error='error'),
                     replace_attrs(output_text=''), replace_attrs(output_text='not json'),
                     replace_attrs(output=[]), replace_attrs(output=None),
                     replace_attrs(output=[NS(type='function_call')]),
                     replace_attrs(output=[NS(type='message', content=[NS(type='refusal')])])]
        for mutate in mutations:
            client = FakeClient(mutate)
            result = AdvisoryPipeline(OpenAIResponsesProvider(client=client, model='test-model')).evaluate(sample_packet(), NOW)
            self.assertEqual(result.action, 'abstain')
            self.assertEqual(len(client.requests), 1)
    def test_unapproved_unknown_misleading_endpoint_rejected(self):
        for endpoint in ('', None, 'http://api.openai.com/v1', 'https://api.openai.com.evil.invalid/v1',
                         'https://evil.invalid/v1', 'https://api.openai.com/v1?key=test',
                         'https://user@api.openai.com/v1', 'https://api.openai.com:443/v1'):
            client = FakeClient(); client.base_url = endpoint
            with self.assertRaises(ValueError): OpenAIResponsesProvider(client=client, model='test')
        with self.assertRaises(ValueError): OpenAIResponsesProvider(client=object(), model='test')
    def test_explicit_model_required(self):
        for model in ('', None, 'bad model', 'x' * 101):
            with self.assertRaises(ValueError): OpenAIResponsesProvider(client=FakeClient(), model=model)
    def test_raw_provider_error_never_leaks(self):
        class Fail(FakeClient):
            def create(self, **kwargs): raise RuntimeError('private-test-token')
        result = AdvisoryPipeline(OpenAIResponsesProvider(client=Fail(), model='test')).evaluate(sample_packet(), NOW)
        self.assertEqual(result.code, 'provider_error')
        self.assertNotIn('private-test-token', repr(result))
