import http.client
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import server

HOST = {'name': 'local', 'label': 'Local', 'local': True}
SOURCE = {'id': '019aaaaa-0000-7000-8000-000000000001', 'agent': 'codex', 'home': '.codex',
          'phase': 'idle', 'cwd': '/tmp/project', 'title': '작업', 'account': '기본', 'updatedAt': 100}
PROFILE = {'id': 'codex:.codex', 'agent': 'codex', 'home': '.codex', 'label': '기본', 'available': True}
OTHER = {'id': 'codex:.codex-alt', 'agent': 'codex', 'home': '.codex-alt', 'label': '다른 계정', 'available': True}
REF = {'host': 'local', 'agent': 'codex', 'home': '.codex', 'id': SOURCE['id']}


def setUpModule():
    # A board under test never reads this user's bot sources (nor, through them, a bot ledger).
    import pathlib
    import shutil
    import tempfile
    from unittest import mock
    folder = tempfile.mkdtemp(prefix="sh-config-")
    patcher = mock.patch.object(server, "CONFIG_DIR", pathlib.Path(folder))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(shutil.rmtree, folder, True)


class FakeTerminals:
    max_terminals = 4
    def __init__(self):
        self.calls = []
        self.records = []
    def list(self):
        return [dict(row) for row in self.records]
    @contextmanager
    def reserve(self, key):
        if sum(bool(row.get('alive')) for row in self.records) >= self.max_terminals:
            raise RuntimeError('열린 터미널이 가득 찼습니다.')
        yield
    def create(self, key, argv, cwd, env, metadata):
        self.calls.append(('create', key))
        record = {'id': 'abcdef'[len(self.records)] * 32, 'key': key, **metadata, 'alive': True}
        self.records.append(record)
        return dict(record)
    def close_terminal(self, tid):
        record = next((row for row in self.records if row['id'] == tid), None)
        if record is None:
            raise KeyError(tid)
        self.calls.append(('close', tid))
        record['alive'] = False
        record['closed'] = True
        return {'ok': True}
    def write(self, tid, text):
        self.calls.append(('write', tid, text))
    def resize(self, tid, cols, rows):
        self.calls.append(('resize', tid, cols, rows))
    def read(self, tid, after, wait, epoch=None):
        return {'after': 0, 'data': '', 'alive': True, 'reset': False, 'epoch': 'test'}


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rows = [dict(SOURCE)]
        self.calls = []
        def runner(host, args, timeout):
            self.calls.append(args)
            if args[0] == 'snapshot':
                return {'codex': self.rows, 'claude': [], 'errors': []}
            return {'messages': [{'role': 'user', 'text': '작업을 이어줘'}]}
        self.board = server.Board([HOST], 60, runner)
        self.board.poll(HOST)
        self.terms = FakeTerminals()
        self.flow = server.Workflow(self.board, self.terms, self.temp.name)
        self.flow.profiles = lambda: [PROFILE, OTHER]
        self.which = patch('server.shutil.which', return_value='/usr/bin/tmux')
        self.which.start()
    def tearDown(self):
        self.which.stop()
        self.temp.cleanup()
    def plan(self, profile=PROFILE):
        return self.flow.plan({'source': REF, 'target': {'host': 'local', 'agent': 'codex', 'account': profile['id']}})
    def test_resume_and_handoff_are_distinct(self):
        self.assertEqual(self.plan()['mode'], 'attach')
        plan = self.plan(OTHER)
        self.assertTrue(plan['allowed'])
        self.assertEqual(plan['mode'], 'handoff')
    def test_browsers_cannot_invent_source_or_account(self):
        with self.assertRaises(ValueError):
            self.flow.plan({'source': {**REF, 'id': 'not-in-snapshot'}, 'target': {}})
        plan = self.plan({**PROFILE, 'id': 'codex:../../tmp'})
        self.assertFalse(plan['allowed'])
    def test_working_can_attach_but_cannot_transfer(self):
        self.rows[0]['phase'] = 'working'
        self.board.poll(HOST)
        self.assertTrue(self.plan()['allowed'])
        self.assertFalse(self.plan(OTHER)['allowed'])
    def test_offline_source_cannot_launch(self):
        self.board.state['local']['ok'] = False
        self.assertFalse(self.plan()['allowed'])
    def test_environment_and_default_environment_configs_do_not_cross(self):
        self.rows[0]['cwd'] = '/Users/u/Isolated/repo'
        self.board.poll(HOST)
        with patch('launch.source_environment', return_value='isolated'):
            self.assertFalse(self.plan(OTHER)['allowed'])
    def test_state_change_after_plan_prevents_transfer(self):
        plan = self.plan(OTHER)
        self.rows[0]['phase'] = 'working'
        with self.assertRaises(ValueError):
            self.flow.launch(plan['id'], 'request-0001')
        self.assertFalse(self.terms.calls)
    def test_launch_is_idempotent(self):
        plan = self.plan()
        with patch('launch.build_launch', return_value={'argv': ['/bin/cat'], 'cwd': '/tmp', 'env': {}}):
            first = self.flow.launch(plan['id'], 'request-0001')
            second = self.flow.launch(plan['id'], 'request-0001')
        self.assertEqual(first, second)
        self.assertEqual(len(self.terms.calls), 1)
    def test_expired_plan_cannot_launch(self):
        plan = self.plan()
        plan['expiresAt'] = 0
        with self.assertRaises(ValueError):
            self.flow.launch(plan['id'], 'request-0001')
    def test_full_capacity_blocks_local_handoff_before_native_preparation(self):
        plan = self.plan(OTHER)
        self.terms.max_terminals = 1
        self.terms.records = [{'id': 'a'*32, 'key': 'other-route', 'alive': True}]
        with patch('launch.build_launch') as build, patch('managed_launch.bind') as bind:
            with self.assertRaisesRegex(RuntimeError, '가득'):
                self.flow.launch(plan['id'], 'full-handoff-0001')
            build.assert_not_called()
            bind.assert_not_called()
        self.assertEqual(self.terms.calls, [])

    def test_prepared_handoff_connection_failure_records_target_and_does_not_repeat(self):
        plan = self.plan(OTHER)
        native = {**REF, 'home': OTHER['home'], 'id': SOURCE['id'][:-1]+'2', 'cwd': '/tmp'}
        spec = {'argv': ['/bin/cat'], 'cwd': '/tmp', 'env': {}, 'nativeSource': native}
        with patch('launch.build_launch', return_value=spec), \
             patch('managed_launch.bind', return_value=spec) as bind, \
             patch.object(self.terms, 'create', side_effect=RuntimeError('fixture connection failure')):
            for _ in range(2):
                with self.assertRaises(server.LaunchFailure) as result:
                    self.flow.launch(plan['id'], 'failed-handoff-0001')
                self.assertEqual(result.exception.recovery['nativeSource'], native)
                self.assertEqual(result.exception.recovery['host'], 'local')
            self.assertEqual(bind.call_count, 1)
    def test_failed_host_requires_manual_refresh(self):
        def broken(*args):
            raise RuntimeError('offline')
        self.board.runner = broken
        self.board.poll(HOST)
        self.board.attempted['local'] = 0
        self.assertFalse(self.board.request_refresh())
        self.assertTrue(self.board.request_refresh(force=True))
    def test_snapshot_survives_restart_as_stale(self):
        self.board.state_dir = Path(self.temp.name)
        self.board.poll(HOST)
        restored = server.Board([HOST], 60, state_dir=self.temp.name)
        self.assertFalse(restored.state['local']['ok'])
        self.assertEqual(restored.state['local']['data']['codex'][0]['id'], SOURCE['id'])
        self.assertEqual((Path(self.temp.name) / 'snapshot.json').stat().st_mode & 0o777, 0o600)


class ConcurrencyTest(unittest.TestCase):
    """Remote work runs outside the workflow lock: a repeat of a request waits for it, one
    route launches once at a time, and a slow route never holds up another."""
    setUp = WorkflowTest.setUp
    tearDown = WorkflowTest.tearDown
    plan = WorkflowTest.plan

    def blocking_build(self, block):
        started, release = threading.Event(), threading.Event()
        built = []
        def build(source, profile, messages, state_dir):
            built.append(profile['id'])
            if block(profile):
                started.set()
                release.wait(5)
            return {'argv': ['/bin/cat'], 'cwd': '/tmp', 'env': {}}
        return build, started, release, built

    def test_a_request_repeated_while_it_runs_waits_for_it_and_launches_once(self):
        plan = self.plan()
        build, started, release, built = self.blocking_build(lambda profile: True)
        results = []
        with patch('launch.build_launch', side_effect=build):
            first = threading.Thread(target=lambda: results.append(self.flow.launch(plan['id'], 'request-0001')))
            first.start()
            self.assertTrue(started.wait(5))
            second = threading.Thread(target=lambda: results.append(self.flow.launch(plan['id'], 'request-0001')))
            second.start()
            second.join(0.2)
            self.assertTrue(second.is_alive(), 'the repeat waits for the request in flight')
            release.set()
            first.join(5); second.join(5)
        self.assertEqual(built, [PROFILE['id']])
        self.assertEqual(len(self.terms.calls), 1)
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.flow.inflight, {})

    def test_a_slow_launch_never_holds_up_another_route(self):
        attach, handoff = self.plan(), self.plan(OTHER)
        build, started, release, built = self.blocking_build(lambda profile: profile['id'] == OTHER['id'])
        with patch('launch.build_launch', side_effect=build):
            slow = threading.Thread(target=self.flow.launch, args=(handoff['id'], 'request-slow'))
            slow.start()
            self.assertTrue(started.wait(5))
            began = time.monotonic()
            quick = self.flow.launch(attach['id'], 'request-quick')
            self.assertLess(time.monotonic() - began, 2)
            self.assertTrue(slow.is_alive(), 'the other route is still preparing')
            release.set()
            slow.join(5)
        self.assertEqual(quick['mode'], 'attach')
        self.assertEqual(len(self.terms.calls), 2)
        self.assertEqual(self.flow.route_locks, {})

    def test_the_same_route_launched_twice_at_once_opens_one_terminal(self):
        first_plan, second_plan = self.plan(), self.plan()
        build, started, release, built = self.blocking_build(lambda profile: True)
        results = {}
        with patch('launch.build_launch', side_effect=build):
            first = threading.Thread(target=lambda: results.update(a=self.flow.launch(first_plan['id'], 'request-a')))
            first.start()
            self.assertTrue(started.wait(5))
            second = threading.Thread(target=lambda: results.update(b=self.flow.launch(second_plan['id'], 'request-b')))
            second.start()
            second.join(0.2)
            self.assertTrue(second.is_alive(), 'the same route waits for the launch in progress')
            release.set()
            first.join(5); second.join(5)
        self.assertEqual(len(built), 1)
        self.assertEqual(len(self.terms.calls), 1)
        self.assertTrue(results['b'].get('reused'))
        self.assertEqual(results['a']['terminal']['id'], results['b']['terminal']['id'])

    def test_a_waiting_repeat_runs_the_request_itself_when_the_first_ends_unanswered(self):
        plan = self.plan()
        started, release = threading.Event(), threading.Event()
        calls = []
        def build(source, profile, messages, state_dir):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                release.wait(5)
                raise ValueError('fixture: refused before anything ran')
            return {'argv': ['/bin/cat'], 'cwd': '/tmp', 'env': {}}
        outcomes = {}
        def run(name):
            try:
                outcomes[name] = self.flow.launch(plan['id'], 'request-0003')
            except ValueError as exc:
                outcomes[name] = exc
        with patch('launch.build_launch', side_effect=build):
            first = threading.Thread(target=run, args=('first',))
            first.start()
            self.assertTrue(started.wait(5))
            second = threading.Thread(target=run, args=('second',))
            second.start()
            second.join(0.2)
            release.set()
            first.join(5); second.join(5)
        self.assertIsInstance(outcomes['first'], ValueError)
        self.assertEqual(outcomes['second']['mode'], 'attach')
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.terms.calls), 1)

    def test_a_plan_that_expired_while_its_route_was_busy_is_not_launched(self):
        plan = self.plan()
        saved = self.flow.plans[plan['id']][0]
        with patch.object(self.flow, '_route', side_effect=self._expire_then_route(saved)):
            with self.assertRaisesRegex(ValueError, '만료'):
                self.flow.launch(plan['id'], 'request-0004')
        self.assertFalse(self.terms.calls)

    @staticmethod
    def _expire_then_route(saved):
        @contextmanager
        def route(key):
            saved['expiresAt'] = 0
            yield
        return route

    def test_a_refused_request_can_be_tried_again(self):
        plan = self.plan(OTHER)
        self.rows[0]['phase'] = 'working'
        with self.assertRaises(ValueError):
            self.flow.launch(plan['id'], 'request-0001')
        self.assertEqual(self.flow.inflight, {})
        self.rows[0]['phase'] = 'idle'
        with patch('launch.build_launch', return_value={'argv': ['/bin/cat'], 'cwd': '/tmp', 'env': {}}):
            self.assertEqual(self.flow.launch(plan['id'], 'request-0001')['mode'], 'handoff')


class SecurityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.terms = FakeTerminals()
        self.board = server.Board([HOST], 60, runner=lambda *args: {'codex': [], 'claude': []})
        self.httpd = server.make_server('127.0.0.1', 0, self.board, 'secret-test-only', terminals=self.terms, state_dir=self.temp.name)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        res, data = self.request('POST', '/login', {'token': 'secret-test-only'})
        self.cookie = res.getheader('Set-Cookie').split(';')[0]
        self.csrf = json.loads(data)['csrfToken']
    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.temp.cleanup()
    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        merged = {'Content-Type': 'application/json', **(headers or {})}
        conn.request(method, path, json.dumps(data) if data is not None else None, merged)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response, body
    def auth(self):
        return {'Cookie': self.cookie, 'X-CSRF-Token': self.csrf}
    def test_cross_origin_login_is_rejected(self):
        res, _ = self.request('POST', '/login', {'token': 'secret-test-only'}, {'Origin': 'https://evil.example'})
        self.assertEqual(res.status, 403)
    def test_csrf_required_for_input(self):
        res, _ = self.request('POST', '/api/terminal/' + 'a'*32 + '/input', {'text': 'x', 'requestId': 'input-0001'}, {'Cookie': self.cookie})
        self.assertEqual(res.status, 403)
        self.assertFalse(self.terms.calls)
    def test_input_ack_is_idempotent(self):
        path = '/api/terminal/' + 'a'*32 + '/input'
        payload = {'text': 'hello', 'requestId': 'input-0001'}
        for _ in range(2):
            res, _ = self.request('POST', path, payload, self.auth())
            self.assertEqual(res.status, 200)
        self.assertEqual(self.terms.calls, [('write', 'a'*32, 'hello')])
        res, _ = self.request('POST', path, {**payload, 'text': 'changed'}, self.auth())
        self.assertEqual(res.status, 400)

    def restart_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = server.make_server('127.0.0.1', 0, self.board, 'secret-test-only',
                                       terminals=self.terms, state_dir=self.temp.name)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]
        res, data = self.request('POST', '/login', {'token': 'secret-test-only'})
        self.cookie = res.getheader('Set-Cookie').split(';')[0]
        self.csrf = json.loads(data)['csrfToken']

    def test_accepted_input_is_not_written_again_after_server_restart(self):
        path = '/api/terminal/' + 'a'*32 + '/input'
        payload = {'text': '한글\r', 'requestId': 'restart-input-0001'}
        self.assertEqual(self.request('POST', path, payload, self.auth())[0].status, 200)
        self.restart_server()
        self.assertEqual(self.request('POST', path, payload, self.auth())[0].status, 200)
        self.assertEqual(self.terms.calls, [('write', 'a'*32, '한글\r')])

    def test_partial_input_failure_stays_unknown_after_retry_and_restart(self):
        path = '/api/terminal/' + 'a'*32 + '/input'
        payload = {'text': 'x'*2048, 'requestId': 'partial-input-0001'}
        received = []
        def partial(tid, text):
            received.append(text[:1024])
            raise RuntimeError('fixture partial write timeout')
        self.terms.write = partial
        self.assertEqual(self.request('POST', path, payload, self.auth())[0].status, 409)
        self.restart_server()
        self.assertEqual(self.request('POST', path, payload, self.auth())[0].status, 409)
        self.assertEqual(len(''.join(received)), 1024)

    def test_terminal_list_prunes_persisted_closed_receipts_and_releases_capacity(self):
        self.httpd.terminal_inputs.max_records = 1
        path = '/api/terminal/' + 'a'*32 + '/input'
        payload = {'text': 'fixture', 'requestId': 'quota-input-0001'}
        self.assertEqual(self.request('POST', path, payload, self.auth())[0].status, 200)
        self.terms.records = [{'id': 'a'*32, 'alive': False, 'closed': True}]
        res, body = self.request('GET', '/api/terminals', headers=self.auth())
        self.assertEqual(res.status, 200)
        self.assertNotIn('inputCleanupPending', json.loads(body))
        path = '/api/terminal/' + 'b'*32 + '/input'
        self.assertEqual(self.request('POST', path, payload, self.auth())[0].status, 200)

    def test_terminal_list_carries_the_version_its_events_announce(self):
        res, body = self.request('GET', '/api/terminals', headers=self.auth())
        self.assertEqual(res.status, 200)
        self.assertEqual(json.loads(body)['version'], 0)
        # What a TerminalManager reports (create, close, attach, detach) is a new version.
        self.httpd.events.terminals_changed()
        res, body = self.request('GET', '/api/terminals', headers=self.auth())
        self.assertEqual(json.loads(body)['version'], 1)

    def test_close_succeeds_and_reports_failed_receipt_cleanup_for_refresh_retry(self):
        self.terms.records = [{'id': 'a'*32, 'alive': True, 'closed': False}]
        with patch.object(self.httpd.terminal_inputs, 'prune_all_closed', side_effect=RuntimeError('fixture disk error')):
            res, body = self.request('POST', '/api/terminal/'+'a'*32+'/close', {}, self.auth())
            self.assertEqual(res.status, 200)
            self.assertTrue(json.loads(body)['inputCleanupPending'])
            res, body = self.request('GET', '/api/terminals', headers=self.auth())
            self.assertEqual(res.status, 200)
            self.assertTrue(json.loads(body)['inputCleanupPending'])
        self.assertFalse(self.terms.records[0]['alive'])
        res, body = self.request('GET', '/api/terminals', headers=self.auth())
        self.assertNotIn('inputCleanupPending', json.loads(body))

    def test_chat_rejection_before_dispatch_is_explicit(self):
        payload = {'source': REF, 'requestId': 'message-rejected-0001', 'text': 'draft'}
        res, body = self.request('POST', '/api/chat/send', payload, {'Cookie': self.cookie})
        self.assertEqual(res.status, 403)
        self.assertEqual(json.loads(body)['dispatchState'], 'not_started')
        self.assertEqual(json.loads(body)['code'], 'csrf_expired')
        with patch.object(self.httpd.chat, 'rpc') as rpc:
            res, body = self.request('POST', '/api/chat/send', payload, self.auth())
            self.assertEqual(res.status, 400)
            self.assertEqual(json.loads(body)['dispatchState'], 'not_started')
            rpc.assert_not_called()
        with patch.object(self.httpd.chat, '_prepare_send', side_effect=RuntimeError('offline')):
            res, body = self.request('POST', '/api/chat/send', payload, self.auth())
            self.assertEqual(res.status, 409)
            self.assertEqual(json.loads(body)['dispatchState'], 'not_started')

    def test_chat_failure_after_dispatch_is_never_labelled_not_started(self):
        payload = {'source': REF, 'requestId': 'message-unknown-0001', 'text': 'draft'}
        prepared = ({**SOURCE, 'host': 'local'}, HOST, payload['requestId'], 'draft', [], [])
        with patch.object(self.httpd.chat, '_prepare_send', return_value=prepared), \
             patch.object(self.httpd.chat, 'rpc', side_effect=ValueError('fixture late response')):
            res, body = self.request('POST', '/api/chat/send', payload, self.auth())
            self.assertEqual(res.status, 400)
            self.assertNotIn('dispatchState', json.loads(body))
    def test_close_requires_auth_csrf_and_same_origin(self):
        self.terms.records = [{'id': 'a'*32, 'key': 'fixture', 'alive': True}]
        path = '/api/terminal/' + 'a'*32 + '/close'
        res, _ = self.request('POST', path, {})
        self.assertEqual(res.status, 401)
        res, _ = self.request('POST', path, {}, {'Cookie': self.cookie})
        self.assertEqual(res.status, 403)
        res, _ = self.request('POST', path, {}, {**self.auth(), 'Origin': 'https://evil.example'})
        self.assertEqual(res.status, 403)
        self.assertFalse(self.terms.calls)
        res, body = self.request('POST', path, {}, self.auth())
        self.assertEqual(res.status, 200)
        self.assertEqual(json.loads(body), {'ok': True})
        self.assertFalse(self.terms.list()[0]['alive'])
        res, _ = self.request('POST', path, {}, self.auth())
        self.assertEqual(res.status, 200)
        res, _ = self.request('POST', '/api/terminal/' + 'b'*32 + '/close', {}, self.auth())
        self.assertEqual(res.status, 400)
        res, _ = self.request('POST', '/api/terminal/not-an-id/close', {}, self.auth())
        self.assertEqual(res.status, 404)
    def test_logout_revokes_cookie(self):
        res, _ = self.request('POST', '/logout', {}, self.auth())
        self.assertEqual(res.status, 200)
        res, _ = self.request('GET', '/api/snapshot', headers=self.auth())
        self.assertEqual(res.status, 401)
    def test_oversized_and_array_body_rejected(self):
        for value in ([], {'text': 'x'*70000}):
            res, _ = self.request('POST', '/api/plan', value, self.auth())
            self.assertEqual(res.status, 400)
    def test_an_oversized_body_already_sent_gets_its_answer_not_a_reset(self):
        # The whole body is in the server's socket before it answers: closing with it unread
        # would reset the connection and drop the answer.
        import socket
        body = json.dumps({'text': 'x' * 70000}).encode()
        head = ('POST /api/plan HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n'
                'Cookie: %s\r\nX-CSRF-Token: %s\r\nContent-Length: %d\r\n\r\n' % (self.cookie, self.csrf, len(body)))
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as client:
            client.sendall(head.encode() + body)
            time.sleep(0.3)
            answer = b''
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                answer += chunk
        self.assertTrue(answer.startswith(b'HTTP/1.0 400') or answer.startswith(b'HTTP/1.1 400'), answer[:40])
    def test_terminal_stream_requires_auth(self):
        res, _ = self.request('GET', '/api/terminal/'+'a'*32+'/events')
        self.assertEqual(res.status, 401)
    def test_loopback_enforced_at_factory(self):
        with self.assertRaises(ValueError):
            server.make_server('0.0.0.0', 0, self.board, 'x')
    def tailscale_headers(self):
        return {'Host': 'remote.example.ts.net:8447', 'X-Forwarded-Proto': 'https',
                'Tailscale-User-Login': 'owner@example.invalid'}
    def enable_tailscale(self):
        self.httpd.tailscale_user = 'owner@example.invalid'
        self.httpd.workflow.profiles = lambda: []
    def test_tailscale_auth_is_opt_in_and_returns_csrf(self):
        headers = self.tailscale_headers()
        res, _ = self.request('GET', '/api/capabilities', headers=headers)
        self.assertEqual(res.status, 401)
        self.enable_tailscale()
        res, body = self.request('GET', '/api/capabilities', headers=headers)
        self.assertEqual(res.status, 200)
        data = json.loads(body)
        self.assertEqual(data['authMode'], 'tailscale')
        self.assertEqual(data['csrfToken'], self.csrf)
        self.assertIsNone(res.getheader('Set-Cookie'))
        res, body = self.request('GET', '/api/capabilities', headers={'Cookie': self.cookie})
        self.assertEqual(res.status, 200)
        self.assertEqual(json.loads(body)['authMode'], 'token')
    def test_tailscale_auth_rejects_missing_wrong_and_insecure_headers(self):
        self.enable_tailscale()
        valid = self.tailscale_headers()
        cases = [{k: v for k, v in valid.items() if k != 'Tailscale-User-Login'},
                 {**valid, 'Tailscale-User-Login': 'other@example.invalid'},
                 {**valid, 'Tailscale-User-Login': 'owner@example.invalid '},
                 {**valid, 'X-Forwarded-Proto': 'http'},
                 {k: v for k, v in valid.items() if k != 'X-Forwarded-Proto'},
                 {**valid, 'Host': '127.0.0.1:' + str(self.port)}]
        for headers in cases:
            with self.subTest(headers=headers):
                res, _ = self.request('GET', '/api/snapshot', headers=headers)
                self.assertEqual(res.status, 401)
        res, _ = self.request('GET', '/api/snapshot', headers={**cases[1], 'Cookie': self.cookie})
        self.assertEqual(res.status, 200)
    def test_tailscale_auth_rejects_duplicate_proxy_headers(self):
        self.enable_tailscale()
        for duplicate in ('Host', 'X-Forwarded-Proto', 'Tailscale-User-Login'):
            with self.subTest(duplicate=duplicate):
                conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
                conn.putrequest('GET', '/api/snapshot', skip_host=True)
                for key, value in self.tailscale_headers().items():
                    conn.putheader(key, value)
                    if key == duplicate:
                        conn.putheader(key, value)
                conn.endheaders()
                response = conn.getresponse()
                response.read()
                conn.close()
                self.assertEqual(response.status, 401)
    def test_tailscale_auth_requires_loopback_peer(self):
        self.enable_tailscale()
        handler = server.Handler.__new__(server.Handler)
        handler.server = self.httpd
        handler.headers = http.client.HTTPMessage()
        for key, value in self.tailscale_headers().items():
            handler.headers[key] = value
        handler.client_address = ('100.64.0.1', 12345)
        self.assertFalse(handler.tailscale_authorized())
        handler.client_address = ('127.0.0.1', 12345)
        self.assertTrue(handler.tailscale_authorized())
    def test_tailscale_input_still_requires_csrf_and_same_origin(self):
        self.enable_tailscale()
        headers = self.tailscale_headers()
        path = '/api/terminal/' + 'a'*32 + '/input'
        payload = {'text': '한글 입력', 'requestId': 'input-ts-0001'}
        res, _ = self.request('POST', path, payload, headers)
        self.assertEqual(res.status, 403)
        headers['X-CSRF-Token'] = self.csrf
        res, _ = self.request('POST', path, payload, {**headers, 'Origin': 'https://evil.example'})
        self.assertEqual(res.status, 403)
        res, _ = self.request('POST', path, payload, {**headers, 'Origin': 'https://remote.example.ts.net:8447'})
        self.assertEqual(res.status, 200)
        self.assertEqual(self.terms.calls, [('write', 'a'*32, '한글 입력')])

if __name__ == '__main__':
    unittest.main()

class ContinuityTest(unittest.TestCase):
    setUp = WorkflowTest.setUp
    tearDown = WorkflowTest.tearDown
    plan = WorkflowTest.plan
    def test_target_extra_fields_do_not_change_identity(self):
        plan = self.flow.plan({'source': REF, 'target': {'host': 'local', 'agent': 'codex', 'account': PROFILE['id'], 'ignored': 'x'}})
        self.assertEqual(set(plan['target']), {'host', 'agent', 'account'})

    def test_live_route_reconnects_without_reading_or_new_handoff(self):
        plan = self.plan(OTHER)
        import hashlib
        key = hashlib.sha256(json.dumps([plan['source'], plan['target']], sort_keys=True).encode()).hexdigest()
        self.terms.list = lambda: [{'id': 'b'*32, 'key': key, 'alive': True}]
        next_plan = self.plan(OTHER)
        self.assertEqual(next_plan['mode'], 'reconnect')
        before = len(self.calls)
        with patch('launch.build_launch') as builder:
            result = self.flow.launch(next_plan['id'], 'reconnect-0001')
        builder.assert_not_called()
        self.assertTrue(result['reused'])
        self.assertEqual(len(self.calls), before)
        self.assertFalse(self.terms.calls)

    def test_dead_route_requires_explicit_new_plan_and_new_key(self):
        plan = self.plan()
        import hashlib
        base = hashlib.sha256(json.dumps([plan['source'], plan['target']], sort_keys=True).encode()).hexdigest()
        self.terms.list = lambda: [{'id': 'b'*32, 'key': base, 'alive': False}]
        with patch('launch.build_launch', return_value={'argv': ['/bin/cat'], 'cwd': '/tmp', 'env': {}}):
            self.flow.launch(plan['id'], 'restart-0001')
        self.assertEqual(self.terms.calls[0][1], base + ':' + plan['id'])

    def test_closed_route_uses_fresh_plan_and_starts_new_terminal(self):
        original_plan = self.plan()
        spec = {'argv': ['/bin/cat'], 'cwd': '/tmp', 'env': {}}
        with patch('launch.build_launch', return_value=spec):
            first = self.flow.launch(original_plan['id'], 'first-open-0001')
            reconnect = self.plan()
            self.assertEqual(reconnect['mode'], 'reconnect')
            self.terms.close_terminal(first['terminal']['id'])
            with self.assertRaises(RuntimeError):
                self.flow.launch(reconnect['id'], 'closed-route-0001')
            fresh = self.plan()
            self.assertEqual(fresh['mode'], 'attach')
            second = self.flow.launch(fresh['id'], 'fresh-open-0001')
            repeated = self.flow.launch(fresh['id'], 'fresh-open-0001')
        self.assertNotEqual(first['terminal']['id'], second['terminal']['id'])
        self.assertEqual(second, repeated)
        self.assertEqual(second['terminal']['key'], first['terminal']['key'] + ':' + fresh['id'])
        self.assertEqual([row['alive'] for row in self.terms.list()], [False, True])
        self.assertEqual(len([call for call in self.terms.calls if call[0] == 'create']), 2)

    def test_fresh_handoff_bypasses_previous_transcript(self):
        self.board.read('local', 'codex', SOURCE['id'], '.codex')
        old = self.board.runner
        def runner(host, args, timeout):
            if args[0] == 'read':
                return {'messages': [{'role': 'user', 'text': '방금 추가한 결정'}]}
            return old(host, args, timeout)
        self.board.runner = runner
        plan = self.plan(OTHER)
        with patch('launch.build_launch', return_value={'argv': ['/bin/cat'], 'cwd': '/tmp', 'env': {}}) as builder:
            self.flow.launch(plan['id'], 'fresh-0001')
        self.assertEqual(builder.call_args[0][2][0]['text'], '방금 추가한 결정')

    def test_private_conversation_survives_offline_and_is_redacted(self):
        self.board.state_dir = Path(self.temp.name)
        self.board.runner = lambda *a: {'messages': [{'role': 'user', 'text': 'name@example.com sk-proj-12345678901234567890'}]}
        self.board.read('local', 'codex', SOURCE['id'], '.codex')
        self.board.flush_reads()
        restored = server.Board([HOST], 60, state_dir=self.temp.name)
        restored.state['local']['paused'] = True
        result = restored.read('local', 'codex', SOURCE['id'], '.codex')
        self.assertTrue(result['stale'])
        self.assertIn('name@example.com', result['messages'][0]['text'], 'an email address is not a secret')
        self.assertNotIn('sk-proj', result['messages'][0]['text'])
        self.assertEqual((Path(self.temp.name) / 'conversations.json').stat().st_mode & 0o777, 0o600)
