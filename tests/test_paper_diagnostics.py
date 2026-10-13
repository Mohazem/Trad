"""Synthetic errors only: no credentials, network, or persistent experiment state."""
import contextlib
import io
import json
from email.message import Message
from pathlib import Path
import runpy
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from tests.test_paper_once import FakeBroker, NOW, fixture, order
from trading.paper_once import AttemptJournal, OneShotPaper
from trading.paper_transport import MAX_ERROR_BYTES, PAPER, PaperError, ProtectedTransport
from trading.paper_diagnostics import MAX_EXCERPT_CHARS, read_error_body, safe_response_detail

RID = '0123456789abcdef0123456789abcdef'
SECRET = 'SYNTHETIC-SECRET-DO-NOT-LOG'
KEY = 'SYNTHETIC-KEY-DO-NOT-LOG'


def response_error(body=None, *, status=403, request_ids=(RID,), content_type='application/json'):
    headers = Message()
    for value in request_ids:
        headers['X-Request-ID'] = value
    headers['Content-Type'] = content_type
    headers['Authorization'] = SECRET
    headers['Set-Cookie'] = SECRET
    headers['APCA-API-KEY-ID'] = KEY
    body = body if body is not None else json.dumps({
        'code': 40310000, 'message': SECRET, 'request_body': {'private': KEY}}).encode()
    return urllib.error.HTTPError(PAPER + '/v2/account?private=' + SECRET,
                                  status, SECRET, headers, io.BytesIO(body))


class DiagnosticTests(unittest.TestCase):
    def request_error(self, error, *, key=KEY):
        transport = ProtectedTransport()
        output = io.StringIO()
        with patch.object(transport, 'preflight', return_value='http://proxy:8080'), \
                patch('os.environ', {'ALPACA_PAPER_KEY': key, 'ALPACA_PAPER_SECRET_': SECRET}), \
                patch('urllib.request.build_opener') as build, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            build.return_value.open.side_effect = error
            with self.assertRaises(PaperError) as caught:
                transport.request(PAPER, 'GET', '/v2/account')
            build.return_value.open.assert_called_once()
        self.assertEqual(output.getvalue(), '')
        # The sanitized error does not retain the original HTTPError as context.
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        exposed = json.dumps(vars(caught.exception)) + str(caught.exception) + repr(caught.exception)
        self.assertNotIn(SECRET, exposed)
        self.assertNotIn(KEY, exposed)
        self.assertNotIn('request_body', exposed)
        self.assertNotIn('Authorization', exposed)
        self.assertTrue(error.closed)
        return caught.exception

    def test_only_allowlisted_projection_survives_sensitive_response(self):
        error = self.request_error(response_error())
        detail = error.safe_details()
        self.assertEqual(detail.pop('response_detail'), {
            'format': 'json', 'state': 'captured', 'truncated': False, 'source': 'unverified',
            'recognized_fragments': [], 'redacted_excerpt': '[REDACTED]'})
        self.assertEqual(detail, {'code': 'http_error', 'http_status': 403,
                                  'request_id': RID, 'provider_error_code': 40310000})

    def test_documented_422_code_is_preserved_without_message(self):
        error = self.request_error(response_error(b'{"code":42210000,"message":"private"}', status=422))
        self.assertEqual(error.provider_error_code, 42210000)

    def test_request_id_format_is_strict_and_duplicates_are_omitted(self):
        for ids in ((SECRET,), ('https://example.invalid/' + SECRET,), (RID + '\r\n' + SECRET,),
                    (' ' + RID,), (RID + ' ',), ('a' * 31,), ('a' * 33,),
                    ('00000000-0000-0000-0000-000000000000',), (RID, RID), ()):
            with self.subTest(ids=ids):
                self.assertIsNone(self.request_error(response_error(request_ids=ids)).request_id)

    def test_reflected_known_credential_shaped_like_request_id_is_omitted(self):
        error = self.request_error(response_error(), key=RID)
        self.assertIsNone(error.request_id)
        self.assertNotIn(RID, json.dumps(error.safe_details()))

    def test_malformed_unknown_duplicate_or_noninteger_provider_codes_are_omitted(self):
        for body in (b'not json', b'[]', b'{"code":40319999}', b'{"code":true}',
                     b'{"code":"40310000"}', b'{"code":40310000.0}', b'{"code":42210000}',
                     b'{"code":40310000,"code":40310000}', b'{"code":NaN}', b'{"code":{"secret":"x"}}'):
            with self.subTest(body=body):
                self.assertIsNone(self.request_error(response_error(body)).provider_error_code)

    def test_unsupported_and_encoded_bodies_are_not_read(self):
        for encoded in (False, True):
            error = response_error(content_type='application/octet-stream' if not encoded else 'application/json')
            if encoded:
                error.headers['Content-Encoding'] = 'gzip'
            error.read = Mock(side_effect=AssertionError('body must not be read'))
            self.assertIsNone(self.request_error(error).provider_error_code)
            error.read.assert_not_called()

    def test_error_read_is_bounded_inside_original_deadline(self):
        error = response_error()
        def read(size):
            self.assertEqual(size, MAX_ERROR_BYTES + 1)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)
            return b'x' * size
        error.read = Mock(side_effect=read)
        result = self.request_error(error)
        self.assertIsNone(result.provider_error_code)
        self.assertEqual(result.status, 403)
        error.read.assert_called_once_with(MAX_ERROR_BYTES + 1)

    def test_read_timeout_keeps_safe_status_and_request_id_without_retry(self):
        error = response_error()
        error.read = Mock(side_effect=PaperError('request_timeout'))
        result = self.request_error(error)
        detail = result.safe_details()
        self.assertEqual(detail.pop('response_detail')['state'], 'read_failed')
        self.assertEqual(detail, {'code': 'http_error', 'http_status': 403, 'request_id': RID})
        error.read.assert_called_once()

    def test_status_and_metadata_validation_never_echo_arbitrary_values(self):
        for status in (SECRET, True, 0, 600):
            result = PaperError('http_error', status, request_id=SECRET, provider_error_code=SECRET)
            self.assertEqual(result.safe_details(), {'code': 'http_error', 'http_status': None})

    def test_cli_emits_only_safe_diagnostic_fields(self):
        error = PaperError('http_error', 403, request_id=RID, provider_error_code=40310000)
        output = io.StringIO()
        with patch('sys.argv', ['paper_cli', 'market-packet', '--confirm-secret-destinations']), \
                patch('trading.paper_once.PaperBroker.candidate_packet', side_effect=error), \
                patch('trading.paper_transport.ProtectedTransport.request') as request, \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
            runpy.run_module('trading.paper_cli', run_name='__main__')
        self.assertEqual(exit_info.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'stopped', **error.safe_details()})
        request.assert_not_called()

    def test_json_preserves_reason_but_drops_accounts_people_urls_and_tokens(self):
        private = ['SYNTHETIC-ACCOUNT-123', 'Jane Example', 'jane@example.invalid',
                   'https://example.invalid/private', 'eyJ_SYNTHETIC_BEARER', 'opaque-value-abcdef']
        payload = {'code': 40310000, 'message': 'insufficient buying power for ' + ', '.join(private),
                   'account_id': private[0], 'name': private[1], 'cookie': SECRET,
                   'request_headers': {'Authorization': 'Bearer ' + private[4]}}
        error = self.request_error(response_error(json.dumps(payload).encode()))
        detail = error.response_detail
        self.assertEqual(detail['recognized_fragments'], ['insufficient buying power'])
        self.assertEqual(detail['redacted_excerpt'], '[REDACTED] insufficient buying power [REDACTED]')
        self.assertEqual(detail['source'], 'unverified')
        for value in private:
            self.assertNotIn(value, json.dumps(vars(error)))

    def test_text_proxy_reason_is_a_hint_not_verified_origin(self):
        error = self.request_error(response_error(
            b'Blocked by network policy: https://private.invalid/path?token=hidden\n'
            b'Authorization: Bearer token-secret\nCookie: session=private', content_type='text/plain'))
        detail = error.response_detail
        self.assertEqual(detail['recognized_fragments'], ['blocked by network policy'])
        self.assertEqual(detail['source_hint'], 'proxy_or_network_policy_text')
        self.assertEqual(detail['source'], 'unverified')
        for value in ('private.invalid', 'token-secret', 'session=private'):
            self.assertNotIn(value, json.dumps(vars(error)))

    def test_html_ignores_attributes_scripts_comments_and_form_fields(self):
        body = (b'<html><!-- insufficient funds --><script>invalid credentials</script>'
                b'<style>unauthorized</style><form><textarea>order not found</textarea></form>'
                b'<h1 title="minimum order size">Proxy access denied</h1>'
                b'<a href="https://private.invalid/token">Jane Example</a></html>')
        error = self.request_error(response_error(body, content_type='text/html; charset=utf-8'))
        self.assertEqual(error.response_detail['recognized_fragments'], ['access denied', 'proxy access denied'])
        self.assertEqual(error.response_detail['source_hint'], 'proxy_or_network_policy_text')
        self.assertNotIn('Jane Example', json.dumps(vars(error)))

    def test_text_credentials_and_urls_are_not_used_as_reason_evidence(self):
        body = (b'Authorization: Bearer forbidden\nCookie: insufficient funds\n'
                b'APCA-API-SECRET-KEY: access denied\n'
                b'https://example.invalid/unauthorized\n'
                b'Unknown opaque text')
        detail = self.request_error(response_error(body, content_type='text/plain')).response_detail
        self.assertEqual(detail['recognized_fragments'], [])

    def test_known_placeholder_equal_to_reason_is_redacted(self):
        error = self.request_error(response_error(b'{"message":"insufficient funds"}'), key='insufficient funds')
        self.assertEqual(error.response_detail['recognized_fragments'], [])

    def test_oversized_text_is_truncated_once_without_suffix_or_secret(self):
        error = response_error(b'permission denied ' + b'x' * MAX_ERROR_BYTES + b' order not found',
                               content_type='text/plain')
        real_read = error.read
        error.read = Mock(wraps=real_read)
        detail = self.request_error(error).response_detail
        error.read.assert_called_once_with(MAX_ERROR_BYTES + 1)
        self.assertTrue(detail['truncated'])
        self.assertEqual(detail['state'], 'truncated')
        self.assertEqual(detail['recognized_fragments'], ['permission denied'])
        self.assertLessEqual(len(detail['redacted_excerpt']), MAX_EXCERPT_CHARS)

    def test_truncated_structured_bodies_are_not_scanned_as_free_text(self):
        for media, body in (('application/json', b'{"message":"insufficient funds '),
                            ('text/html', b'<script>insufficient funds ')):
            detail = self.request_error(response_error(body + b'x' * MAX_ERROR_BYTES,
                                                       content_type=media)).response_detail
            self.assertTrue(detail['truncated'])
            self.assertEqual(detail['recognized_fragments'], [])

    def test_malformed_json_and_invalid_utf8_are_described_without_echo(self):
        for body, state in ((b'{"message":"insufficient funds",}', 'malformed_json'),
                            (b'{"message":"forbidden","message":"secret"}', 'malformed_json'),
                            (b'\xffinsufficient funds', 'invalid_utf8'),
                            (b'[]', 'unsupported_json_shape'), (b'', 'empty')):
            detail = self.request_error(response_error(body)).response_detail
            self.assertEqual(detail['state'], state)
            self.assertEqual(detail['recognized_fragments'], [])

    def test_nested_json_and_unknown_fields_do_not_become_diagnostic_text(self):
        body = b'{"account":"insufficient funds","error":{"token":"forbidden"}}'
        detail = self.request_error(response_error(body)).response_detail
        self.assertEqual(detail['recognized_fragments'], [])

    def test_problem_json_and_unknown_provider_code_still_preserve_reason(self):
        error = self.request_error(response_error(b'{"code":999999,"detail":"order not found"}',
                                                  status=404, content_type='application/problem+json'))
        self.assertIsNone(error.provider_error_code)
        self.assertEqual(error.response_detail['recognized_fragments'], ['order not found'])

    def test_duplicate_metadata_is_not_trusted(self):
        error = response_error()
        error.headers['Content-Type'] = 'text/html'
        error.read = Mock(side_effect=AssertionError('must not read ambiguous content'))
        detail = self.request_error(error).response_detail
        self.assertEqual(detail['state'], 'unsupported_media_type')
        error.read.assert_not_called()

    def test_arbitrary_detail_cannot_inject_excerpt_source_or_fields(self):
        raw = {'format': 'text', 'state': 'captured', 'truncated': SECRET,
               'recognized_fragments': [SECRET, 'permission denied'],
               'redacted_excerpt': SECRET, 'source': SECRET, 'account': SECRET}
        detail = PaperError('http_error', 403, response_detail=raw).safe_details()['response_detail']
        self.assertNotIn(SECRET, json.dumps(detail))
        self.assertEqual(detail['source'], 'unverified')
        self.assertFalse(detail['truncated'])
        self.assertIsNone(safe_response_detail({'format': SECRET, 'state': 'captured'}))

    def test_repeated_reason_fragments_have_a_fixed_output_bound(self):
        body = ('insufficient funds ' * 200).encode()
        detail = self.request_error(response_error(body, content_type='text/plain')).response_detail
        self.assertEqual(detail['recognized_fragments'], ['insufficient funds'])
        self.assertLessEqual(len(detail['redacted_excerpt']), MAX_EXCERPT_CHARS)


class ReconciliationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spec = fixture()[0]
        self.broker = FakeBroker()
        self.journal = AttemptJournal(Path(self.tmp.name) / 'synthetic.sqlite3', create=True)
        self.runner = OneShotPaper(self.broker, self.journal, mode='manual_one_shot', clock=lambda: NOW)

    def test_submission_error_survives_reconciliation_without_second_post(self):
        detail, _ = read_error_body(response_error(b'{"message":"insufficient funds"}'))
        failure = PaperError('http_error', 403, request_id=RID, provider_error_code=40310000, response_detail=detail)
        self.broker.transport.post_result = failure
        self.broker.lookups = [None, PaperError('http_error', 404)]
        result = self.runner.submit_approved(self.spec, approved_digest=self.spec.approval_digest())
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['submission_error'], failure.safe_details())
        self.assertEqual(result['reconciliation_error']['http_status'], 404)
        self.runner.submit_approved(self.spec, approved_digest=self.spec.approval_digest())
        self.assertEqual(sum(call[1] == 'POST' for call in self.broker.transport.calls), 1)
        self.assertFalse(self.journal.claim(self.spec.approval_digest()))

    def test_preflight_error_preserves_safe_metadata_without_post(self):
        detail, _ = read_error_body(response_error(b'{"message":"permission denied"}'))
        failure = PaperError('http_error', 403, request_id=RID, provider_error_code=40310000, response_detail=detail)
        self.broker.lookups = [failure]
        with self.assertRaises(PaperError) as caught:
            self.runner.submit_approved(self.spec, approved_digest=self.spec.approval_digest())
        self.assertEqual(caught.exception.safe_details(), {**failure.safe_details(), 'code': 'preflight_stopped_no_post'})
        self.assertEqual(self.broker.transport.calls, [])

    def test_cancel_error_survives_followup_without_second_delete(self):
        failure = PaperError('http_error', 403, request_id=RID, provider_error_code=40310000)
        self.journal.claim(self.spec.approval_digest())
        self.broker.lookups = [order(status='new', filled='0'), order()]
        self.broker.transport.cancel_result = failure
        result = self.runner.reconcile(self.spec, cancel_open=True, approved_digest=self.spec.approval_digest())
        self.assertEqual(result['state'], 'filled')
        self.assertEqual(result['cancellation_error'], failure.safe_details())
        self.assertEqual([call[1] for call in self.broker.transport.calls], ['DELETE'])


if __name__ == '__main__':
    unittest.main()
