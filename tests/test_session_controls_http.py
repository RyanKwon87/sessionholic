"""HTTP safety and dispatch boundaries, entirely synthetic native transport."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class SessionControlsHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.host = {'name': 'fixture-mini', 'label': '합성 Mini', 'local': True}
        self.source = {'host': self.host['name'], 'agent': 'codex', 'home': '.codex-fixture', 'id': str(uuid.uuid4())}
        self.row = {**self.source, 'cwd': '/fixture/workspace', 'kind': 'cli', 'phase': 'working',
                    'title': '합성 외부 작업', 'startedAt': 100, 'updatedAt': 101}
        self.claude_sid = str(uuid.uuid4())
        self.claude = {'host': self.host['name'], 'agent': 'claude', 'id': 'terminal-fixture', 'home': '.claude'}
        self.claude_row = {**self.claude, 'kind': 'interactive', 'sessionId': self.claude_sid,
                           'cwd': '/fixture/claude', 'phase': 'working', 'pid': 424242, 'title': '합성 Claude'}
        self.board = server.Board([self.host], 60, runner=lambda *args: self.fail('native collector called'), live=False)
        self.board.state[self.host['name']].update(ok=True, fetchedAt=100,
                 data={'codex': [self.row], 'claude': [self.claude_row]})
        terminals = SimpleNamespace(list=lambda: [], close=lambda: None)
        self.httpd = server.make_server('127.0.0.1', 0, self.board, 'fixture-token',
                                       state_dir=self.temporary.name, terminals=terminals)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        response, result = self.request('/login', {'token': 'fixture-token'}, authenticated=False)
        self.cookie = response.getheader('Set-Cookie').split(';', 1)[0]
        self.csrf = result['csrfToken']
        self.proof = 'eyJmaXh0dXJlIjoxfQ.' + 'a' * 64

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.temporary.cleanup()

    def request(self, path, body, authenticated=True, csrf=True, extra=None):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Cookie'] = self.cookie
            if csrf:
                headers['X-CSRF-Token'] = self.csrf
        headers.update(extra or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request('POST', path, body=json.dumps(body), headers=headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response, json.loads(raw)

    def action(self, **changes):
        return {'source': self.source, 'action': 'interrupt', 'targetProof': self.proof,
                'requestId': 'fixture-control-1234', **changes}

    def test_all_controls_require_login_csrf_and_same_origin(self):
        with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            for path in ('/api/session/preview', '/api/session/action', '/api/session/archive'):
                with self.subTest(path=path):
                    response, _ = self.request(path, self.action(), authenticated=False)
                    self.assertEqual(response.status, 401)
                    response, result = self.request(path, self.action(), csrf=False)
                    self.assertEqual((response.status, result['code']), (403, 'csrf_expired'))
                    response, _ = self.request(path, self.action(), extra={'Origin': 'https://other.invalid'})
                    self.assertEqual(response.status, 403)
            rpc.assert_not_called()

    def test_preview_only_sends_trusted_snapshot_identity(self):
        malicious = {**self.source, 'pid': 1, 'cwd': '/other', 'kind': 'app-server', 'sessionId': 'other',
                     'command': 'kill all', 'profile': 'other', 'phase': 'idle'}
        expected = {key: self.row.get(key) for key in ('agent', 'id', 'home', 'cwd', 'kind', 'sessionId', 'phase')}
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value={'capabilities': {}, 'targetProof': self.proof}) as rpc:
            response, result = self.request('/api/session/preview', {'source': malicious, 'pid': 1})
        self.assertEqual(response.status, 200, result)
        rpc.assert_called_once_with(self.host, 'control_preview', {'source': expected}, timeout=8)
        self.assertNotIn('pid', rpc.call_args.args[2]['source'])

    def test_unknown_account_host_or_session_rejected_without_rpc(self):
        variants = [None, {**self.source, 'home': '.codex-other'}, {**self.source, 'host': 'other'},
                    {**self.source, 'id': str(uuid.uuid4())}, {**self.source, 'agent': 'shell'}]
        with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            for source in variants:
                with self.subTest(source=source):
                    response, _ = self.request('/api/session/preview', {'source': source})
                    self.assertEqual(response.status, 400)
            rpc.assert_not_called()

    def test_offline_preview_and_action_never_dispatch(self):
        self.board.state[self.host['name']]['ok'] = False
        with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            for path in ('/api/session/preview', '/api/session/action'):
                response, result = self.request(path, self.action())
                self.assertEqual(response.status, 409, result)
            rpc.assert_not_called()

    def test_malformed_action_proof_and_request_id_rejected_before_rpc(self):
        invalid = [{'action': 'kill-all'}, {'action': 'archive'}, {'requestId': ''}, {'requestId': '../secret'},
                   {'requestId': 123}, {'targetProof': None}, {'targetProof': ''},
                   {'targetProof': 'not-a-proof'}, {'targetProof': 'a' * 4097}]
        with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            for changes in invalid:
                with self.subTest(changes=changes):
                    response, _ = self.request('/api/session/action', self.action(**changes))
                    self.assertEqual(response.status, 400)
            rpc.assert_not_called()

    def test_confirmed_action_dispatches_once_and_refreshes_owner_only(self):
        receipt = {'confirmed': True, 'applied': True, 'status': 'interrupted', 'requestId': 'fixture-control-1234'}
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value=receipt) as rpc, \
                patch.object(self.board, 'request_refresh') as refresh, patch.object(self.httpd.chat, '_forget_state') as forget:
            response, result = self.request('/api/session/action', self.action())
        self.assertEqual((response.status, result), (200, receipt))
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(rpc.call_args.args[1], 'control_execute')
        self.assertEqual(rpc.call_args.args[2]['targetProof'], self.proof)
        refresh.assert_called_once_with(self.host['name'], force=False)
        self.assertEqual(forget.call_count, 1)
        self.assertEqual(forget.call_args.args[0]['id'], self.source['id'])

    def test_unconfirmed_unknown_and_failed_transport_never_retry_or_refresh(self):
        variants = [{'confirmed': False, 'applied': None, 'status': 'unconfirmed'}, {}, None, TimeoutError()]
        for value in variants:
            with self.subTest(value=value), patch.object(self.httpd.workflow.transfers, 'rpc') as rpc, \
                    patch.object(self.board, 'request_refresh') as refresh, patch.object(self.httpd.chat, '_forget_state') as forget:
                if isinstance(value, Exception):
                    rpc.side_effect = value
                else:
                    rpc.return_value = value
                response, result = self.request('/api/session/action', self.action())
                self.assertEqual(rpc.call_count, 1)
                self.assertIn(response.status, (200, 409, 503))
                refresh.assert_not_called()
                forget.assert_not_called()

    def test_archive_is_board_only_and_works_while_host_offline(self):
        self.board.state[self.host['name']]['ok'] = False
        with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc, patch.object(self.board, 'request_refresh') as refresh:
            response, result = self.request('/api/session/archive', {'source': self.source, 'archived': True})
            self.assertEqual(response.status, 200, result)
            self.assertEqual(result['changed'], 1)
            self.assertFalse(result['executionChanged'])
            response, result = self.request('/api/session/archive', {'source': self.source, 'archived': False})
            self.assertEqual(response.status, 200, result)
            self.assertFalse(result['archived'])
            rpc.assert_not_called()
            refresh.assert_not_called()
        self.assertEqual(self.board.state[self.host['name']]['data']['codex'][0]['phase'], 'working')

    def test_archive_rejects_non_boolean(self):
        with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            response, _ = self.request('/api/session/archive', {'source': self.source, 'archived': 'true'})
        self.assertEqual(response.status, 400)
        rpc.assert_not_called()

    def test_external_claude_routes_native_session_binding_without_browser_pid(self):
        browser = {**self.claude, 'pid': 999999, 'sessionId': 'wrong', 'cwd': '/wrong'}
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value={'capabilities': {}}) as rpc:
            response, result = self.request('/api/session/preview', {'source': browser})
        self.assertEqual(response.status, 200, result)
        source = rpc.call_args.args[2]['source']
        self.assertEqual(source['sessionId'], self.claude_sid)
        self.assertEqual(source['cwd'], '/fixture/claude')
        self.assertNotIn('pid', source)

    def test_claude_missing_terminal_id_uses_exact_native_session_id(self):
        other = {**self.claude_row, 'id': '', 'sessionId': str(uuid.uuid4()), 'cwd': '/fixture/other'}
        self.board.state[self.host['name']]['data']['claude'] = [other, {**self.claude_row, 'id': ''}]
        selector = {**self.claude, 'id': self.claude_sid}
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value={'capabilities': {}}) as rpc:
            response, result = self.request('/api/session/preview', {'source': selector})
        self.assertEqual(response.status, 200, result)
        native = rpc.call_args.args[2]['source']
        self.assertEqual(native['id'], self.claude_sid)
        self.assertEqual(native['sessionId'], self.claude_sid)
        self.assertEqual(native['cwd'], self.claude_row['cwd'])
        with patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            response, _ = self.request('/api/session/preview', {'source': {**self.claude, 'id': ''}})
            self.assertEqual(response.status, 400)
            rpc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
