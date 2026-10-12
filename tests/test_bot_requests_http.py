"""Bot requests have a separate read surface and cannot inherit direct-session controls."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import test_goals_http


# The same isolated configuration folder as the goal routes' tests.
setUpModule = test_goals_http.setUpModule


class BotRequestsHTTPTest(unittest.TestCase):
    setUpBase = test_goals_http.GoalsHTTPTest.setUp
    tearDown = test_goals_http.GoalsHTTPTest.tearDown
    request = test_goals_http.GoalsHTTPTest.request

    def setUp(self):
        self.setUpBase()
        self.host = {'name': 'fixture-bot-host', 'label': '합성 기기', 'local': True}
        self.ref = {'host': self.host['name'], 'agent': 'codex', 'home': '.codex',
                    'id': '019aaaaa-0000-7000-8000-000000000029'}
        self.board.hosts.append(self.host)
        self.board.state[self.host['name']] = {**self.host, 'ok': True, 'fetchedAt': 100,
                'data': {'codex': [{**self.ref, 'phase': 'idle', 'cwd': '/fixture/work'}], 'claude': []}}
        self.response = {'requests': [{'id': 'fixture-request', 'host': self.host['name'],
                         'botName': '예제 봇', 'nativeRef': self.ref}], 'sources': [], 'fetchedAt': 100}
        self.bots = SimpleNamespace(snapshot=Mock(return_value=self.response))
        self.httpd.bot_requests = self.httpd.workflow.bot_requests = self.bots

    def test_bot_metadata_requires_login_and_refresh_requires_csrf(self):
        response, _ = self.request('GET', '/api/bot-requests', authenticated=False)
        self.assertEqual(response.status, 401)
        response, _ = self.request('POST', '/api/bot-requests/refresh', {}, csrf=False)
        self.assertEqual(response.status, 403)
        self.bots.snapshot.assert_not_called()

    def test_reads_and_explicit_refresh_have_separate_cache_contract(self):
        response, data = self.request('GET', '/api/bot-requests')
        self.assertEqual(response.status, 200)
        self.assertEqual(data, self.response)
        self.bots.snapshot.assert_called_once_with()
        response, _ = self.request('POST', '/api/bot-requests/refresh', {})
        self.assertEqual(response.status, 200)
        self.bots.snapshot.assert_called_with(force=True)

    def test_known_bot_cannot_inherit_direct_send_terminal_or_termination(self):
        requests = [('/api/chat/send', {'source': self.ref, 'requestId': 'bot-block-send-01', 'text': '합성 입력'}),
                    ('/api/plan', {'source': self.ref, 'target': {'host': self.host['name'], 'agent': 'codex', 'account': 'codex:.codex'}}),
                    ('/api/session/action', {'source': self.ref, 'action': 'terminate', 'requestId': 'bot-block-control', 'targetProof': 'aaa.' + 'a'*64})]
        with patch.object(self.httpd.workflow.transfers, 'rpc', side_effect=AssertionError('bot authority bypass')):
            for path, body in requests:
                response, result = self.request('POST', path, body)
                self.assertIn(response.status, (400, 409), result)
                self.assertIn('봇', result['error'])

    def test_other_native_identity_is_not_inferred_to_be_a_bot(self):
        self.assertIsNone(self.httpd.workflow.bot_request({**self.ref, 'id': 'different'}))
        self.assertIsNone(self.httpd.workflow.bot_request({**self.ref, 'home': '.codex-other'}))
        self.assertEqual(self.httpd.workflow.bot_request(self.ref)['id'], 'fixture-request')

    def test_preexisting_bot_terminal_cannot_bypass_direct_controls(self):
        tid = 'a' * 32
        with patch.object(self.httpd.terminals, 'list', return_value=[{'id': tid, 'nativeSource': self.ref}]), \
                patch.object(self.httpd.terminal_inputs, 'submit', side_effect=AssertionError('bot terminal input')):
            for action, body in (('input', {'requestId': 'bot-terminal-input', 'text': 'synthetic'}), ('close', {})):
                response, result = self.request('POST', '/api/terminal/' + tid + '/' + action, body)
                self.assertEqual(response.status, 400)
                self.assertIn('봇', result['error'])


if __name__ == '__main__':
    unittest.main()
