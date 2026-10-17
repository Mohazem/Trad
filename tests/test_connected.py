from datetime import datetime, timedelta, timezone
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from trading.ai_contracts import sample_packet
from trading.ai_pipeline import AdvisoryPipeline, simulate_snapshot
from trading.ai_providers import AgentRequest, OfflineMockProvider
from trading.ai_validation import ValidationError, validate_packet
from trading.connected import (AlpacaMarket, ConnectedError, HuggingFaceProvider,
                               NoRedirect, OllamaProvider, analyze, configuration_status,
                               local_json, market_packet, normalize_bars, request_json)
from trading.models import canonical
from trading.web import Dashboard, make_server

NOW = datetime(2026, 10, 16, 14, 30, tzinfo=timezone.utc)
CREDS = {"ALPACA_PAPER_KEY": "test-market", "ALPACA_PAPER_SECRET_": "test-secret",
         "TRAD_HF_TOKEN": "test-token", "TRAD_HF_MODEL": "test/model", "TRAD_AI_PROVIDER": "huggingface"}


def market_transport(url, **kwargs):
    if url.endswith("/v2/clock"):
        return {"timestamp": NOW.isoformat(), "is_open": True, "next_open": (NOW+timedelta(days=1)).isoformat()}
    symbol = parse_qs(urlsplit(url).query)["symbols"][0]
    return {"bars": {symbol: [{"t": (NOW-timedelta(minutes=i+1)).isoformat(), "c": 100-i/100}
                              for i in range(30)]}}


def hf_transport(url, **kwargs):
    p = kwargs["payload"]
    request = AgentRequest(p["response_format"]["json_schema"]["name"].removeprefix("trading_"),
                           p["messages"][0]["content"], p["messages"][1]["content"],
                           canonical(p["response_format"]["json_schema"]["schema"]))
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": OfflineMockProvider().complete(request, 1)}}]}


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, CREDS, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_market_endpoints_and_read_only_clock(self):
        calls = []
        def transport(url, **kw):
            calls.append((url, kw))
            return market_transport(url, **kw)
        result = AlpacaMarket(transport).fetch(NOW)
        self.assertEqual([a["status"] for a in result["assets"]], ["fresh"]*3)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0][0], "https://paper-api.alpaca.markets/v2/clock")
        self.assertNotIn("payload", calls[0][1])
        for url, options in calls[1:]:
            self.assertEqual(urlsplit(url).hostname, "data.alpaca.markets")
            self.assertEqual(options["headers"]["APCA-API-KEY-ID"], "test-market")
        self.assertIn("feed=iex", calls[1][0])
        self.assertIn("/crypto/us/bars", calls[3][0])
        self.assertNotIn("test-secret", json.dumps(result))

    def test_closed_session_keeps_old_prices_out_of_analysis(self):
        def transport(url, **kw):
            if url.endswith('/v2/clock'):
                return {'timestamp': NOW.isoformat(), 'is_open': False,
                        'next_open': (NOW+timedelta(days=3)).isoformat()}
            data = market_transport(url, **kw)
            symbol = parse_qs(urlsplit(url).query)['symbols'][0]
            if symbol != 'BTC/USD':
                for bar in data['bars'][symbol]:
                    bar['t'] = (datetime.fromisoformat(bar['t'])-timedelta(hours=4)).isoformat()
            return data
        market = AlpacaMarket(transport).fetch(NOW)
        self.assertEqual([a['status'] for a in market['assets']], ['closed', 'closed', 'fresh'])
        self.assertEqual({s['symbol'] for s in market_packet(market, NOW)['sources']}, {'BTC/USD'})

    def test_clock_failure_or_stale_clock_does_not_hide_stale_data(self):
        for clock in [None, {'timestamp': (NOW-timedelta(hours=1)).isoformat(),
                            'is_open': False, 'next_open': (NOW+timedelta(days=1)).isoformat()}]:
            def transport(url, **kw):
                if url.endswith('/v2/clock'):
                    if clock is None:
                        raise ConnectedError('Horloge indisponible')
                    return clock
                return market_transport(url, **kw)
            market = AlpacaMarket(transport).fetch(NOW+timedelta(minutes=4))
            self.assertEqual([a['status'] for a in market['assets']], ['stale']*3)

    def test_missing_credentials_does_not_request(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConnectedError):
            AlpacaMarket(lambda *a, **k: self.fail("Unexpected request")).fetch(NOW)

    def test_closed_stocks_excluded_but_not_hidden(self):
        result = AlpacaMarket(market_transport).fetch(NOW)
        for a in result["assets"][:2]: a["status"] = "stale"
        report = analyze(result, HuggingFaceProvider("test", "test/model", hf_transport), NOW)
        self.assertEqual(report["symbols_analyzed"], ["BTC/USD"])
        self.assertEqual(report["symbols_excluded"], ["AAPL", "MSFT"])
        self.assertEqual(report["execution"], "disabled")

    def test_all_stale_no_ai_call(self):
        result = AlpacaMarket(market_transport).fetch(NOW+timedelta(hours=1))
        with self.assertRaises(ConnectedError):
            analyze(result, HuggingFaceProvider("test", "test/model", lambda *a, **kw: self.fail()), NOW+timedelta(hours=1))

    def test_no_future_duplicate_negative_or_nan_bars(self):
        for bars in [[{"t": (NOW+timedelta(seconds=1)).isoformat(), "c": 100}],
                     [{"t": NOW.isoformat(), "c": "NaN"}],
                     [{"t": NOW.isoformat(), "c": -1}],
                     [{"t": NOW.isoformat(), "c": 100}]*2]:
            with self.subTest(bars=bars), self.assertRaises(ConnectedError):
                normalize_bars(bars, NOW)

    def test_actual_source_timestamp_not_replaced_with_retrieval_time(self):
        m = AlpacaMarket(market_transport).fetch(NOW)
        p = market_packet(m, NOW)
        self.assertEqual(p["data_kind"], "market")
        self.assertEqual(p["sources"][0]["asof"], (NOW-timedelta(minutes=1)).isoformat())
        self.assertEqual(p["sources"][1]["values"]["samples"], 9)

    def test_market_data_cannot_enter_synthetic_simulation(self):
        p = market_packet(AlpacaMarket(market_transport).fetch(NOW), NOW)
        with self.assertRaises(ValidationError): validate_packet(p, NOW)
        with self.assertRaises(ValueError):
            simulate_snapshot(AdvisoryPipeline(data_kind="market"), p, object(), NOW)
        result = AdvisoryPipeline().evaluate(p, NOW)
        self.assertEqual(result.code, "invalid_packet")

    def test_partial_source_failure_is_reported(self):
        def partial(url, **kw):
            if "symbols=MSFT" in url: raise ConnectedError("Source indisponible")
            return market_transport(url, **kw)
        m = AlpacaMarket(partial).fetch(NOW)
        self.assertEqual(m["assets"][1]["status"], "error")
        p = market_packet(m, NOW)
        self.assertNotIn("MSFT", [s["symbol"] for s in p["sources"]])


class HFTests(unittest.TestCase):
    def test_three_roles_structured_output_no_tools_and_bounded_tokens(self):
        calls=[]
        def transport(url, **kw):
            calls.append((url, kw))
            return hf_transport(url, **kw)
        provider=HuggingFaceProvider("test-token", "test/model", transport)
        result=AdvisoryPipeline(provider).evaluate(sample_packet(), datetime.fromisoformat(sample_packet()["asof"]))
        self.assertEqual(result.status, "proposed")
        self.assertEqual(len(calls), 3)
        for url, kw in calls:
            self.assertEqual(url, "https://router.huggingface.co/v1/chat/completions")
            self.assertEqual(kw["headers"]["Authorization"], "Bearer test-token")
            p=kw["payload"]
            self.assertTrue(p["response_format"]["json_schema"]["strict"])
            self.assertEqual(p["max_tokens"],1200)
            self.assertNotIn("tools",p)
            self.assertFalse(p["stream"])
        self.assertNotIn("test-token",canonical(result.records))

    def test_truncation_tool_call_refusal_invalid_json_fail_closed(self):
        for choice in [{"finish_reason":"length","message":{"content":"{}"}},
                       {"finish_reason":"stop","message":{"tool_calls":[{}],"content":"{}"}},
                       {"finish_reason":"stop","message":{"refusal":"no","content":"{}"}},
                       {"finish_reason":"stop","message":{"content":"not json"}}]:
            provider=HuggingFaceProvider("test","test/model",lambda *a, **kw:{"choices":[choice]})
            r=AdvisoryPipeline(provider).evaluate(sample_packet(),datetime.fromisoformat(sample_packet()["asof"]))
            self.assertEqual(r.action,"abstain")
            self.assertEqual(r.calls,1)

    def test_upstream_error_body_never_reaches_user(self):
        with patch("trading.connected.build_opener") as opener:
            opener.return_value.open.side_effect=HTTPError("https://example.invalid",401,"sensitive-token",{},None)
            with self.assertRaises(ConnectedError) as context:
                request_json("https://router.huggingface.co/v1/chat/completions")
            self.assertNotIn("sensitive-token",str(context.exception))
            self.assertIn("401",str(context.exception))
            self.assertEqual(opener.return_value.open.call_count,1)

    def test_no_redirects_or_unapproved_origins(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None))
        for url in ["http://data.alpaca.markets/test", "https://data.alpaca.markets.evil.invalid/", "https://evil.invalid"]:
            with self.assertRaises(ConnectedError): request_json(url)

    def test_age_rechecked_after_inference(self):
        with patch.dict(os.environ, CREDS, clear=True): m=AlpacaMarket(market_transport).fetch(NOW)
        class Clock:
            values=iter([NOW,NOW+timedelta(minutes=4)])
            @classmethod
            def now(cls,tz): return next(cls.values)
        with patch("trading.connected.datetime",Clock), self.assertRaises(ConnectedError):
            analyze(m,HuggingFaceProvider("test","test/model",hf_transport))


class OllamaTests(unittest.TestCase):
    def test_local_three_roles_schema_and_limits_without_secret(self):
        calls = []
        def transport(path, **kw):
            calls.append((path, kw))
            p = kw['payload']
            role = json.loads(p['messages'][1]['content'])['previous_outputs']
            name = ('analyst', 'critic', 'synthesizer')[len(role)]
            request = AgentRequest(name, p['messages'][0]['content'],
                                   p['messages'][1]['content'], canonical(p['format']))
            return {'done': True, 'done_reason': 'stop', 'message': {
                'content': OfflineMockProvider().complete(request, 1)}}
        with patch.dict(os.environ, {}, clear=True):
            provider = OllamaProvider(transport=transport)
            m = {'assets': []}
            with patch.dict(os.environ, CREDS, clear=True):
                m = AlpacaMarket(market_transport).fetch(NOW)
            result = analyze(m, provider, NOW)
        self.assertEqual(result['result']['status'], 'proposed')
        self.assertEqual(result['execution'], 'disabled')
        self.assertEqual(len(calls), 3)
        for path, kw in calls:
            self.assertEqual(path, '/api/chat')
            self.assertEqual(kw['payload']['model'], 'qwen2.5:1.5b')
            self.assertEqual(kw['payload']['options']['num_predict'], 512)
            self.assertFalse(kw['payload']['stream'])
            self.assertNotIn('headers', kw)
            self.assertFalse(kw['payload']['format']['additionalProperties'])

    def test_local_truncation_and_tool_calls_abstain(self):
        for data in [{'done': False}, {'done': True, 'done_reason': 'length', 'message': {'content': '{}'}},
                     {'done': True, 'done_reason': 'stop', 'message': {'content': '{}', 'tool_calls': [{}]}}]:
            provider = OllamaProvider(transport=lambda *a, **kw: data)
            r = AdvisoryPipeline(provider).evaluate(sample_packet(), datetime.fromisoformat(sample_packet()['asof']))
            self.assertEqual(r.action, 'abstain')
            self.assertEqual(r.calls, 1)

    def test_readiness_needs_local_model_not_hf_key(self):
        with patch.dict(os.environ, {}, clear=True), patch('trading.connected.local_json', return_value={
                'models': [{'name': 'qwen2.5:1.5b'}]}):
            status = configuration_status()
            self.assertTrue(status['ai_ready'])
            self.assertEqual(status['ai_provider'], 'ollama')
        with patch.dict(os.environ, {}, clear=True), patch('trading.connected.local_json', return_value={'models': []}):
            self.assertFalse(configuration_status()['ai_ready'])

    def test_local_route_allowlist(self):
        for route in ['/api/pull', '/api/create', 'https://evil.invalid']:
            with self.assertRaises(ConnectedError): local_json(route)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,CREDS,clear=True);self.environment.start();self.addCleanup(self.environment.stop)
        class Feed:
            def fetch(self): return AlpacaMarket(market_transport).fetch(NOW)
        self.calls=0
        def advisor(market):
            self.calls+=1
            return analyze(market,HuggingFaceProvider("test","test/model",hf_transport),NOW)
        self.dashboard=Dashboard(Feed(),advisor)
        self.server=make_server(0,self.dashboard)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)

    def get(self,path):
        with urlopen(self.base+path,timeout=5) as response: return response.read().decode()

    def test_page_market_and_assets_never_trigger_ai(self):
        page=self.get("/")
        self.assertIn("HUGGING FACE",page)
        self.assertNotIn("__CSRF_TOKEN__",page)
        for path in ["/api/market","/api/health","/app.js","/style.css"]:
            self.assertNotIn("test-secret",self.get(path))
        self.assertEqual(self.calls,0)

    def test_explicit_post_full_advisory_then_cooldown(self):
        headers={"Origin":self.base,"X-CSRF-Token":self.dashboard.token}
        with urlopen(Request(self.base+"/api/analyze",method="POST",headers=headers),timeout=5) as response:
            result=json.load(response)
        self.assertEqual(result["analysis"]["execution"],"disabled")
        self.assertEqual(result["analysis"]["result"]["calls"],3)
        with self.assertRaises(HTTPError) as context:
            urlopen(Request(self.base+"/api/analyze",method="POST",headers=headers),timeout=5)
        self.assertEqual(context.exception.code,422)
        self.assertEqual(self.calls,1)

    def test_cross_origin_no_token_and_host_rejected(self):
        for headers in [{},{"Origin":"https://evil.invalid","X-CSRF-Token":self.dashboard.token},
                        {"Origin":self.base,"X-CSRF-Token":self.dashboard.token,"Host":"evil.invalid"}]:
            with self.assertRaises(HTTPError) as context:
                urlopen(Request(self.base+"/api/analyze",method="POST",headers=headers),timeout=5)
            self.assertEqual(context.exception.code,403)
        self.assertEqual(self.calls,0)


if __name__ == "__main__": unittest.main()
