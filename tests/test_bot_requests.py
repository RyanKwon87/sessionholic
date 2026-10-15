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

    def test_a_bot_session_stays_the_bots_after_the_ledger_stops_naming_it(self):
        self.ledger()
        self.native()
        api = self.configure()
        api.snapshot()
        source = {'host': 'mini', 'agent': 'codex', 'id': self.sid, 'home': '.codex'}
        self.assertTrue(api.owns(source))
        self.assertFalse(api.owns({**source, 'id': str(uuid.uuid4())}))
        self.assertFalse(api.owns({**source, 'host': 'pc'}))
        # Later requests pushed it out of the recent window (or a read could not confirm it).
        with closing(sqlite3.connect(self.database())) as db, db:
            db.execute('UPDATE tasks SET codex_thread=NULL')
        self.assertEqual([row['nativeRef'] for row in api.snapshot(force=True)['requests']], [None])
        self.assertTrue(api.owns(source))
        self.assertTrue(self.configure().owns(source), 'kept for the next board process too')
        self.assertEqual((self.home / 'state/bot-owners.json').stat().st_mode & 0o777, 0o600)
        # A write that failed is tried again by a later read with nothing new.
        other = self.configure()
        with closing(sqlite3.connect(self.database())) as db, db:
            db.execute('UPDATE tasks SET codex_thread=?', (self.sid,))
        (self.home / 'state/bot-owners.json').unlink()
        with patch.object(bots.os, 'replace', side_effect=OSError('fixture disk')):
            other.snapshot(force=True)
        self.assertFalse((self.home / 'state/bot-owners.json').exists())
        with closing(sqlite3.connect(self.database())) as db, db:
            db.execute('UPDATE tasks SET codex_thread=NULL')
        other.snapshot(force=True)
        self.assertTrue(self.configure().owns(source))

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

    # Hermes conversations: synthetic sessions and messages only.
    ENVELOPE = ('[슬랙 요청]\n- 요청자: 예시 요청자\n- 작업 번호: 7\n[처리 원칙]\n- 합성 원칙\n'
                '[요청]\n재고 수량을 알려 주세요\n\n[마무리 규칙] 최종 답변만 Slack에 올라간다.')

    def hermes_store(self, path, sessions):
        """sessions: {session id: [(role, content, tool_calls, timestamp)]}."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,
                content TEXT,tool_calls TEXT,reasoning TEXT,timestamp REAL);
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id,timestamp);''')
            for session, rows in sessions.items():
                for role, content, calls, ts in rows:
                    db.execute('INSERT INTO messages(session_id,role,content,tool_calls,reasoning,timestamp) VALUES(?,?,?,?,?,?)',
                               (session, role, content, calls, 'REASONING_PRIVATE', ts))
        return path

    def hermes_ledger(self, refs):
        """A request ledger under <bot>/slack/ whose tasks name these Hermes references."""
        self.source = {**self.source, 'path': 'bot/slack/ledger.sqlite3'}
        path = self.ledger(len(refs))
        with closing(sqlite3.connect(path)) as db, db:
            for number, ref in enumerate(refs, 1):
                db.execute('UPDATE tasks SET codex_thread=? WHERE id=?', (ref, number))
        return path

    def test_ledger_hermes_reference_gives_an_alias_and_the_board_keeps_the_session(self):
        session = '20990101_000000_abc123'
        self.hermes_ledger(['hermes:' + session + '#a1b2c3d4', 'hermes:../escape#x', '--last'])
        rows = bots.read_source(self.source, self.home)['requests']
        by_id = {row['id']: row for row in rows}
        linked = by_id[bots._opaque(self.source, 1)]
        self.assertIsNone(linked['nativeRef'])
        self.assertEqual(linked['conversationRef'], {'kind': 'hermes', 'id': bots._opaque(self.source, 'hermes:' + session)})
        self.assertEqual(linked['_conversation'], session)
        for number in (2, 3):
            self.assertIsNone(by_id[bots._opaque(self.source, number)]['conversationRef'])
        api = self.configure()
        listed = api.snapshot()
        self.assertNotIn(session, json.dumps(listed))
        self.assertNotIn('_conversation', json.dumps(listed))
        self.assertEqual(api._source_cache[self.source['id']]['sessions'], {linked['id']: session})

    def test_conversation_reads_user_and_assistant_text_read_only_and_never_tools(self):
        session, other = '20990101_000000_abc123', '20990101_000001_def456'
        self.hermes_ledger(['hermes:' + session + '#a1b2c3d4'])
        store = self.hermes_store(self.home / 'bot/hermes/state.db', {
            session: [('user', self.ENVELOPE, None, 1700000001),
                      ('assistant', '', '[{"name":"TOOL_CALL_PRIVATE"}]', 1700000002),
                      ('tool', 'TOOL_RESULT_PRIVATE', None, 1700000003),
                      ('assistant', '확인했습니다.\n\n```\n수량 = 3\n```\n토큰 xoxb-example-secret-value', None, 1700000004)],
            other: [('user', 'OTHER_SESSION_PRIVATE', None, 1700000005)]})
        before = hashlib.sha256(store.read_bytes()).hexdigest()
        api = self.configure()
        request = api.snapshot()['requests'][0]
        result = api.conversation(self.source['id'], request['id'])
        [segment] = result['segments']
        self.assertEqual((segment['kind'], segment['requests'], segment['error'], segment['more']), ('hermes', 1, None, False))
        self.assertEqual([m['role'] for m in segment['messages']], ['user', 'assistant'])
        self.assertEqual(segment['messages'][0]['text'], '재고 수량을 알려 주세요')
        self.assertEqual(segment['messages'][0]['ts'], 1700000001)
        self.assertIn('```\n수량 = 3\n```', segment['messages'][1]['text'])
        body = json.dumps(result, ensure_ascii=False)
        for private in ('xoxb-example', 'TOOL_CALL_PRIVATE', 'TOOL_RESULT_PRIVATE', 'REASONING_PRIVATE',
                        'OTHER_SESSION_PRIVATE', session, '작업 번호', str(self.home)):
            self.assertNotIn(private, body)
        self.assertEqual(hashlib.sha256(store.read_bytes()).hexdigest(), before)
        self.assertFalse(Path(str(store) + '-wal').exists())
        self.assertFalse(Path(str(store) + '-shm').exists())

    # A Hermes gateway's framing around the person's words (synthetic, in the gateway's own markers).
    SENDER = '[예시 사용자 | Slack user <@U0EXAMPLE1>] '
    THREAD = ('[Thread context — prior messages in this thread (not yet in conversation history):]\n'
              '[assistant] 앞선 답변\n예시 동료: 앞선 말\n[End of thread context]\n\n')
    SKILL = ('[IMPORTANT: The user has invoked the "work" skill, indicating they want you to follow its '
             'instructions. The full skill content is loaded below.]\n\n# 스킬 본문\n')
    QUOTED = 'The user has provided the following instruction alongside the skill invocation: 본문 속 인용\n'

    def test_hermes_user_turns_show_only_what_the_person_typed(self):
        typed = bots._hermes_typed
        cases = [
            ('재고를 알려 주세요', '재고를 알려 주세요'),
            (self.SENDER + '재고를 알려 주세요', '재고를 알려 주세요'),
            (self.THREAD + '\n\n[New message]\n' + self.SENDER + '이어서 해 주세요', '이어서 해 주세요'),
            ('[Replying to: "예전 메시지"]\n\n' + self.SENDER + '이거 다시', '이거 다시'),
            ('[Replying to your previous message: "앞 답"]\n\n그대로 진행', '그대로 진행'),
            ("[The user sent a document: '표.pdf'. It is saved at: /tmp/x.pdf. Extract it.]\n\n요약해 주세요", '요약해 주세요'),
            ("[The user sent a document: '표.pdf'. It is saved at: /tmp/x.pdf.]\n\n", bots.HERMES_ATTACHED),
            ('[이전 채널 메시지]\n예시 동료: 안녕\n\n[New message]\n' + self.SENDER + '새 요청', '새 요청'),
            # The body may quote the instruction line: the last one is the person's.
            (self.SKILL + self.QUOTED + 'The user has provided the following instruction alongside the skill invocation: '
             '제목을 고쳐 줘\n\n[Runtime note: 합성]', '/work 제목을 고쳐 줘'),
            (self.SKILL, '/work'),
            ('[IMPORTANT: The user has invoked the "/clean /work" skill bundle, follow each.]\nUser instruction: 정리해 줘'
             '\n\n[Loaded as part of the bundle]\n본문', '/clean /work 정리해 줘'),
            # The person's own brackets, Slack links and mentions stay as typed.
            ('[긴급] 오늘 배송 확인', '[긴급] 오늘 배송 확인'),
            ('<https://example.slack.com/archives/D0EXAMPLE/p1> 이거 봐 주세요', '<https://example.slack.com/archives/D0EXAMPLE/p1> 이거 봐 주세요'),
            ('[예시 사용자] 이름표만 있는 글', '[예시 사용자] 이름표만 있는 글'),
        ]
        for stored, shown in cases:
            self.assertEqual(typed(stored), (shown, True), stored[:40])
        # A notice the gateway wrote itself is not the person's.
        self.assertIsNone(typed('[IMPORTANT: Background process 1a2b matched "done".]'))
        self.assertIsNone(typed(self.THREAD))
        # A turn longer than the read: the words are at its end, after the last block.
        head = self.THREAD[:60] + '가' * 100
        self.assertEqual(typed(head, '나' * 50 + '\n[End of thread context]\n\n\n\n[New message]\n' + self.SENDER + '끝의 말'),
                         ('끝의 말', True))
        self.assertEqual(typed(self.SKILL, '본문 끝\nThe user has provided the following instruction alongside the '
                                          'skill invocation: 긴 스킬 지시'), ('/work 긴 스킬 지시', True))
        # The person's own long text keeps its start, said to be cut (its label still off).
        self.assertEqual(typed(self.SENDER + '다' * 30, '라' * 30), ('다' * 30, False))
        self.assertEqual(typed('마' * 30, '바' * 30), ('마' * 30, False))

    def test_conversation_hides_the_gateway_framing_and_its_notices(self):
        session = '20990101_000000_abc123'
        self.hermes_ledger(['hermes:' + session])
        long_turn = self.THREAD[:-len('[End of thread context]\n\n')] + ('예시 동료: ' + '가' * 200 + '\n') * 100 + \
            '[End of thread context]\n\n\n\n[New message]\n' + self.SENDER + '긴 맥락 뒤의 말'
        self.assertGreater(len(long_turn), bots.BODY_LIMIT)
        self.hermes_store(self.home / 'bot/hermes/state.db', {session: [
            ('user', self.THREAD + '\n\n[New message]\n' + self.SENDER + '이어서 해 주세요', None, 1700000001),
            ('assistant', '네, 진행할게요.', None, 1700000002),
            ('user', '[IMPORTANT: Background process 1a2b matched "done".]', None, 1700000003),
            ('user', long_turn, None, 1700000004),
            ('user', self.SENDER + '토큰 xoxb-example-secret-value 확인', None, 1700000005)]})
        api = self.configure()
        [segment] = api.conversation(self.source['id'], api.snapshot()['requests'][0]['id'])['segments']
        shown = [(m['role'], m['text']) for m in segment['messages']]
        self.assertEqual(shown, [('user', '이어서 해 주세요'), ('assistant', '네, 진행할게요.'),
                                 ('user', '긴 맥락 뒤의 말'), ('user', '토큰 [가림] 확인')])
        for message in segment['messages']:
            self.assertNotIn('truncated', message, message['text'])
        body = json.dumps(segment, ensure_ascii=False)
        for framing in ('Slack user', 'Thread context', 'New message', '앞선 답변', 'IMPORTANT', 'U0EXAMPLE1'):
            self.assertNotIn(framing, body)

    def test_conversation_is_bounded_to_the_newest_rows_and_cut_bodies_say_so(self):
        session = '20990101_000000_abc123'
        self.hermes_ledger(['hermes:' + session])
        rows = [('user' if n % 2 else 'assistant', '메시지 %d' % n, None, 1700000000 + n) for n in range(1, 101)]
        rows.append(('assistant', '가' * (bots.BODY_LIMIT + 10), None, 1700000200))
        self.hermes_store(self.home / 'bot/hermes/state.db', {session: rows})
        api = self.configure()
        [segment] = api.conversation(self.source['id'], api.snapshot()['requests'][0]['id'])['segments']
        self.assertEqual(len(segment['messages']), bots.CONVERSATION_ROWS)
        self.assertTrue(segment['more'])
        self.assertEqual(segment['messages'][0]['text'], '메시지 22')
        last = segment['messages'][-1]
        self.assertTrue(last['truncated'])
        self.assertNotIn('fullLength', last, 'the reader cut it: its full length is not known here')
        self.assertLessEqual(len(last['text']), 4002)

    def test_gateway_session_conversation_reads_its_own_database(self):
        source = {**self.source, 'kind': 'hermes_sqlite', 'path': '.hermes/state.db'}
        path = self.database(source)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,session_key TEXT,user_id TEXT,chat_id TEXT,
                started_at REAL,ended_at REAL,origin_json TEXT);
                CREATE INDEX idx_started ON sessions(started_at);''')
            db.execute("INSERT INTO sessions VALUES('gateway-1','slack','key','user','chat',1700000000,NULL,'{}')")
        self.hermes_store(path, {'gateway-1': [('user', '안녕하세요', None, 1700000001), ('assistant', '반갑습니다', None, 1700000002)]})
        api = self.configure([source])
        row = api.snapshot()['requests'][0]
        self.assertEqual(row['conversationRef']['kind'], 'hermes')
        result = api.conversation(source['id'], row['id'])
        self.assertEqual([m['text'] for m in result['segments'][0]['messages']], ['안녕하세요', '반갑습니다'])

    def test_unreadable_conversation_is_a_segment_error_and_bad_targets_are_refused(self):
        session = '20990101_000000_abc123'
        self.hermes_ledger(['hermes:' + session])
        api = self.configure()
        request = api.snapshot()['requests'][0]
        # No database beside the ledger yet, then one without the session index.
        [segment] = api.conversation(self.source['id'], request['id'])['segments']
        self.assertEqual(segment['messages'], [])
        self.assertIn('읽지 못했습니다', segment['error'])
        store = self.home / 'bot/hermes/state.db'
        store.parent.mkdir(parents=True)
        with closing(sqlite3.connect(store)) as db, db:
            db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL)')
        self.assertIn('읽지 못했습니다', api.conversation(self.source['id'], request['id'])['segments'][0]['error'])
        for source_id, request_id in ((self.source['id'], self.source['id'] + ':' + '0' * 24), ('../x', request['id']),
                                      (self.source['id'], 'other:' + '0' * 24), (None, None)):
            with self.subTest(source_id=source_id, request_id=request_id), self.assertRaises(ValueError):
                api.conversation(source_id, request_id)
        # A source no longer configured is not read for its old requests.
        self.config.write_text(json.dumps({'version': 1, 'sources': [{**self.source, 'id': 'other'}]}))
        with self.assertRaises(ValueError):
            api.conversation(self.source['id'], request['id'])

    def test_a_ledger_without_a_bot_folder_has_no_derived_conversation_store(self):
        self.assertIsNone(bots._hermes_store(self.source))
        self.assertEqual(bots._hermes_store({**self.source, 'path': 'bot/slack/ledger.sqlite3'}), 'bot/hermes/state.db')
        self.assertIsNone(bots._hermes_store({**self.source, 'kind': 'holicii_sqlite', 'path': 'bot/slack/inbox.db'}))
        with self.assertRaisesRegex(bots.SourceError, '대화 기록'):
            bots.read_conversation(self.source, ['20990101_000000_abc123'], self.home)
        for sessions in ([], ['../escape'], ['a'] * 2, ['a'] * (bots.CONVERSATION_SESSIONS + 1), 'a'):
            with self.subTest(sessions=sessions), self.assertRaisesRegex(bots.SourceError, '식별자'):
                bots.read_conversation({**self.source, 'path': 'bot/slack/ledger.sqlite3'}, sessions, self.home)

    def test_codex_part_is_read_through_the_board_and_keeps_only_the_conversation(self):
        self.ledger()
        self.native()
        api = self.configure()
        request = api.snapshot()['requests'][0]
        read = Mock(return_value=[{'role': 'user', 'text': '요청', 'ts': 1700000001, 'attachments': ['x']},
                                  {'role': 'tool', 'text': 'TOOL_PRIVATE'},
                                  {'role': 'assistant', 'text': '답변', 'ts': 1700000002, 'truncated': True, 'fullLength': 9000}])
        [segment] = api.conversation(self.source['id'], request['id'], read_native=read)['segments']
        read.assert_called_once_with('mini', {'agent': 'codex', 'id': self.sid, 'home': '.codex'})
        self.assertEqual(segment['kind'], 'codex')
        self.assertEqual(segment['messages'], [{'role': 'user', 'text': '요청', 'ts': 1700000001},
                                               {'role': 'assistant', 'text': '답변', 'ts': 1700000002, 'truncated': True, 'fullLength': 9000}])
        read.side_effect = RuntimeError('PRIVATE_REMOTE_DETAILS')
        [segment] = api.conversation(self.source['id'], request['id'], read_native=read)['segments']
        self.assertIn('읽지 못했습니다', segment['error'])
        self.assertNotIn('PRIVATE_REMOTE_DETAILS', json.dumps(segment))

    def test_remote_conversation_is_checked_and_cannot_add_rows_or_roles(self):
        session = '20990101_000000_abc123'
        for bad in ({'conversations': [{'id': 'other', 'messages': []}]},
                    {'conversations': [{'id': session, 'messages': [{'role': 'tool', 'text': 'x'}]}]},
                    {'conversations': [{'id': session, 'messages': [{'role': 'user', 'text': 'x' * (bots.BODY_LIMIT + 1)}]}]},
                    {'conversations': [{'id': session, 'messages': [{'role': 'user'}] * (bots.CONVERSATION_ROWS + 1)}]},
                    {'conversations': [{'id': session, 'messages': []}, {'id': session, 'messages': []}]},
                    {'conversations': 'x'}, None):
            with self.subTest(bad=bad), self.assertRaises(bots.SourceError):
                bots.BotRequests._checked_conversations([session], bad)
        def listed(source, **extra):
            return {'requests': [{'id': bots._opaque(source, 1), 'sourceId': source['id'], 'host': 'mini',
                                  'nativeRef': None, **extra}]}
        holicii = {**self.source, 'kind': 'holicii_sqlite'}
        for source, extra in ((holicii, {'_conversation': session, 'conversationRef': bots._conversation_ref(holicii, session)}),
                              (self.source, {'_conversation': session, 'conversationRef': {'kind': 'hermes', 'id': self.source['id'] + ':' + '0' * 24}}),
                              (self.source, {'_conversation': '../x', 'conversationRef': bots._conversation_ref(self.source, '../x')}),
                              (self.source, {'conversationRef': bots._conversation_ref(self.source, session)})):
            with self.subTest(extra=extra), self.assertRaises(bots.SourceError):
                bots.BotRequests._checked(source, listed(source, **extra))
        rows, sessions = bots.BotRequests._checked(self.source, listed(
            self.source, _conversation=session, conversationRef=bots._conversation_ref(self.source, session)))
        self.assertEqual(sessions, {rows[0]['id']: session})
        self.assertNotIn('_conversation', rows[0])

    def test_remote_conversation_runs_the_same_stdin_reader_with_its_sessions(self):
        session = '20990101_000000_abc123'
        source = {**self.source, 'host': 'pc', 'path': 'bot/slack/ledger.sqlite3'}
        expected = {'conversations': [{'id': session, 'messages': [{'role': 'user', 'text': '요청', 'ts': 1, 'size': 2}], 'more': False}]}
        api = self.configure([source])
        api._source_cache[source['id']] = {'signature': json.dumps(source, sort_keys=True), 'source': source,
                                           'rows': [{'id': bots._opaque(source, 1), 'updatedAt': None, 'nativeRef': None}],
                                           'sessions': {bots._opaque(source, 1): session}, 'status': {}, 'failed': False,
                                           'fetched': bots.time.monotonic()}
        command = Mock(return_value=['ssh', 'example-pc', 'python-stdin'])
        done = subprocess.CompletedProcess([], 0, json.dumps(expected).encode())
        with patch.dict(sys.modules, {'server': types.SimpleNamespace(collector_command=command)}), \
                patch.object(api, 'snapshot'), patch.object(subprocess, 'run', return_value=done) as run:
            result = api.conversation(source['id'], bots._opaque(source, 1))
        self.assertEqual(result['segments'][0]['messages'][0]['text'], '요청')
        program = run.call_args.kwargs['input']
        self.assertIn(b'BOT_REQUEST = ', program)
        self.assertIn(b"BOT_CONVERSATION = ['" + session.encode() + b"']", program)
        self.assertEqual(run.call_args.kwargs['timeout'], 20)

    def test_standalone_conversation_reader_executes_with_only_stdlib(self):
        session = '20990101_000000_abc123'
        self.source = {**self.source, 'path': 'bot/slack/ledger.sqlite3'}
        self.hermes_store(self.home / 'bot/hermes/state.db', {session: [('user', '요청', None, 1700000001)]})
        program = ('BOT_REQUEST = ' + ascii(self.source) + '\nBOT_CONVERSATION = ' + ascii([session]) + '\n').encode() \
            + Path(bots.__file__).read_bytes()
        done = subprocess.run([sys.executable, '-I', '-'], input=program, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}, timeout=5)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        result = json.loads(done.stdout)
        self.assertEqual(result['conversations'][0]['messages'][0]['text'], '요청')
        self.assertEqual(done.stderr, b'')

    # Conversation keys: synthetic direct-message and thread keys only.
    def keyed_ledger(self, tasks):
        """tasks: [(codex_thread, session_key)] in receipt order, finished one second apart."""
        self.source = {**self.source, 'path': 'bot/slack/ledger.sqlite3'}
        path = self.ledger(len(tasks))
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('ALTER TABLE tasks ADD COLUMN session_key TEXT')
            for number, (thread, key) in enumerate(tasks, 1):
                db.execute('UPDATE tasks SET codex_thread=?,session_key=?,finished_at=? WHERE id=?',
                           (thread, key, 1700000100 + number, number))
        return path

    def test_requests_sharing_a_conversation_key_share_an_alias_that_hides_the_key(self):
        self.keyed_ledger([(None, 'D_EXAMPLE:dm'), (None, 'D_EXAMPLE:dm'), (None, 'C_EXAMPLE:1700000000.000100'), (None, None)])
        rows = {row['id']: row for row in bots.read_source(self.source, self.home)['requests']}
        keys = [rows[bots._opaque(self.source, number)]['conversationKey'] for number in (1, 2, 3, 4)]
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], keys[2])
        self.assertIsNone(keys[3])
        self.assertRegex(keys[0], r'\Aexample-bot:[0-9a-f]{24}\Z')
        api = self.configure()
        body = json.dumps(api.snapshot())
        for private in ('D_EXAMPLE', '1700000000.000100', ':dm'):
            self.assertNotIn(private, body)
        self.assertEqual(len({row['conversationKey'] for row in api.snapshot()['requests']}), 3)
        # A remote answer cannot name a key outside its source.
        row = {'id': bots._opaque(self.source, 1), 'sourceId': self.source['id'], 'host': 'mini', 'nativeRef': None}
        for forged in ('other:' + '0' * 24, 'D_EXAMPLE:dm', 7):
            with self.subTest(forged=forged), self.assertRaises(bots.SourceError):
                bots.BotRequests._checked(self.source, {'requests': [{**row, 'conversationKey': forged}]})

    def test_a_conversation_key_joins_its_codex_and_hermes_sessions_oldest_first(self):
        first, second, elsewhere = '20990101_000000_aaa111', '20990101_000000_bbb222', '20990101_000000_ccc333'
        self.keyed_ledger([(self.sid, 'D_EXAMPLE:dm'), (self.sid, 'D_EXAMPLE:dm'), ('hermes:' + first + '#g1', 'D_EXAMPLE:dm'),
                           ('hermes:' + second + '#g2', 'D_EXAMPLE:dm'), ('hermes:' + elsewhere + '#g3', 'D_OTHER:dm')])
        self.native()
        self.hermes_store(self.home / 'bot/hermes/state.db', {
            first: [('user', '첫 요청', None, 1700000001)], second: [('assistant', '둘째 답변', None, 1700000002)],
            elsewhere: [('user', 'ELSEWHERE_PRIVATE', None, 1700000003)]})
        api = self.configure()
        api.snapshot()
        read = Mock(return_value=[{'role': 'user', 'text': 'Codex 요청'}, {'role': 'tool', 'text': 'TOOL_PRIVATE'}])
        with patch.object(api, '_collect', wraps=api._collect) as collect:
            result = api.conversation(self.source['id'], bots._opaque(self.source, 4), read_native=read)
        self.assertEqual([(part['kind'], part['requests']) for part in result['segments']], [('codex', 2), ('hermes', 1), ('hermes', 1)])
        self.assertEqual([[message['text'] for message in part['messages']] for part in result['segments']],
                         [['Codex 요청'], ['첫 요청'], ['둘째 답변']])
        self.assertEqual([part['firstAt'] for part in result['segments']], sorted(part['firstAt'] for part in result['segments']))
        read.assert_called_once_with('mini', {'agent': 'codex', 'id': self.sid, 'home': '.codex'})
        self.assertEqual([call.args[1] for call in collect.call_args_list], [[second, first]], 'one Hermes read, newest first')
        body = json.dumps(result, ensure_ascii=False)
        for private in ('ELSEWHERE_PRIVATE', 'TOOL_PRIVATE', first, second):
            self.assertNotIn(private, body)
        # The older request of the same conversation opens the same view.
        older = api.conversation(self.source['id'], bots._opaque(self.source, 1), read_native=read)
        self.assertEqual([part['kind'] for part in older['segments']], ['codex', 'hermes', 'hermes'])
        # A full Codex window says older rows may be missing.
        read.return_value = [{'role': 'assistant', 'text': '답변'}] * bots.CONVERSATION_ROWS
        self.assertTrue(api.conversation(self.source['id'], bots._opaque(self.source, 1), read_native=read)['segments'][0]['more'])

    def test_only_the_newest_sessions_of_a_long_conversation_are_read(self):
        sessions = ['20990101_00000%d_abc123' % number for number in range(bots.CONVERSATION_SESSIONS + 2)]
        self.keyed_ledger([('hermes:' + session, 'D_EXAMPLE:dm') for session in sessions])
        self.hermes_store(self.home / 'bot/hermes/state.db',
                          {session: [('user', '요청 ' + session[-8], None, 1700000001)] for session in sessions})
        api = self.configure()
        result = api.conversation(self.source['id'], api.snapshot()['requests'][0]['id'])
        self.assertEqual([part['messages'][0]['text'] for part in result['segments']],
                         ['요청 %d' % number for number in range(2, bots.CONVERSATION_SESSIONS + 2)])

    def test_gateway_sessions_of_one_thread_share_a_key_and_are_read_together(self):
        source = {**self.source, 'kind': 'hermes_sqlite', 'path': '.hermes/state.db'}
        path = self.database(source)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,session_key TEXT,user_id TEXT,chat_id TEXT,
                started_at REAL,ended_at REAL,origin_json TEXT);
                CREATE INDEX idx_started ON sessions(started_at);''')
            for number, (name, key) in enumerate([('gateway-1', 'thread-a'), ('gateway-2', 'thread-a'), ('gateway-3', 'thread-b')]):
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)', (name, 'slack', key, 'user', 'chat', 1700000000 + number, None, '{}'))
        self.hermes_store(path, {'gateway-1': [('user', '앞 대화', None, 1700000001)],
                                 'gateway-2': [('assistant', '뒤 대화', None, 1700000002)],
                                 'gateway-3': [('user', 'OTHER_THREAD', None, 1700000003)]})
        api = self.configure([source])
        rows = {row['id']: row for row in api.snapshot()['requests']}
        one, two, three = (rows[bots._opaque(source, name)] for name in ('gateway-1', 'gateway-2', 'gateway-3'))
        self.assertEqual(one['conversationKey'], two['conversationKey'])
        self.assertNotEqual(one['conversationKey'], three['conversationKey'])
        result = api.conversation(source['id'], two['id'])
        self.assertEqual([part['messages'][0]['text'] for part in result['segments']], ['앞 대화', '뒤 대화'])
        self.assertNotIn('OTHER_THREAD', json.dumps(result, ensure_ascii=False))

    def test_missing_configuration_is_empty_and_never_discovers_unregistered_databases(self):
        with patch.object(Path, 'home', return_value=self.home):
            api = bots.BotRequests(self.hosts, self.config, self.home/'state')
        with patch.object(api, '_collect') as collect:
            self.assertEqual(api.snapshot()['sources'], [])
        collect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
