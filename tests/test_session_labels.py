"""Synthetic persistence, exact identity and authenticated board-only title edits."""
import copy
import gzip
import http.client
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goals import GoalError
from session_labels import SessionLabels, SessionSeen, SessionTidied, reference
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


class SessionTidiedTest(unittest.TestCase):
    """When a session was put away from the list: its own file and field, cleared on request."""
    def test_a_tidy_mark_is_kept_apart_from_read_marks_and_can_be_cleared(self):
        with tempfile.TemporaryDirectory() as temp:
            tidied = SessionTidied(Path(temp), clock=lambda: 5000)
            source = {'host': 'example-mini', 'agent': 'codex', 'id': str(uuid.uuid4()), 'home': '.codex', 'updatedAt': 4000}
            snapshot = {'version': 1, 'hosts': [{'name': 'example-mini', 'data': {'codex': [dict(source)], 'claude': []}}]}
            self.assertEqual(tidied.mark(source, 4100), ({'seenAt': 4100}, True))
            decorated = tidied.decorate(snapshot)
            self.assertEqual(decorated['hosts'][0]['data']['codex'][0]['tidiedAt'], 4100)
            self.assertNotIn('seenAt', decorated['hosts'][0]['data']['codex'][0])
            self.assertTrue(decorated['sessionTidied']['ok'])
            self.assertTrue((Path(temp) / 'session-tidied.json').exists())
            self.assertFalse((Path(temp) / 'session-seen.json').exists())
            self.assertEqual(stat.S_IMODE((Path(temp) / 'session-tidied.json').stat().st_mode), 0o600)
            self.assertTrue(tidied.unmark(source))
            self.assertFalse(tidied.unmark(source), 'nothing left to clear')
            self.assertIsNone(tidied.decorate(snapshot)['hosts'][0]['data']['codex'][0]['tidiedAt'])


class SessionLabelsHTTPTest(unittest.TestCase):
    def test_pins_are_shared_exact_identity_and_removal_is_independent(self):
        response, result = self.request('POST', '/api/session/pin', {'source': self.source, 'pinned': True})
        self.assertEqual(response.status, 200)
        self.assertIsInstance(result['pinnedAt'], int)
        snapshot = self.request('GET', '/api/snapshot')[1]
        row = snapshot['hosts'][0]['data']['codex'][0]
        self.assertEqual(row['pinnedAt'], result['pinnedAt'])
        from session_labels import SessionPinned
        self.assertEqual(SessionPinned(self.temp.name).decorate(self.board.snapshot())['hosts'][0]['data']['codex'][0]['pinnedAt'], result['pinnedAt'])
        self.request('POST', '/api/session/remove', {'source': self.source, 'removed': True})
        row = self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]
        self.assertEqual(row['pinnedAt'], result['pinnedAt']); self.assertIsNotNone(row['removedAt'])
        self.assertEqual(self.request('POST', '/api/session/pin', {'source': self.source, 'pinned': False})[0].status, 200)
        row = self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]
        self.assertIsNone(row['pinnedAt']); self.assertIsNotNone(row['removedAt'])

    def test_pin_route_requires_auth_csrf_and_an_exact_listed_source(self):
        payload = {'source': self.source, 'pinned': True}
        self.assertEqual(self.request('POST', '/api/session/pin', payload, auth=False)[0].status, 401)
        self.assertEqual(self.request('POST', '/api/session/pin', payload, csrf=False)[0].status, 403)
        for changed in ({'pinned': 1}, {'source': {**self.source, 'id': str(uuid.uuid4())}}, {'at': 1}):
            self.assertEqual(self.request('POST', '/api/session/pin', {**payload, **changed})[0].status, 400)
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

    def test_a_listed_session_is_put_away_from_the_list_and_shown_again(self):
        response, result = self.request('POST', '/api/session/tidy', {'source': self.source, 'tidied': True})
        self.assertEqual(response.status, 200)
        self.assertEqual(type(result['tidiedAt']), int)
        snapshot = self.request('GET', '/api/snapshot')[1]
        self.assertEqual(snapshot['hosts'][0]['data']['codex'][0]['tidiedAt'], result['tidiedAt'])
        self.assertEqual(self.request('POST', '/api/session/tidy', {'source': self.source, 'tidied': False})[1], {'tidiedAt': None})
        self.assertIsNone(self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]['tidiedAt'])
        for payload in ({'source': self.source}, {'source': self.source, 'tidied': 'yes'},
                        {'source': {**self.source, 'id': str(uuid.uuid4())}, 'tidied': True}):
            self.assertEqual(self.request('POST', '/api/session/tidy', payload)[0].status, 400)
        self.assertEqual(self.request('POST', '/api/session/tidy', {'source': self.source, 'tidied': True}, csrf=False)[0].status, 403)

    def test_a_listed_session_is_removed_from_the_list_and_shown_again(self):
        response, result = self.request('POST', '/api/session/remove', {'source': self.source, 'removed': True})
        self.assertEqual(response.status, 200)
        self.assertEqual(type(result['removedAt']), int)
        row = self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]
        self.assertEqual(row['removedAt'], result['removedAt'])
        # A later change does not undo it (unlike a tidy): the screen keeps it away.
        self.native['updatedAt'] = result['removedAt'] + 9999
        self.assertEqual(self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]['removedAt'], result['removedAt'])
        shown = self.request('POST', '/api/session/remove', {'source': self.source, 'removed': False})[1]
        self.assertEqual(shown['removedAt'], None)
        # The snapshot version that carries it: a screen keeps its own choice until then.
        self.assertEqual(shown['version'], self.request('GET', '/api/snapshot')[1]['version'])
        self.assertIsNone(self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]['removedAt'])
        for payload in ({'source': self.source}, {'source': self.source, 'removed': 1},
                        {'source': {**self.source, 'id': str(uuid.uuid4())}, 'removed': True}):
            self.assertEqual(self.request('POST', '/api/session/remove', payload)[0].status, 400)
        self.assertEqual(self.request('POST', '/api/session/remove', {'source': self.source, 'removed': True}, csrf=False)[0].status, 403)

    def test_a_mic_trace_is_kept_as_codes_only_owner_only_and_bounded(self):
        trace = {'env': {'api': 'webkit', 'field': 'chat-input', 'touch': True, 'value': 3},
                 'outcome': 'error:no-speech',
                 'events': [{'t': 0, 'e': 'press'}, {'t': 120, 'e': 'result', 'n': 1, 'len': 5, 'last': False},
                            {'t': 130, 'e': 'input', 'type': 'insertCompositionText', 'comp': True, 'idx': None}]}
        response, result = self.request('POST', '/api/diagnostics/dictation', trace, extra={'User-Agent': 'Mozilla/5.0 (Linux; Android 15) \u00e9'})
        self.assertEqual((response.status, result), (200, {'ok': True}))
        path = Path(self.temp.name) / 'dictation-trace.jsonl'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        line = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual(line['events'], trace['events'])
        self.assertEqual(line['env'], trace['env'])
        self.assertEqual(line['agent'], 'Mozilla/5.0 (Linux; Android 15) ')
        # Words never pass: strings are short ASCII codes only; shapes and sizes are fixed.
        for bad in ({**trace, 'events': [{'t': 1, 'e': 'result', 'text': '안녕하세요'}]},
                    {**trace, 'outcome': 'said hello'}, {**trace, 'words': 'x'},
                    {**trace, 'events': [{'t': 1, 'e': 'x', 'nested': {'a': 1}}]},
                    {**trace, 'events': [{'t': 1, 'e': 'x'}] * 401}, {**trace, 'env': {'not a key': 1}}):
            self.assertEqual(self.request('POST', '/api/diagnostics/dictation', bad)[0].status, 400)
        self.assertEqual(self.request('POST', '/api/diagnostics/dictation', trace, csrf=False)[0].status, 403)
        # About 1 MB, then the previous file takes the old lines.
        with patch.object(server.DictationTraces, 'LIMIT', 600):
            for _ in range(3):
                self.request('POST', '/api/diagnostics/dictation', trace)
        self.assertTrue((Path(self.temp.name) / 'dictation-trace.jsonl.1').exists())
        self.assertLessEqual(path.stat().st_size, 600)

    def test_opening_a_listed_session_is_kept_with_its_time(self):
        response, result = self.request('POST', '/api/session/opened', {'source': self.source})
        self.assertEqual(response.status, 200)
        self.assertEqual(type(result['openedAt']), int)
        self.assertEqual(self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]['openedAt'], result['openedAt'])
        for payload in ({'source': self.source, 'at': 1}, {'source': {**self.source, 'id': str(uuid.uuid4())}}):
            self.assertEqual(self.request('POST', '/api/session/opened', payload)[0].status, 400)
        self.assertEqual(self.request('POST', '/api/session/opened', {'source': self.source}, csrf=False)[0].status, 403)

    def test_a_terminal_its_user_closes_puts_its_session_away(self):
        closed = []
        linked = {'id': 'a' * 32, 'mode': 'new-session', 'alive': True, 'closed': False, 'linkConfidence': 'estimated',
                  'nativeSource': {**self.source, 'cwd': '/example/workspace', 'kind': None}}
        unlisted = {'id': 'b' * 32, 'mode': 'attach', 'alive': True, 'closed': False,
                    'nativeSource': {**self.source, 'id': str(uuid.uuid4())}}
        terminals = SimpleNamespace(list=lambda: [linked, unlisted], close=lambda: None,
                                    close_terminal=lambda tid: closed.append(tid) or {'ok': True})
        self.httpd.terminals = self.httpd.workflow.terminals = terminals
        self.assertEqual(self.request('POST', '/api/terminal/%s/close' % ('b' * 32), {})[0].status, 200)
        self.assertIsNone(self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]['tidiedAt'])
        self.assertEqual(self.request('POST', '/api/terminal/%s/close' % ('a' * 32), {})[0].status, 200)
        self.assertEqual(closed, ['b' * 32, 'a' * 32])
        self.assertEqual(type(self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]['tidiedAt']), int)

    def claude_row(self, phase='done'):
        sid = str(uuid.uuid4())
        row = {'id': 'abcd1234', 'sessionId': sid, 'kind': 'background', 'cwd': '/example/workspace',
               'title': 'Claude job', 'phase': phase}
        self.board.state[self.host['name']]['data']['claude'] = [row]
        return row

    def claude_tidied(self):
        return self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['claude'][0]['tidiedAt']

    def test_claude_sessions_are_put_away_by_their_exact_conversation(self):
        row = self.claude_row()
        native = self.httpd.workflow.native_of({**row, 'host': self.host['name'], 'agent': 'claude', 'home': '.claude'})
        self.assertEqual(native['sessionId'], row['sessionId'])
        self.assertNotIn('sessionId', self.httpd.workflow.native_of({**self.native, 'host': self.host['name'], 'agent': 'codex', 'sessionId': 'x' * 10}))
        closed = []
        terminal = {'id': 'c' * 32, 'mode': 'attach', 'alive': True, 'closed': False, 'nativeSource': native}
        self.httpd.terminals = self.httpd.workflow.terminals = SimpleNamespace(
            list=lambda: [terminal], close=lambda: None, close_terminal=lambda tid: closed.append(tid) or {'ok': True})
        self.assertEqual(self.request('POST', '/api/terminal/%s/close' % ('c' * 32), {})[0].status, 200)
        self.assertEqual(type(self.claude_tidied()), int)
        # 실행 종료 from 세션 관리 names the row the board resolved, sessionId included.
        self.claude_row()
        self.board.state[self.host['name']]['ok'] = True
        proof = 'proof.' + 'f' * 64
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value={'confirmed': True}), \
                patch.object(self.board, 'request_refresh'):
            response, _ = self.request('POST', '/api/session/action', {
                'source': {'host': self.host['name'], 'agent': 'claude', 'id': 'abcd1234'}, 'action': 'terminate',
                'requestId': 'request-claude-0001', 'targetProof': proof})
        self.assertEqual(response.status, 200)
        self.assertEqual(type(self.claude_tidied()), int)

    def test_a_view_closed_while_its_run_goes_on_or_an_ended_terminal_puts_nothing_away(self):
        for phase, mode, alive in (('working', 'attach', True), ('needs_input', 'attach', True), ('done', 'new-session', False)):
            with self.subTest(phase=phase, mode=mode):
                row = self.claude_row(phase)
                native = self.httpd.workflow.native_of({**row, 'host': self.host['name'], 'agent': 'claude', 'home': '.claude'})
                terminal = {'id': 'd' * 32, 'mode': mode, 'alive': alive, 'closed': not alive, 'nativeSource': native}
                self.httpd.terminals = self.httpd.workflow.terminals = SimpleNamespace(
                    list=lambda: [terminal], close=lambda: None, close_terminal=lambda tid: {'ok': True})
                self.assertEqual(self.request('POST', '/api/terminal/%s/close' % ('d' * 32), {})[0].status, 200)
                self.assertIsNone(self.claude_tidied())
        # A run the board ends (not just a view) is put away even while it was working.
        row = self.claude_row('working')
        native = self.httpd.workflow.native_of({**row, 'host': self.host['name'], 'agent': 'claude', 'home': '.claude'})
        terminal = {'id': 'e' * 32, 'mode': 'resume', 'alive': True, 'closed': False, 'nativeSource': native}
        self.httpd.terminals = self.httpd.workflow.terminals = SimpleNamespace(
            list=lambda: [terminal], close=lambda: None, close_terminal=lambda tid: {'ok': True})
        self.request('POST', '/api/terminal/%s/close' % ('e' * 32), {})
        self.assertEqual(type(self.claude_tidied()), int)

    def test_a_confirmed_terminate_puts_the_session_away_and_an_interrupt_does_not(self):
        self.board.state[self.host['name']]['ok'] = True
        proof = 'proof.' + 'f' * 64
        with patch.object(self.httpd.workflow.transfers, 'rpc', return_value={'confirmed': True}), \
                patch.object(self.board, 'request_refresh'):
            for action in ('interrupt', 'terminate'):
                response, _ = self.request('POST', '/api/session/action', {'source': self.source, 'action': action,
                                                                           'requestId': 'request-%s-0001' % action, 'targetProof': proof})
                self.assertEqual(response.status, 200)
                tidied = self.request('GET', '/api/snapshot')[1]['hosts'][0]['data']['codex'][0]['tidiedAt']
                self.assertEqual(tidied is None, action == 'interrupt', action)

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


class BoardPreferencesTest(unittest.TestCase):
    def setUp(self):
        from session_labels import BoardPreferences
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = BoardPreferences(self.temp.name)

    def test_empty_store_preserves_browser_defaults_then_persists_shared_empty_or_custom_list(self):
        from session_labels import BoardPreferences
        self.assertEqual(self.store.snapshot(), {'ok': True, 'revision': 0, 'quickReplies': None})
        result = self.store.set_quick_replies([' 계속 진행 '], 0)
        self.assertEqual(result, {'ok': True, 'revision': 1, 'quickReplies': ['계속 진행']})
        self.assertEqual(BoardPreferences(self.temp.name).snapshot(), result)
        self.assertEqual(self.store.set_quick_replies([], 1)['quickReplies'], [])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_two_devices_conflict_without_overwriting_and_same_value_retry_is_idempotent(self):
        from session_labels import BoardPreferences
        second = BoardPreferences(self.temp.name)
        self.store.set_quick_replies(['first'], 0)
        with self.assertRaises(GoalError) as caught:
            second.set_quick_replies(['second'], 0)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(second.snapshot()['quickReplies'], ['first'])
        self.assertEqual(second.set_quick_replies(['first'], 0)['revision'], 1)

    def test_validation_and_corrupt_store_never_replace_saved_content(self):
        for rows, revision in [(['x'] * 13, 0), (['x' * 201], 0), (['\n'], 0),
                               (['bad\x00'], 0), (['bad\ud800'], 0), ([], True), ([], -1)]:
            with self.subTest(rows=repr(rows)), self.assertRaises(GoalError):
                self.store.set_quick_replies(rows, revision)
        self.store.set_quick_replies(['safe'], 0)
        raw = b'PRIVATE_CORRUPT_CONTENT'; self.store.path.write_bytes(raw)
        with self.assertRaises(GoalError): self.store.snapshot()
        with self.assertRaises(GoalError): self.store.set_quick_replies(['replacement'], 1)
        self.assertEqual(self.store.path.read_bytes(), raw)

    def test_atomic_failure_preserves_previous_list(self):
        self.store.set_quick_replies(['old'], 0)
        with patch('goals.os.replace', side_effect=OSError('fixture disk failure')), self.assertRaises(GoalError):
            self.store.set_quick_replies(['new'], 1)
        self.assertEqual(self.store.snapshot()['quickReplies'], ['old'])
        self.assertFalse(list(Path(self.temp.name).glob('*.tmp')))

    def test_unsafe_storage_is_not_read_or_written(self):
        target = Path(self.temp.name) / 'unrelated.json'; target.write_text('private'); target.chmod(0o600)
        for linked in ('symlink', 'hardlink'):
            if linked == 'symlink': self.store.path.symlink_to(target)
            else: os.link(target, self.store.path)
            with self.assertRaises(GoalError): self.store.snapshot()
            with self.assertRaises(GoalError): self.store.set_quick_replies(['replacement'], 0)
            self.assertEqual(target.read_text(), 'private'); self.store.path.unlink()


class BoardPreferencesHTTPTest(unittest.TestCase):
    setUp = SessionLabelsHTTPTest.setUp
    tearDown = SessionLabelsHTTPTest.tearDown
    request = SessionLabelsHTTPTest.request
    def test_shared_preferences_auth_csrf_origin_and_snapshot_updates(self):
        self.assertEqual(self.request('GET', '/api/preferences', auth=False)[0].status, 401)
        body = {'quickReplies': ['shared'], 'revision': 0}
        for changes in ({'auth': False}, {'csrf': False}, {'extra': {'Origin': 'https://other.invalid'}}):
            self.assertIn(self.request('POST', '/api/preferences/quick-replies', body, **changes)[0].status, (401, 403))
        response, result = self.request('POST', '/api/preferences/quick-replies', body)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.request('GET', '/api/preferences')[1], result)
        self.assertEqual(self.request('GET', '/api/snapshot')[1]['quickReplyPreferences'], result)
        response, result = self.request('POST', '/api/preferences/quick-replies', {**body, 'quickReplies': ['conflict']})
        self.assertEqual((response.status, result['code']), (409, 'preferences_conflict'))


class HistoryHTTPTest(unittest.TestCase):
    setUp = SessionLabelsHTTPTest.setUp
    tearDown = SessionLabelsHTTPTest.tearDown
    request = SessionLabelsHTTPTest.request

    def test_claude_message_identity_includes_the_transcript_and_refuses_reused_worker_ids(self):
        from urllib.parse import urlencode
        sid, worker = str(uuid.uuid4()), 'claude-worker-123'
        source = {'host': self.host['name'], 'agent': 'claude', 'id': worker, 'home': '.claude', 'sessionId': sid}
        self.board.state[self.host['name']]['data']['claude'] = [{**source, 'kind': 'background', 'cwd': '/example/claude'}]
        calls = []
        def run(host, args, timeout):
            calls.append(json.loads(args[1])); native = calls[-1]['sources'][0]
            self.assertEqual(native['id'], sid)
            return {'readOnly': True, 'results': [{'source': native, 'error': 'fixture no saved body', 'complete': True}]}
        self.board.runner = run
        params = {**source, 'bodyHash': 'a' * 64}
        self.assertEqual(self.request('GET', '/api/history/message?' + urlencode(params))[0].status, 200)
        self.assertEqual(self.request('GET', '/api/history/message?' + urlencode({**params, 'sessionId': str(uuid.uuid4())}))[0].status, 400)
        self.assertEqual(len(calls), 1)

    def test_message_read_is_authenticated_bound_to_listed_source_and_read_only(self):
        from urllib.parse import urlencode
        calls = []
        def run(host, args, timeout):
            calls.append(args); self.assertEqual(args[0], 'history')
            payload = json.loads(args[1]); native = payload['sources'][0]
            return {'readOnly': True, 'results': [{'source': native, 'bodyHash': payload['bodyHash'],
                    'text': 'full saved body', 'role': 'assistant', 'textOffset': 0,
                    'fullLength': 15, 'nextTextOffset': None, 'recordOffset': 100, 'complete': True}]}
        self.board.runner = run
        params = {**self.source, 'bodyHash': 'a' * 64}
        route = '/api/history/message?' + urlencode(params)
        self.assertEqual(self.request('GET', route, auth=False)[0].status, 401); self.assertEqual(calls, [])
        self.assertEqual(self.request('GET', '/api/history/message?' + urlencode({**params, 'id': str(uuid.uuid4())}))[0].status, 400)
        self.assertEqual(calls, [])
        response, data = self.request('GET', route)
        self.assertEqual(response.status, 200); self.assertTrue(data['readOnly']); self.assertEqual(data['text'], 'full saved body')
        self.assertEqual(len(calls), 1)
        for changed in ({'bodyHash': 'invalid'}, {'offset': '-1'}, {'textOffset': 'true'}, {'path': '/private'}):
            self.assertEqual(self.request('GET', '/api/history/message?' + urlencode({**params, **changed}))[0].status, 400)
        self.assertEqual(len(calls), 1)

    def test_mismatched_history_source_or_body_never_returns_content(self):
        from urllib.parse import urlencode
        route = '/api/history/message?' + urlencode({**self.source, 'bodyHash': 'a' * 64})
        for mismatch in ('source', 'bodyHash'):
            def run(host, args, timeout):
                payload = json.loads(args[1]); native = payload['sources'][0]
                row = {'source': native, 'bodyHash': 'a' * 64, 'text': 'WRONG_CONTENT', 'textOffset': 0}
                row[mismatch] = {**native, 'id': 'different-id'} if mismatch == 'source' else 'b' * 64
                return {'readOnly': True, 'results': [row]}
            self.board.runner = run
            response, data = self.request('GET', route)
            self.assertNotEqual(response.status, 200); self.assertNotIn('WRONG_CONTENT', json.dumps(data))

    def test_search_cursor_query_and_catalog_scope_preserve_progress_and_skip_hidden_or_offline(self):
        from urllib.parse import urlencode
        calls = []; self.board.state[self.host['name']]['ok'] = True
        def run(host, args, timeout):
            calls.append(args); self.assertEqual(args[0], 'history'); payload = json.loads(args[1]); native = payload['sources'][0]
            return {'readOnly': True, 'results': [{'source': native, 'complete': native['offset'] > 0,
                'nextOffset': 100 if native['offset'] == 0 else None, 'hits': [{'bodyHash': 'a' * 64, 'role': 'assistant',
                'snippet': '<script>matching text</script>', 'recordOffset': 12, 'fullLength': 10000}]}]}
        self.board.runner = run
        route = '/api/history/search?q=matching'
        self.assertEqual(self.request('GET', route, auth=False)[0].status, 401); self.assertEqual(calls, [])
        response, first = self.request('GET', route)
        self.assertEqual(response.status, 200); self.assertEqual(first['scanned'], 0); self.assertIsNotNone(first['cursor'])
        response, second = self.request('GET', route + '&' + urlencode({'cursor': first['cursor']}))
        self.assertEqual(response.status, 200); self.assertEqual(second['scanned'], 1); self.assertIsNone(second['cursor'])
        self.assertEqual(first['hits'][0]['source']['id'], self.source['id'])
        self.assertEqual(self.request('GET', '/api/history/search?' + urlencode({'q': 'changed', 'cursor': first['cursor']}))[0].status, 409)
        for query in ('q=x', 'q=matching&cursor=bad', 'q=matching&path=/private'):
            self.assertEqual(self.request('GET', '/api/history/search?' + query)[0].status, 400)
        before = len(calls); self.board.state[self.host['name']]['ok'] = False
        response, data = self.request('GET', route); self.assertEqual(response.status, 200)
        self.assertEqual(len(calls), before); self.assertEqual(len(data['errors']), 1)
        self.httpd.session_removed.mark(self.source, 123)
        response, data = self.request('GET', route); self.assertEqual(data['total'], 0)
        response, data = self.request('GET', route + '&includeRemoved=1'); self.assertEqual(data['total'], 1)

    def test_history_parallel_limit_is_reported_without_native_execution(self):
        self.httpd.history_gate.acquire(); self.httpd.history_gate.acquire()
        try:
            response, result = self.request('GET', '/api/history/search?q=matching')
            self.assertEqual(response.status, 429)
        finally:
            self.httpd.history_gate.release(); self.httpd.history_gate.release()

    def question(self, index):
        return {'bodyHash': '%064x' % index, 'snippet': '질문 %d' % index, 'recordOffset': 100 + index,
                'ts': 1700000000 + index, 'fullLength': 4}

    def test_questions_are_read_once_for_a_listed_session_and_go_on_with_before(self):
        from urllib.parse import urlencode
        calls = []
        def run(host, args, timeout):
            calls.append(json.loads(args[1])); self.assertEqual(args[0], 'history')
            native = calls[-1]['sources'][0]
            more = 'before' not in native
            return {'readOnly': True, 'results': [{'source': native, 'questions': [self.question(2), self.question(1)] if more else [self.question(0)],
                    'complete': not more, 'before': 40 if more else None, 'scannedBytes': 9, 'skipped': 0}]}
        self.board.runner = run
        route = '/api/history/questions?' + urlencode(self.source)
        self.assertEqual(self.request('GET', route, auth=False)[0].status, 401); self.assertEqual(calls, [])
        response, data = self.request('GET', route)
        self.assertEqual(response.status, 200)
        self.assertEqual(data, {'questions': [self.question(2), self.question(1)], 'complete': False, 'before': 40, 'readOnly': True})
        self.assertEqual(calls[-1]['questions'], True)
        self.assertNotIn('offset', calls[-1]['sources'][0])
        response, data = self.request('GET', route + '&before=40')
        self.assertEqual((data['questions'], data['complete'], data['before']), ([self.question(0)], True, None))
        self.assertEqual(calls[-1]['sources'][0]['before'], 40)
        # Unknown arguments, a session not listed, a bad position: refused before any read.
        before = len(calls)
        for changed in ({'path': '/private'}, {'id': str(uuid.uuid4())}, {'before': '-1'}, {'before': 'x'}):
            self.assertEqual(self.request('GET', '/api/history/questions?' + urlencode({**self.source, **changed}))[0].status, 400)
        self.assertEqual(len(calls), before)

    def test_a_questions_answer_for_another_session_or_out_of_shape_is_never_shown(self):
        from urllib.parse import urlencode
        route = '/api/history/questions?' + urlencode(self.source)
        bad_rows = [{**self.question(1), 'bodyHash': 'zz'}, {**self.question(1), 'snippet': 'x' * 301},
                    {**self.question(1), 'recordOffset': -1}, {**self.question(1), 'ts': 'now'}]
        for case in ['source', 'error', 'many'] + bad_rows:
            def run(host, args, timeout, case=case):
                native = json.loads(args[1])['sources'][0]
                row = {'source': native, 'questions': [self.question(1)], 'complete': True, 'before': None}
                if case == 'source': row['source'] = {**native, 'id': 'different-id'}
                elif case == 'error': row = {'source': native, 'error': '저장 대화 기록을 확인하지 못했습니다.', 'complete': True}
                elif case == 'many': row['questions'] = [self.question(index) for index in range(301)]
                else: row['questions'] = [case]
                return {'readOnly': True, 'results': [row]}
            self.board.runner = run
            response, data = self.request('GET', route)
            self.assertNotEqual(response.status, 200, case); self.assertNotIn('질문 1', json.dumps(data, ensure_ascii=False))

    def test_questions_have_their_own_parallel_limit(self):
        from urllib.parse import urlencode
        self.board.runner = lambda host, args, timeout: {'readOnly': True, 'results': [
            {'source': json.loads(args[1])['sources'][0], 'questions': [], 'complete': True, 'before': None}]}
        self.httpd.history_gate.acquire(); self.httpd.history_gate.acquire()
        try:
            self.assertEqual(self.request('GET', '/api/history/questions?' + urlencode(self.source))[0].status, 200,
                             'a 전체 보기 or search in progress does not hold the questions back')
        finally:
            self.httpd.history_gate.release(); self.httpd.history_gate.release()
        self.httpd.questions_gate.acquire(); self.httpd.questions_gate.acquire()
        try:
            self.assertEqual(self.request('GET', '/api/history/questions?' + urlencode(self.source))[0].status, 429)
        finally:
            self.httpd.questions_gate.release(); self.httpd.questions_gate.release()


class SnapshotResponseTest(unittest.TestCase):
    """/api/snapshot is kept while nothing in it changed, and every input of it changes it."""
    setUp = SessionLabelsHTTPTest.setUp
    tearDown = SessionLabelsHTTPTest.tearDown
    request = SessionLabelsHTTPTest.request
    STORES = ('session_labels', 'session_seen', 'session_tidied', 'session_removed', 'session_opened', 'session_pinned')
    FIELDS = ('sessionLabels', 'sessionSeen', 'sessionTidied', 'sessionRemoved', 'sessionOpened', 'sessionPinned')

    def counted(self):
        """Patches counting deep copies and store file reads (both still done)."""
        copies, reads = [], []
        deepcopy, labels, seen = copy.deepcopy, SessionLabels._read, SessionSeen._read
        patches = (patch('copy.deepcopy', side_effect=lambda *a, **k: copies.append(1) or deepcopy(*a, **k)),
                   patch.object(SessionSeen, '_read', autospec=True, side_effect=lambda store: reads.append(1) or seen(store)),
                   patch.object(SessionLabels, '_read', autospec=True, side_effect=lambda store: reads.append(1) or labels(store)))
        return copies, reads, patches

    def raw(self, gzip_ok=False):
        client = http.client.HTTPConnection('127.0.0.1', self.httpd.server_address[1], timeout=5)
        client.request('GET', '/api/snapshot', headers={'Cookie': self.cookie,
                                                        'Accept-Encoding': 'gzip' if gzip_ok else 'identity'})
        response = client.getresponse()
        body = response.read()
        client.close()
        self.assertEqual(response.status, 200, body[:200])
        if response.getheader('Content-Encoding') == 'gzip':
            body = gzip.decompress(body)
        return response, body

    def snapshot(self):
        return json.loads(self.raw()[1])

    def warm(self):
        # The first answer starts the read and tidy records (new files): the next one is kept.
        self.snapshot()
        return self.snapshot()

    def fresh(self):
        """What the board answered before answers were kept: every store's decorate on its
        own copy, chained, then the bootId and preferences, serialized as send_json does."""
        snapshot = self.board.snapshot()
        for name in self.STORES:
            snapshot = getattr(self.httpd, name).decorate(snapshot)
        snapshot = {**snapshot, 'bootId': self.httpd.boot_id, 'quickReplyPreferences': self.httpd.preferences.snapshot()}
        return json.dumps(snapshot, ensure_ascii=False).encode()

    def test_an_unchanged_snapshot_is_answered_without_copying_or_reading_any_store(self):
        self.httpd.session_labels.set(self.source, 'Alias')
        first = self.warm()
        copies, reads, patches = self.counted()
        with patches[0], patches[1], patches[2]:
            again = self.snapshot()
        self.assertEqual((len(copies), len(reads)), (0, 0))
        self.assertEqual({**again, 'serverTime': 0}, {**first, 'serverTime': 0})

    def test_a_board_change_copies_the_list_once_and_reads_no_unchanged_store_again(self):
        import session_labels
        self.httpd.session_labels.set(self.source, 'Alias')
        self.warm()
        self.native['title'] = 'Changed native title'
        self.board.changed()
        copies, reads, patches = self.counted()
        identities, identity = [], session_labels._identity
        with patches[0], patches[1], patches[2], \
             patch.object(session_labels, '_identity', side_effect=lambda source: identities.append(1) or identity(source)):
            row = self.snapshot()['hosts'][0]['data']['codex'][0]
        self.assertEqual((row['displayTitle'], row['originalTitle']), ('Alias', 'Changed native title'))
        self.assertEqual(len(copies), 1, 'Board.snapshot copies the hosts; the stores decorate that copy')
        self.assertEqual((len(reads), len(identities)), (0, 0), 'unchanged files and rows are not read or identified again')

    def test_a_kept_answer_is_the_same_bytes_as_a_fresh_one(self):
        self.httpd.session_labels.set(self.source, 'Board name')
        self.httpd.session_seen.mark(self.source, 4000)
        self.httpd.session_pinned.mark(self.source, 4100)
        self.httpd.preferences.set_quick_replies(['계속 진행'], 0)
        other = {**self.native, 'id': str(uuid.uuid4()), 'title': None, 'name': '이름 sk-proj-12345678901234567890'}
        claude = {'id': '', 'kind': 'interactive', 'sessionId': str(uuid.uuid4()), 'title': 'Claude'}
        broken = {'id': 'not a session id', 'title': 'Unidentifiable'}
        self.board.state[self.host['name']]['data'].update(codex=[self.native, other, broken], claude=[claude])
        self.board.changed()
        self.snapshot()
        # serverTime is the time of each answer; every other byte is compared as is.
        head = re.compile(rb'^\{"serverTime": (\d+), ')

        def split(body):
            found = head.match(body)
            self.assertIsNotNone(found, body[:40])
            return int(found[1]), body[found.end():]
        expected = self.fresh()
        for gzip_ok in (False, True, False, True):
            before = int(time.time())
            response, body = self.raw(gzip_ok)
            at, rest = split(body)
            self.assertEqual(rest, split(expected)[1])
            self.assertTrue(before <= at <= time.time())
            self.assertEqual(response.getheader('Content-Encoding'), 'gzip' if gzip_ok else None)
            self.assertEqual(response.getheader('Vary'), 'Accept-Encoding')
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        decoded = json.loads(expected)
        rows = decoded['hosts'][0]['data']
        self.assertEqual(rows['codex'][0]['displayTitle'], 'Board name')
        self.assertNotIn('sk-proj', rows['codex'][1]['originalTitle'])
        self.assertNotIn('displayTitle', rows['codex'][2])
        self.assertEqual(rows['claude'][0]['displayTitle'], 'Claude')

    def test_every_input_of_a_kept_answer_changes_the_next_one(self):
        from session_labels import BoardPreferences, SessionOpened, SessionPinned, SessionRemoved
        directory = self.temp.name

        def row():
            return self.snapshot()['hosts'][0]['data']['codex'][0]

        def native_title():
            self.native['title'] = 'Renamed natively'
            self.board.changed()

        def boot():
            self.httpd.boot_id = 'another-boot'

        # Each store is written through its own instance, as another process would.
        changes = (
            ('board', native_title, lambda: row()['originalTitle'], 'Renamed natively'),
            ('labels', lambda: SessionLabels(directory).set(self.source, 'Alias'), lambda: row()['displayTitle'], 'Alias'),
            ('seen', lambda: SessionSeen(directory).mark(self.source, 4001), lambda: row()['seenAt'], 4001),
            ('tidied', lambda: SessionTidied(directory).mark(self.source, 4002), lambda: row()['tidiedAt'], 4002),
            ('removed', lambda: SessionRemoved(directory).mark(self.source, 4003), lambda: row()['removedAt'], 4003),
            ('opened', lambda: SessionOpened(directory).mark(self.source, 4004), lambda: row()['openedAt'], 4004),
            ('pinned', lambda: SessionPinned(directory).mark(self.source, 4005), lambda: row()['pinnedAt'], 4005),
            ('preferences', lambda: BoardPreferences(directory).set_quick_replies(['다음'], 0),
             lambda: self.snapshot()['quickReplyPreferences']['quickReplies'], ['다음']),
            ('bootId', boot, lambda: self.snapshot()['bootId'], 'another-boot'),
        )
        for name, change, read, expected in changes:
            with self.subTest(name):
                self.warm()
                self.assertNotEqual(read(), expected)
                change()
                self.assertEqual(read(), expected)
        with self.subTest('serverTime'):
            kept = self.warm()['serverTime']
            # The session cookie is checked against the same clock: move it a day ahead only.
            with patch.object(server.time, 'time', return_value=kept + 86400.5):
                with patch.object(server.Handler, 'authorized', return_value=True):
                    self.assertEqual(self.snapshot()['serverTime'], kept + 86400)

    def test_a_store_that_cannot_be_read_is_reported_and_never_kept(self):
        self.warm()
        labels = Path(self.temp.name) / 'session-labels.json'
        SessionLabels(self.temp.name).set(self.source, 'Alias')
        saved = labels.read_bytes()
        labels.write_text('{"version": 9}')
        os.chmod(labels, 0o600)
        self.assertFalse(self.snapshot()['sessionLabels']['ok'])
        self.assertFalse(self.snapshot()['sessionLabels']['ok'], 'still unreadable: read again, not kept')
        labels.write_bytes(saved)
        self.assertEqual(self.snapshot()['hosts'][0]['data']['codex'][0]['displayTitle'], 'Alias')
        # A passing failure (its lock was busy) with nothing else changed: the next answer reads again.
        self.board.changed(force=True)
        with patch.object(self.httpd.session_labels, '_storage', side_effect=SessionLabels._unavailable()):
            self.assertFalse(self.snapshot()['sessionLabels']['ok'])
        self.assertTrue(self.snapshot()['sessionLabels']['ok'])
        # A state folder that is no longer private turns every store off, as before.
        self.warm()
        os.chmod(self.temp.name, 0o755)
        try:
            snapshot = self.snapshot()
        finally:
            os.chmod(self.temp.name, 0o700)
        self.assertFalse(any(snapshot[field]['ok'] for field in self.FIELDS))
        self.assertTrue(all(self.snapshot()[field]['ok'] for field in self.FIELDS + ('quickReplyPreferences',)))

    def test_a_row_key_is_the_same_with_or_without_the_memo(self):
        import session_labels
        from session_labels import _identity, _key, _row_key
        sid = str(uuid.uuid4())
        rows = [
            {'host': 'example-mini', 'agent': 'codex', 'id': sid, 'home': '.codex'},
            {'host': 'example-mini', 'agent': 'codex', 'id': sid, 'home': '.codex-work'},
            {'host': 'example-mini', 'agent': 'codex', 'id': sid},
            {'host': 'example-mini', 'agent': 'claude', 'id': '', 'kind': 'interactive', 'sessionId': sid},
            {'host': 'example-mini', 'agent': 'claude', 'id': 'worker-1234', 'sessionId': sid},
            {'host': 'example-mini', 'agent': 'claude', 'id': 'worker-1234', 'sessionId': 'bad id'},
            {'host': 'example-mini', 'agent': 'codex', 'id': sid, 'home': '../x'},
            {'host': 'example-mini', 'agent': 'gemini', 'id': sid},
            {'host': 'example-mini', 'agent': 'codex', 'id': 1234567890},
            {'host': 'example-mini', 'agent': 'codex', 'id': sid, 'home': ['.codex']},
        ]
        for row in rows:
            try:
                expected = _key(_identity(row))
            except (GoalError, ValueError):
                expected = None
            with self.subTest(row=row):
                self.assertEqual(_row_key(row), expected)
                self.assertEqual(_row_key(dict(row)), expected, 'from the memo')
        with patch.object(session_labels, '_ROW_KEYS_LIMIT', 2), patch.dict(session_labels._ROW_KEYS, clear=True):
            for row in rows:
                _row_key(row)
            self.assertLessEqual(len(session_labels._ROW_KEYS), 2)


if __name__ == '__main__':
    unittest.main()
