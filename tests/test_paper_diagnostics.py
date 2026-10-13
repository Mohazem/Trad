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
        self.assertEqual(error.safe_details(), {'code': 'http_error', 'http_status': 403,
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

    def test_non_json_and_encoded_bodies_are_not_read(self):
        for encoded in (False, True):
            error = response_error(content_type='text/html' if not encoded else 'application/json')
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
        self.assertEqual(result.safe_details(), {'code': 'http_error', 'http_status': 403, 'request_id': RID})
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


class ReconciliationDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spec = fixture()[0]
        self.broker = FakeBroker()
        self.journal = AttemptJournal(Path(self.tmp.name) / 'synthetic.sqlite3', create=True)
        self.runner = OneShotPaper(self.broker, self.journal, mode='manual_one_shot', clock=lambda: NOW)

    def test_submission_error_survives_reconciliation_without_second_post(self):
        failure = PaperError('http_error', 403, request_id=RID, provider_error_code=40310000)
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
        self.broker.lookups = [PaperError('http_error', 403, request_id=RID, provider_error_code=40310000)]
        with self.assertRaises(PaperError) as caught:
            self.runner.submit_approved(self.spec, approved_digest=self.spec.approval_digest())
        self.assertEqual(caught.exception.safe_details(), {'code': 'preflight_stopped_no_post',
                         'http_status': 403, 'request_id': RID, 'provider_error_code': 40310000})
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
