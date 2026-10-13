"""Synthetic persistence, exact identity and authenticated board-only title edits."""
import copy
import http.client
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goals import GoalError
from session_labels import SessionLabels, SessionSeen, reference
import server


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


class SessionLabelsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.labels = SessionLabels(self.directory)
        self.source = {'host': 'example-mini', 'agent': 'codex', 'id': str(uuid.uuid4()),
                       'home': '.codex', 'title': 'Native title', 'cwd': '/example/workspace'}

    def snapshot(self, *sources):
        hosts = {}
        for source in sources or [self.source]:
            host = hosts.setdefault(source['host'], {'name': source['host'], 'data': {'codex': [], 'claude': []}})
            host['data'][source['agent']].append(dict(source))
        return {'version': 3, 'hosts': list(hosts.values())}

    def row(self, snapshot):
        return snapshot['hosts'][0]['data']['codex'][0]

    def test_persistent_display_alias_keeps_native_fields_and_input_untouched(self):
        snapshot = self.snapshot()
        before = copy.deepcopy(snapshot)
        result = self.labels.set(self.source, '  Board review title  ')
        self.assertEqual(result['titleAlias'], 'Board review title')
        self.assertEqual(result['originalTitle'], 'Native title')
        self.assertEqual(result['revision'], 1)
        reloaded = SessionLabels(self.directory).decorate(snapshot)
        self.assertEqual(self.row(reloaded)['displayTitle'], 'Board review title')
        self.assertEqual(self.row(reloaded)['title'], 'Native title')
        self.assertEqual(self.row(reloaded)['cwd'], self.source['cwd'])
        self.assertEqual(snapshot, before)
        self.assertEqual(reloaded['sessionLabels'], {'ok': True, 'revision': 1})

    def test_reset_uses_current_native_title_and_is_idempotent(self):
        self.labels.set(self.source, 'Board title')
        updated = {**self.source, 'title': 'New native title'}
        result = self.labels.set(updated, None)
        self.assertIsNone(result['titleAlias'])
        self.assertEqual(result['displayTitle'], 'New native title')
        before = self.labels.path.read_bytes()
        self.assertEqual(self.labels.set(updated, '   ')['revision'], result['revision'])
        self.assertEqual(self.labels.path.read_bytes(), before)
        self.assertEqual(json.loads(before)['labels'], {})

    def test_aliases_are_separate_for_host_home_agent_and_claude_transcript(self):
        claude = {**self.source, 'agent': 'claude', 'home': '.claude', 'sessionId': str(uuid.uuid4())}
        sources = [self.source, {**self.source, 'home': '.codex-other'},
                   {**self.source, 'host': 'example-air'}, claude,
                   {**claude, 'sessionId': str(uuid.uuid4())}]
        for index, source in enumerate(sources):
            self.labels.set(source, 'Alias ' + str(index))
        for index, source in enumerate(sources):
            snapshot = self.labels.decorate(self.snapshot(source))
            row = snapshot['hosts'][0]['data'][source['agent']][0]
            self.assertEqual(row['titleAlias'], 'Alias ' + str(index))
        self.assertEqual(len(json.loads(self.labels.path.read_text())['labels']), len(sources))

    def test_private_permissions_and_no_conversation_or_cwd_storage(self):
        self.labels.set({**self.source, 'snippet': 'private conversation', 'messages': ['private message']}, 'Display')
        for path in (self.labels.path, self.labels.lock_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        raw = self.labels.path.read_text()
        self.assertNotIn('private conversation', raw)
        self.assertNotIn('private message', raw)
        self.assertNotIn('/example/workspace', raw)
        self.assertNotIn('Native title', raw)

    def test_oversized_invalid_and_control_titles_do_not_write(self):
        for title in ('x' * 161, 42, [], {}, 'line\nbreak', 'tab\tname', 'bad\x7f', 'bad\ud800', 'bad\u202e'):
            with self.subTest(title=repr(title)), self.assertRaises(GoalError):
                self.labels.set(self.source, title)
        self.assertFalse(self.labels.path.exists())
        self.assertEqual(self.labels.set(self.source, '이름 👩‍💻')['titleAlias'], '이름 👩‍💻')

    def test_corruption_is_never_overwritten_or_exposed_and_snapshot_survives(self):
        self.labels.set(self.source, 'Saved')
        raw = b'PRIVATE_CORRUPT_CONTENT'
        self.labels.path.write_bytes(raw)
        with self.assertRaises(GoalError) as caught:
            self.labels.set(self.source, 'Replacement')
        self.assertEqual(caught.exception.status, 503)
        snapshot = self.labels.decorate(self.snapshot())
        self.assertFalse(snapshot['sessionLabels']['ok'])
        self.assertEqual(self.row(snapshot)['title'], 'Native title')
        self.assertNotIn(raw.decode(), json.dumps(snapshot))
        self.assertEqual(self.labels.path.read_bytes(), raw)

    def test_failed_atomic_replace_retains_previous_value_and_no_temp_file(self):
        self.labels.set(self.source, 'Previous')
        before = self.labels.path.read_bytes()
        with patch('goals.os.replace', side_effect=OSError('synthetic disk failure')), self.assertRaises(GoalError):
            self.labels.set(self.source, 'Failed')
        self.assertEqual(self.labels.path.read_bytes(), before)
        self.assertEqual(self.row(self.labels.decorate(self.snapshot()))['titleAlias'], 'Previous')
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_symlink_and_hardlink_storage_never_written(self):
        target = self.directory / 'unrelated.json'
        target.write_text('private unrelated')
        target.chmod(0o600)
        for link in ('symlink', 'hardlink'):
            if link == 'symlink':
                self.labels.path.symlink_to(target)
            else:
                os.link(target, self.labels.path)
            with self.subTest(link=link), self.assertRaises(GoalError):
                self.labels.set(self.source, 'Changed')
            self.assertEqual(target.read_text(), 'private unrelated')
            self.labels.path.unlink()

    def test_two_store_instances_preserve_concurrent_independent_aliases(self):
        other = SessionLabels(self.directory)
        second = {**self.source, 'home': '.codex-other'}
        barrier, failures = threading.Barrier(2), []
        def save(store, source, value):
            try:
                barrier.wait(2)
                store.set(source, value)
            except Exception as exc:
                failures.append(type(exc).__name__)
        threads = [threading.Thread(target=save, args=(store, source, value)) for store, source, value in
                   ((self.labels, self.source, 'First'), (other, second, 'Second'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(failures, [])
        rows = self.labels.decorate(self.snapshot(self.source, second))['hosts'][0]['data']['codex']
        self.assertEqual([row['titleAlias'] for row in rows], ['First', 'Second'])

    def test_normalized_claude_reference_without_id_uses_exact_transcript(self):
        source = {'host': 'example', 'agent': 'claude', 'id': '', 'kind': 'interactive', 'sessionId': str(uuid.uuid4())}
        self.assertEqual(reference(source), {'host': 'example', 'agent': 'claude', 'home': '.claude', 'id': source['sessionId']})


class SessionSeenTest(unittest.TestCase):
    """When each session was last looked at: shared, never moving back, never lighting up
    what existed before the board kept it."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = [5000]
        self.seen = SessionSeen(Path(self.temp.name), clock=lambda: self.now[0])
        self.source = {'host': 'example-mini', 'agent': 'codex', 'id': str(uuid.uuid4()), 'home': '.codex',
                       'title': 'Native title', 'updatedAt': 4000}

    def snapshot(self, *rows):
        return {'version': 1, 'hosts': [{'name': 'example-mini', 'data': {'codex': [dict(row) for row in rows or [self.source]], 'claude': []}}]}

    def test_the_first_read_starts_the_record_then_marks_are_shared_and_kept(self):
        saved = Path(self.temp.name) / 'session-seen.json'
        self.assertFalse(saved.exists())
        before = self.seen.decorate(self.snapshot())
        # Started by the first read (a fresh board lights nothing up, yet marks can begin).
        self.assertEqual(before['sessionSeen'], {'ok': True, 'since': 5000})
        self.assertIsNone(before['hosts'][0]['data']['codex'][0]['seenAt'])
        self.assertTrue(saved.exists())
        self.now[0] = 6000
        self.assertEqual(self.seen.decorate(self.snapshot())['sessionSeen']['since'], 5000, 'kept, not restarted')
        self.assertEqual(self.seen.mark(self.source, 4000), ({'seenAt': 4000}, True))
        other = {**self.source, 'id': str(uuid.uuid4())}
        after = self.seen.decorate(self.snapshot(self.source, other))
        rows = after['hosts'][0]['data']['codex']
        self.assertEqual([row['seenAt'] for row in rows], [4000, None])
        # Never moves back (and writes nothing); a later look moves it on.
        self.assertEqual(self.seen.mark(self.source, 3000), ({'seenAt': 4000}, False))
        self.assertEqual(self.seen.mark(self.source, 4100), ({'seenAt': 4100}, True))
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(json.loads(saved.read_text())['since'], 5000)
        self.assertNotIn('Native title', saved.read_text(), 'no title is kept')

    def test_a_damaged_store_leaves_the_list_as_it_is_and_refuses_a_mark(self):
        self.seen.mark(self.source, 4000)
        (Path(self.temp.name) / 'session-seen.json').write_text('{"version": 9}')
        with patch('sys.stderr', new_callable=__import__('io').StringIO) as log:
            decorated = self.seen.decorate(self.snapshot())
            self.seen.decorate(self.snapshot())
        self.assertEqual(log.getvalue().count('읽음 기록을 읽지 못했습니다'), 1, 'said once')
        self.assertFalse(decorated['sessionSeen']['ok'])
        self.assertNotIn('seenAt', decorated['hosts'][0]['data']['codex'][0])
        with self.assertRaises(GoalError):
            self.seen.mark(self.source, 4200)
        with self.assertRaises(GoalError):
            SessionSeen(Path(self.temp.name)).mark(self.source, -1)


class SessionLabelsHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.host = {'name': 'example-mini', 'label': 'Example Mini', 'local': True}
        self.source = {'host': self.host['name'], 'agent': 'codex', 'home': '.codex', 'id': str(uuid.uuid4())}
        self.native = {**self.source, 'title': 'Native title', 'cwd': '/example/workspace', 'phase': 'idle'}
        self.board = server.Board([self.host], 60, runner=lambda *args: self.fail('native collection'), live=False)
        self.board.state[self.host['name']].update(ok=False, data={'codex': [self.native], 'claude': []})
        self.httpd = server.make_server('127.0.0.1', 0, self.board, 'example-token', state_dir=self.temp.name,
                                       terminals=SimpleNamespace(list=lambda: [], close=lambda: None))
        self.httpd.workflow.bot_requests = SimpleNamespace(snapshot=lambda: {'requests': []})
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        response, result = self.request('POST', '/login', {'token': 'example-token'}, auth=False)
        self.cookie = response.getheader('Set-Cookie').split(';', 1)[0]
        self.csrf = result['csrfToken']

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(2)
        self.httpd.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, auth=True, csrf=True, extra=None):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Cookie'] = self.cookie
            if csrf:
                headers['X-CSRF-Token'] = self.csrf
        headers.update(extra or {})
        client = http.client.HTTPConnection('127.0.0.1', self.httpd.server_address[1], timeout=5)
        client.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = client.getresponse()
        result = json.loads(response.read())
        client.close()
        return response, result

    def save(self, title='Board name', source=None, **options):
        return self.request('POST', '/api/session/label', {'source': source or self.source, 'title': title}, **options)

    def test_a_listed_session_is_marked_read_no_later_than_the_board_lists_it(self):
        self.native['updatedAt'] = 4000
        response, result = self.request('POST', '/api/session/seen', {'source': self.source, 'updatedAt': 9999})
        self.assertEqual((response.status, result), (200, {'seenAt': 4000}))
        response, snapshot = self.request('GET', '/api/snapshot')
        self.assertEqual(snapshot['hosts'][0]['data']['codex'][0]['seenAt'], 4000)
        self.assertEqual(type(snapshot['sessionSeen']['since']), int)
        for payload in ({'source': self.source}, {'source': self.source, 'updatedAt': '4000'},
                        {'source': {**self.source, 'id': str(uuid.uuid4())}, 'updatedAt': 1}):
            self.assertEqual(self.request('POST', '/api/session/seen', payload)[0].status, 400)
        self.assertEqual(self.request('POST', '/api/session/seen', {'source': self.source, 'updatedAt': 1}, csrf=False)[0].status, 403)

    def test_auth_csrf_and_origin_are_required(self):
        self.assertEqual(self.save(auth=False)[0].status, 401)
        self.assertEqual(self.save(csrf=False)[0].status, 403)
        self.assertEqual(self.save(extra={'Origin': 'https://example.invalid'})[0].status, 403)
        self.assertFalse(self.httpd.session_labels.path.exists())

    def test_offline_alias_and_reset_are_board_only_with_snapshot_event(self):
        changes = []
        self.board.add_listener(changes.append)
        before = copy.deepcopy(self.board.state)
        with patch.object(self.board, 'request_refresh') as refresh, patch.object(self.httpd.workflow.transfers, 'rpc') as rpc:
            response, result = self.save(source={**self.source, 'title': 'Untrusted original', 'cwd': '/other', 'pid': 1})
            self.assertEqual(response.status, 200, result)
            self.assertEqual(result['originalTitle'], 'Native title')
            self.assertEqual(result['source'], self.source)
            self.assertEqual(changes, [1])
            snapshot = self.request('GET', '/api/snapshot')[1]
            self.assertEqual(snapshot['sessionLabels']['revision'], result['revision'])
            row = snapshot['hosts'][0]['data']['codex'][0]
            self.assertEqual((row['title'], row['displayTitle']), ('Native title', 'Board name'))
            self.assertEqual(self.save(None)[1]['displayTitle'], 'Native title')
            rpc.assert_not_called()
            refresh.assert_not_called()
        self.assertEqual(self.board.state, before)

    def test_same_id_other_home_alias_is_independent_for_both_agents(self):
        for agent, homes in (('codex', ('.codex', '.codex-other')), ('claude', (None, '.claude-other'))):
            rows = [{**self.native, 'agent': agent, 'home': home, 'sessionId': str(uuid.uuid4())} for home in homes]
            self.board.state[self.host['name']]['data'][agent] = rows
            for index, home in enumerate(homes):
                source = {**self.source, 'agent': agent, 'home': home}
                if agent == 'claude':
                    source['sessionId'] = rows[index]['sessionId']
                response, result = self.save('Alias ' + str(index), source)
                self.assertEqual(response.status, 200, result)
            snapshot = self.request('GET', '/api/snapshot')[1]
            self.assertEqual([row['titleAlias'] for row in snapshot['hosts'][0]['data'][agent]], ['Alias 0', 'Alias 1'])

    def test_unknown_source_wrong_home_and_duplicate_exact_identity_rejected(self):
        for source in ({**self.source, 'host': 'other'}, {**self.source, 'id': str(uuid.uuid4())},
                       {**self.source, 'home': '.codex-other'}, {**self.source, 'home': None},
                       {**self.source, 'home': ''}, {**self.source, 'agent': 'shell'}):
            self.assertEqual(self.save(source=source)[0].status, 400)
        self.board.state[self.host['name']]['data']['codex'].append(dict(self.native))
        self.assertEqual(self.save()[0].status, 400)
        self.assertFalse(self.httpd.session_labels.path.exists())

    def test_claude_empty_id_normalization_and_wrong_home(self):
        sid = str(uuid.uuid4())
        self.board.state[self.host['name']]['data']['claude'] = [{'id': '', 'sessionId': sid, 'kind': 'interactive', 'title': 'Claude'}]
        source = {'host': self.host['name'], 'agent': 'claude', 'id': sid, 'sessionId': sid}
        response, result = self.save(source=source)
        self.assertEqual(response.status, 200, result)
        self.assertEqual(result['source']['home'], '.claude')
        self.assertEqual(result['source']['sessionId'], sid)
        self.assertEqual(self.save(source={**source, 'home': '.claude-other'})[0].status, 400)

    def test_claude_reused_worker_id_cannot_rename_a_different_transcript(self):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        row = {**self.native, 'agent': 'claude', 'home': '.claude', 'sessionId': first}
        self.board.state[self.host['name']]['data']['claude'] = [row]
        source = {**self.source, 'agent': 'claude', 'home': '.claude', 'sessionId': first}
        self.assertEqual(self.save('First alias', source)[0].status, 200)
        before, version = self.httpd.session_labels.path.read_bytes(), self.board.version
        row['sessionId'] = second
        for stale in (source, {key: value for key, value in source.items() if key != 'sessionId'}):
            response, result = self.save('Wrong transcript alias', stale)
            self.assertEqual(response.status, 400, result)
        self.assertEqual(self.httpd.session_labels.path.read_bytes(), before)
        self.assertEqual(self.board.version, version)
        snapshot = self.request('GET', '/api/snapshot')[1]
        self.assertIsNone(snapshot['hosts'][0]['data']['claude'][0]['titleAlias'])
        response, result = self.save('Second alias', {**source, 'sessionId': second})
        self.assertEqual(response.status, 200, result)
        self.assertEqual(result['source']['sessionId'], second)

    def test_bot_linked_native_session_cannot_be_renamed(self):
        self.httpd.workflow.bot_requests = SimpleNamespace(snapshot=lambda: {'requests': [
            {'host': self.source['host'], 'nativeRef': {key: self.source[key] for key in ('agent', 'id', 'home')}}]})
        response, result = self.save()
        self.assertEqual(response.status, 400)
        self.assertIn('봇', result['error'])
        self.assertFalse(self.httpd.session_labels.path.exists())

    def test_failed_write_returns_error_without_event_or_in_memory_alias(self):
        self.save('Saved')
        version = self.board.version
        with patch('goals.os.replace', side_effect=OSError('synthetic failure')):
            self.assertEqual(self.save('Failed')[0].status, 503)
        self.assertEqual(self.board.version, version)
        snapshot = self.request('GET', '/api/snapshot')[1]
        self.assertEqual(snapshot['hosts'][0]['data']['codex'][0]['displayTitle'], 'Saved')

    def test_bad_body_missing_title_and_title_limit_rejected(self):
        for payload in ({'source': self.source}, {'source': self.source, 'title': 1},
                        {'source': self.source, 'title': 'x' * 161},
                        {'source': self.source, 'title': 'x', 'unknown': True}):
            self.assertEqual(self.request('POST', '/api/session/label', payload)[0].status, 400)
        self.assertEqual(self.save('x' * 65536)[0].status, 400)


if __name__ == '__main__':
    unittest.main()
