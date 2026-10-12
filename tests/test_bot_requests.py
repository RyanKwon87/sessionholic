from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot_requests as bots


class BotRequestsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bot-requests-')
        self.home = Path(self.temp.name).resolve()
        self.config = self.home / '.config/session-board/bot-sources.json'
        self.config.parent.mkdir(parents=True)
        self.hosts = [{'name': 'mini', 'local': True},
                      {'name': 'pc', 'local': False, 'platform': 'windows', 'ssh': 'example-pc', 'python': 'python.exe'}]
        self.source = {'id': 'example-bot', 'label': '예시 봇', 'host': 'mini', 'kind': 'slack_ledger',
                       'path': 'bot/ledger.sqlite3', 'authorityLabel': '봇 승인 정책', 'scopeLabel': '등록된 업무 범위'}
        self.sid = str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def database(self, source=None):
        source = source or self.source
        path = self.home / source['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def ledger(self, count=1):
        path = self.database()
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''CREATE TABLE tasks(id INTEGER PRIMARY KEY,channel TEXT,user TEXT,text TEXT,status TEXT,
                codex_thread TEXT,created_at REAL,started_at REAL,finished_at REAL);
                CREATE TABLE users(id TEXT PRIMARY KEY,name TEXT,real_name TEXT);
                CREATE TABLE channels(id TEXT PRIMARY KEY,name TEXT);
                CREATE TABLE sessions(session_key TEXT PRIMARY KEY,codex_thread TEXT);''')
            db.execute('INSERT INTO users VALUES(?,?,?)', ('U_EXAMPLE', 'example', '예시 요청자'))
            db.execute('INSERT INTO channels VALUES(?,?)', ('C_EXAMPLE', '예시 채널'))
            for number in range(1, count + 1):
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?)',
                           (number, 'C_EXAMPLE', 'U_EXAMPLE', '재고 확인 요청', 'running', self.sid, 1700000000 + number, None, None))
        return path

    def native(self, sid=None):
        path = self.home / '.codex/state_5.sqlite'
        path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY)')
            db.execute('INSERT INTO threads VALUES(?)', (sid or self.sid,))
        return path

    def configure(self, sources=None):
        self.config.write_text(json.dumps({'version': 1, 'sources': sources or [self.source]}))
        self.config.chmod(0o600)
        with patch.object(Path, 'home', return_value=self.home):
            return bots.BotRequests(self.hosts, self.config, self.home / 'state')

    def test_ledger_exact_native_link_and_bounded_read_does_not_write(self):
        path = self.ledger(125)
        native = self.native()
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, native)}
        result = bots.read_source(self.source, self.home)['requests']
        self.assertEqual(len(result), 100)
        self.assertEqual(result[0]['id'], bots._opaque(self.source, 125))
        self.assertEqual(result[0]['nativeRef'], {'agent': 'codex', 'id': self.sid, 'home': '.codex'})
        self.assertEqual(result[0]['requesterLabel'], '예시 요청자')
        self.assertEqual(result[0]['channelLabel'], '예시 채널')
        self.assertEqual(result[0]['status'], 'running')
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_no_native_link_without_matching_account_database(self):
        self.ledger()
        self.assertIsNone(bots.read_source(self.source, self.home)['requests'][0]['nativeRef'])
        self.native(str(uuid.uuid4()))
        self.assertIsNone(bots.read_source(self.source, self.home)['requests'][0]['nativeRef'])

    def test_later_session_mapping_does_not_bind_old_task_and_invalid_uuid_stays_unlinked(self):
        path = self.ledger()
        self.native()
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE tasks SET codex_thread=?', ('--last',))
            db.execute('INSERT INTO sessions VALUES(?,?)', ('shared', self.sid))
        self.assertIsNone(bots.read_source(self.source, self.home)['requests'][0]['nativeRef'])

    def test_title_and_identity_labels_redact_secrets_contacts_and_urls(self):
        path = self.ledger()
        secret = 'xoxb-example-secret-value'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE tasks SET text=?', ('요청 '+secret+' api_key=do-not-show user@example.invalid 010-1234-5678 https://example.invalid/?token=private <@U123456> '+('A'*200),))
            db.execute('UPDATE users SET real_name=?', ('user@example.invalid',))
        body = json.dumps(bots.read_source(self.source, self.home), ensure_ascii=False)
        for value in (secret, 'do-not-show', 'example.invalid', '010-1234', 'U123456'):
            self.assertNotIn(value, body)
        self.assertLessEqual(len(json.loads(body)['requests'][0]['title']), 120)

    def test_holicii_reads_only_receipt_metadata_without_decrypting_or_claiming_completion(self):
        source = {**self.source, 'kind': 'holicii_sqlite'}
        path = self.database(source)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE event_inbox(id TEXT PRIMARY KEY,source_id TEXT,actor TEXT,state TEXT,received_at TEXT,processed_at TEXT,payload TEXT)')
            db.execute('INSERT INTO event_inbox VALUES(?,?,?,?,?,?,?)',
                       ('request-1', 'private-channel-key', 'private-user-key', 'processed', '2026-10-12T00:00:00Z', None, 'ENCRYPTED_DO_NOT_READ'))
        with patch.object(bots, '_text', wraps=bots._text) as scrub:
            rows = bots.read_source(source, self.home)['requests']
        self.assertEqual(rows[0]['status'], 'processed')
        self.assertIsNone(rows[0]['nativeRef'])
        self.assertEqual(rows[0]['title'], '등록된 업무 요청')
        text = json.dumps(rows) + repr(scrub.call_args_list)
        for secret in ('private-channel-key', 'private-user-key', 'ENCRYPTED_DO_NOT_READ'):
            self.assertNotIn(secret, text)

    def test_hermes_uses_slack_origin_only_and_does_not_claim_live_runtime(self):
        source = {**self.source, 'kind': 'hermes_sqlite'}
        path = self.database(source)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,session_key TEXT,user_id TEXT,chat_id TEXT,
                started_at REAL,ended_at REAL,origin_json TEXT,last_activity_at REAL);
                CREATE INDEX idx_started ON sessions(started_at);''')
            for name, kind, ended in [('gateway', 'slack', None), ('import', 'codex-cli', None), ('closed', 'slack', 1700000020)]:
                origin = json.dumps({'user_name': '예시 사용자', 'chat_name': '예시 채널', 'chat_topic': 'PRIVATE_TOPIC'})
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)', (name, kind, 'example', 'user', 'channel', 1700000000, ended, origin, 1700000010))
        rows = bots.read_source(source, self.home)['requests']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['status'] for row in rows}, {'unknown', 'closed'})
        self.assertTrue(all(row['nativeRef'] is None for row in rows))
        self.assertNotIn('PRIVATE_TOPIC', json.dumps(rows))

    def test_unknown_schema_and_missing_hermes_index_are_reported_as_source_error(self):
        self.database().touch()
        api = self.configure()
        response = api.snapshot()
        self.assertFalse(response['sources'][0]['ok'])
        self.assertEqual(response['requests'], [])
        self.assertNotIn(str(self.home), json.dumps(response))

    def test_hermes_missing_latest_index_fails_without_scanning_rows(self):
        source = {**self.source, 'kind': 'hermes_sqlite'}
        path = self.database(source)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE sessions(id TEXT,source TEXT,session_key TEXT,user_id TEXT,chat_id TEXT,started_at REAL,ended_at REAL,origin_json TEXT)')
        with self.assertRaisesRegex(bots.SourceError, '인덱스'):
            bots.read_source(source, self.home)

    def test_query_deadline_interrupts_read_without_retry(self):
        self.ledger(125)
        with patch.object(bots.time, 'monotonic', side_effect=[0] + [2] * 1000):
            with self.assertRaises(sqlite3.OperationalError):
                bots.read_source(self.source, self.home)

    def test_readonly_connection_rejects_write_and_does_not_create_absent_wal_sidecar(self):
        path = self.ledger()
        with closing(bots._connect(path, self.home)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('DELETE FROM tasks')
        wal = Path(str(path) + '-wal')
        shm = Path(str(path) + '-shm')
        wal.write_bytes(b'example-active-wal')
        with self.assertRaisesRegex(bots.SourceError, 'WAL'):
            bots.read_source(self.source, self.home)
        self.assertFalse(shm.exists())

    def test_holicii_sql_authorizer_proves_encrypted_payload_is_never_selected(self):
        source = {**self.source, 'kind': 'holicii_sqlite'}
        path = self.database(source)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE event_inbox(id TEXT,source_id TEXT,actor TEXT,state TEXT,received_at TEXT,processed_at TEXT,payload TEXT)')
            db.execute("INSERT INTO event_inbox VALUES('one','source','actor','received',NULL,NULL,'ENCRYPTED')")
        original = bots._connect
        def guarded(*args):
            db = original(*args)
            db.set_authorizer(lambda action, table, column, *unused:
                              sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and column == 'payload' else sqlite3.SQLITE_OK)
            return db
        with patch.object(bots, '_connect', side_effect=guarded):
            self.assertEqual(len(bots.read_source(source, self.home)['requests']), 1)

    def test_configuration_rejects_paths_unknown_fields_unknown_hosts_and_duplicates(self):
        for changed in ({'path': '../other.db'}, {'path': '/tmp/example.db'}, {'path': 'C:/example.db'},
                        {'path': 'folder\\example.db'}, {'path': '~other/example.db'}, {'path': 'auth.json'},
                        {'host': 'unknown'}, {'kind': 'unknown'}, {'extra': 'forbidden'}):
            with self.subTest(changed=changed):
                api = self.configure([{**self.source, **changed}])
                self.assertFalse(api.snapshot()['sources'][0]['ok'])
        api = self.configure([self.source, self.source])
        self.assertFalse(api.snapshot()['sources'][0]['ok'])

    def test_unsafe_permissions_symlink_and_account_path_are_rejected(self):
        path = self.ledger()
        api = self.configure()
        self.config.chmod(0o644)
        self.assertFalse(api.snapshot()['sources'][0]['ok'])
        self.config.chmod(0o600)
        path.chmod(0o666)
        self.assertFalse(api.snapshot(force=True)['sources'][0]['ok'])
        path.chmod(0o600)
        moved = path.with_name('other.sqlite3')
        path.rename(moved)
        path.symlink_to(moved)
        self.assertFalse(api.snapshot(force=True)['sources'][0]['ok'])

    def test_cache_is_copied_expiry_refreshes_and_failure_waits_for_explicit_force(self):
        self.ledger()
        api = self.configure()
        with patch.object(api, '_collect', wraps=api._collect) as collect:
            one = api.snapshot()
            one['requests'].clear()
            self.assertEqual(len(api.snapshot()['requests']), 1)
            self.assertEqual(collect.call_count, 1)
            api._source_cache[self.source['id']]['fetched'] -= 121
            self.assertEqual(len(api.snapshot()['requests']), 1)
            self.assertEqual(collect.call_count, 2)
        with patch.object(api, '_collect', side_effect=TimeoutError('PRIVATE_REMOTE_DETAILS')) as collect:
            response = api.snapshot(force=True)
            api._source_cache[self.source['id']]['fetched'] -= 121
            self.assertEqual(api.snapshot(), response)
            self.assertEqual(collect.call_count, 1)
            self.assertNotIn('PRIVATE_REMOTE_DETAILS', json.dumps(response))
            self.assertTrue(response['requests'][0]['stale'])
        self.assertTrue(api.snapshot(force=True)['sources'][0]['ok'])

    def test_failed_source_keeps_native_mapping_but_other_sources_refresh_after_ttl(self):
        self.ledger()
        self.native()
        sources = [self.source, {**self.source, 'id': 'other'}]
        api = self.configure(sources)
        api.snapshot()
        observed = []
        def collect(source):
            observed.append(source['id'])
            if source['id'] == self.source['id']:
                raise TimeoutError()
            return bots.read_source(source, self.home)
        with patch.object(api, '_collect', side_effect=collect):
            failed = api.snapshot(force=True)
            for cached in api._source_cache.values():
                cached['fetched'] -= 121
            result = api.snapshot()
        self.assertEqual(observed, ['example-bot', 'other', 'other'])
        stale = next(row for row in result['requests'] if row['sourceId'] == self.source['id'])
        self.assertTrue(stale['stale'])
        self.assertEqual(stale['nativeRef']['id'], self.sid)
        self.assertEqual([row['ok'] for row in result['sources']], [False, True])

    def test_bad_config_keeps_prior_native_ownership_evidence_marked_stale(self):
        self.ledger()
        self.native()
        api = self.configure()
        api.snapshot()
        self.config.write_text('{')
        result = api.snapshot()
        self.assertTrue(result['requests'][0]['stale'])
        self.assertEqual(result['requests'][0]['nativeRef']['id'], self.sid)
        self.assertTrue(all(not row['ok'] for row in result['sources']))

    def test_sources_are_sequential_and_one_error_does_not_hide_other_source(self):
        self.ledger()
        sources = [self.source, {**self.source, 'id': 'other'}]
        api = self.configure(sources)
        observed = []
        def collect(source):
            observed.append(source['id'])
            if source['id'] == sources[0]['id']:
                raise RuntimeError('fail')
            return bots.read_source(source, self.home)
        with patch.object(api, '_collect', side_effect=collect):
            result = api.snapshot()
        self.assertEqual(observed, ['example-bot', 'other'])
        self.assertEqual([row['ok'] for row in result['sources']], [False, True])
        self.assertEqual(len(result['requests']), 1)

    def test_remote_uses_existing_host_command_and_self_contained_stdin_without_install(self):
        source = {**self.source, 'host': 'pc'}
        self.ledger()
        expected = bots.read_source(source, self.home)
        api = self.configure([source])
        command = Mock(return_value=['ssh', 'example-pc', 'python-stdin'])
        server = types.SimpleNamespace(collector_command=command)
        done = subprocess.CompletedProcess([], 0, json.dumps(expected).encode())
        with patch.dict(sys.modules, {'server': server}), patch.object(subprocess, 'run', return_value=done) as run:
            result = api.snapshot()
        self.assertTrue(result['sources'][0]['ok'])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['timeout'], 20)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertIn(b'BOT_REQUEST = ', run.call_args.kwargs['input'])
        self.assertIn(b'def read_source(', run.call_args.kwargs['input'])
        command.assert_called_once_with(self.hosts[1], [], self.home / 'state')

    def test_standalone_reader_executes_with_only_stdlib_and_temp_home(self):
        self.ledger()
        program = ('BOT_REQUEST = '+ascii(self.source)+'\n').encode() + Path(bots.__file__).read_bytes()
        done = subprocess.run([sys.executable, '-I', '-'], input=program, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}, timeout=5)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        result = json.loads(done.stdout)
        self.assertEqual(len(result['requests']), 1)
        self.assertEqual(done.stderr, b'')

    def test_remote_response_cannot_reassign_host_or_add_private_fields(self):
        self.ledger()
        api = self.configure()
        result = bots.read_source(self.source, self.home)
        result['requests'][0]['host'] = 'other-account'
        with patch.object(api, '_collect', return_value=result):
            self.assertFalse(api.snapshot()['sources'][0]['ok'])
        result['requests'][0]['host'] = 'mini'
        result['requests'][0]['privatePayload'] = 'DO_NOT_EXPOSE'
        with patch.object(api, '_collect', return_value=result):
            self.assertNotIn('DO_NOT_EXPOSE', json.dumps(api.snapshot(force=True)))

    def test_missing_configuration_is_empty_and_never_discovers_unregistered_databases(self):
        with patch.object(Path, 'home', return_value=self.home):
            api = bots.BotRequests(self.hosts, self.config, self.home/'state')
        with patch.object(api, '_collect') as collect:
            self.assertEqual(api.snapshot()['sources'], [])
        collect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
