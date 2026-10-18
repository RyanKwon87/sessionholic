import copy
import gzip
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import session_history as history
import history_native as native


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.now = 100 * history.DAY
        self.source = {'host': 'fixture', 'agent': 'codex', 'home': '.codex',
                       'id': '11111111-1111-4111-8111-111111111111', 'cwd': '/fixture', 'title': '합성 대화'}
        self.export = {'messages': [{'role': 'user', 'text': '영상 검수', 'ts': 1},
                                    {'role': 'assistant', 'text': '검수 결과입니다.', 'ts': 2}],
                       'family': [self.source['id']], 'fingerprint': 'a' * 64,
                       'lastActivityAt': self.now - 20 * history.DAY, 'canDelete': True, 'sourceBytes': 90000}
        self.calls = []
        self.fail_delete = False
        def rpc(source, args):
            self.calls.append(copy.deepcopy(args))
            if args['action'] == 'export':
                return copy.deepcopy(self.export)
            if self.fail_delete:
                raise RuntimeError('response lost')
            return {'confirmed': True, 'family': self.export['family'], 'reclaimedBytes': None}
        self.store = history.Histories(self.root, rpc, clock=lambda: self.now)
        self.key = history.key(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def sweep(self, protected=None):
        self.store.sweep([self.source], protected or set())

    def row(self):
        return self.store.status()['records'][0]

    def test_verified_whole_text_then_full_grace_and_restart(self):
        self.export['messages'] = [{'role': 'user' if i % 2 == 0 else 'assistant',
                                    'text': str(i) + ' · ' + '대화' * 5000, 'ts': i} for i in range(204)]
        self.sweep()
        self.assertEqual(self.row()['state'], 'pending')
        self.assertNotIn('delete', [c['action'] for c in self.calls])
        restored = history.Histories(self.root, self.store.rpc, clock=lambda: self.now)
        messages, offset = [], 0
        while offset is not None:
            page = restored.conversation(self.key, offset)
            messages.extend(page['messages']); offset = page['nextOffset']
        self.assertEqual([m['text'] for m in messages], [m['text'] for m in self.export['messages']])
        self.now += 7 * history.DAY - 1
        self.sweep(); self.assertEqual(self.row()['state'], 'pending')
        self.now += 2
        self.sweep(); self.assertEqual(self.row()['state'], 'deleted')
        self.assertIsNone(self.row()['reclaimedBytes'])
        self.sweep(); self.assertEqual([c['action'] for c in self.calls].count('delete'), 1)

    def test_changed_history_restarts_the_grace_and_preserves_updated_text(self):
        self.sweep(); first = self.row()['deleteAfter']
        self.now += 8 * history.DAY
        self.export['fingerprint'] = 'b' * 64
        self.export['messages'].append({'role': 'user', 'text': '새 요청', 'ts': 3})
        self.sweep()
        self.assertGreater(self.row()['deleteAfter'], first)
        self.assertEqual(self.store.conversation(self.key)['messages'][-1]['text'], '새 요청')
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_reuse_and_child_protection_cancel_grace(self):
        child = '22222222-2222-4222-8222-222222222222'
        self.export['family'].append(child)
        self.sweep(); self.now += 8 * history.DAY
        self.sweep({('fixture', 'codex', '.codex', child)})
        self.assertEqual(self.row()['state'], 'waiting')
        self.sweep()
        self.assertEqual(self.row()['deleteAfter'], self.now + 7 * history.DAY)
        self.export['lastActivityAt'] = self.now
        self.sweep(); self.assertEqual(self.row()['state'], 'waiting')
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_manual_preservation_and_disabled_policy_block_deletion(self):
        self.sweep(); self.store.preserve(self.key, True)
        self.now += 100 * history.DAY
        self.sweep(); self.assertEqual(self.row()['state'], 'preserved')
        self.store.preserve(self.key, False)
        self.assertEqual(self.row()['deleteAfter'], self.now + 7 * history.DAY)
        self.store.policy({'enabled': False, 'idleDays': 14, 'graceDays': 7})
        before = len(self.calls); self.sweep(); self.assertEqual(len(self.calls), before)

    def test_preserved_child_record_blocks_parent_delete(self):
        child = {**self.source, 'id': '22222222-2222-4222-8222-222222222222'}
        self.store.sweep([child], set())
        self.store.preserve(history.key(child), True)
        self.export['family'].append(child['id'])
        self.now += 8 * history.DAY
        self.sweep()
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_failed_summary_or_archive_verification_never_deletes(self):
        self.export['messages'] = [{'role': 'user', 'text': '아직 답변 없음'}]
        self.sweep(); self.assertEqual(self.row()['state'], 'blocked')
        self.export['messages'].append({'role': 'assistant', 'text': '답변'})
        self.sweep()
        (self.store.root / (self.key + '.json.gz')).write_bytes(b'broken')
        self.now += 8 * history.DAY
        self.sweep()
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_delete_intent_is_persisted_and_response_loss_is_not_replayed(self):
        self.sweep(); self.now += 8 * history.DAY; self.fail_delete = True
        self.sweep(); self.assertEqual(self.row()['state'], 'uncertain')
        self.sweep()
        self.store.preserve(self.key, False)
        self.sweep()
        self.assertEqual([c['action'] for c in self.calls].count('delete'), 1)

    def test_no_binary_even_from_remote_text_and_no_workspace_files(self):
        binary = 'A' * 2048
        self.export['messages'][0]['text'] += '\ndata:image/png;base64,' + binary
        self.export['messages'][1]['image'] = binary
        self.sweep()
        raw = gzip.decompress((self.store.root / (self.key + '.json.gz')).read_bytes()).decode()
        self.assertNotIn(binary, raw)
        self.assertNotIn('image', self.store.conversation(self.key)['messages'][1])
        self.assertEqual((self.store.root / 'journal.json').stat().st_mode & 0o777, 0o600)

    def test_summary_uses_parent_conversation_and_not_a_childs_last_answer(self):
        self.export['messages'].extend([{'role': 'user', 'text': '하위 요청', 'threadId': 'child'},
                                        {'role': 'assistant', 'text': '하위의 다른 결과', 'threadId': 'child'}])
        self.sweep()
        self.assertEqual(self.row()['summary']['lastAnswer'], '검수 결과입니다.')
        self.assertEqual(self.row()['summary']['messageCount'], 4)

    def test_claude_history_is_archived_without_unsafe_delete(self):
        self.source.update(agent='claude', home=None)
        self.export.update(canDelete=False, reason='원본 유지')
        self.key = history.key(self.source)
        self.sweep(); self.now += 100 * history.DAY; self.sweep()
        self.assertEqual(self.row()['state'], 'archived')
        self.assertNotIn('delete', [c['action'] for c in self.calls])

    def test_paths_policy_and_symlinks_are_rejected(self):
        for value in ({'enabled': True, 'idleDays': 0, 'graceDays': 7},
                      {'enabled': True, 'idleDays': True, 'graceDays': 7}):
            with self.assertRaises(ValueError): self.store.policy(value)
        with self.assertRaises(ValueError): self.store.conversation('../outside')
        self.store.root.symlink_to(self.root / 'outside')
        with self.assertRaises(ValueError): self.store.status()


class NativeHistoryTest(unittest.TestCase):
    def codex_client(self):
        sid = '11111111-1111-4111-8111-111111111111'
        class Client:
            initialized = {'userAgent': 'codex-cli 0.163.0'}
            calls = []
            status = 'notLoaded'
            answer = '전체 답변' * 4000
            queued = []
            goal = None
            children = []
            pinned = []
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def call(self, method, params):
                self.calls.append((method, copy.deepcopy(params)))
                if method == 'thread/read':
                    return {'thread': {'id': params['threadId'], 'status': {'type': self.status}, 'updatedAt': 1,
                                       'isPinned': any(row['id'] == params['threadId'] for row in self.pinned)}}
                if method == 'thread/list':
                    if params.get('isPinned'): return {'data': self.pinned}
                    return {'data': self.children if not params['archived'] else []}
                if method == 'thread/turns/list':
                    return {'data': [{'status': 'completed', 'startedAt': 1, 'completedAt': 2, 'itemsView': 'full',
                      'items': [{'id': 'user', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '요청'},
                          {'type': 'image', 'url': 'data:image/png;base64,SECRET_IMAGE'}]},
                                {'id': 'answer', 'type': 'agentMessage', 'text': self.answer}]}]}
                if method == 'thread/queue/list': return {'data': self.queued}
                if method == 'thread/goal/get': return {'goal': self.goal}
                if method == 'thread/loaded/list': return {'data': [] if self.status == 'notLoaded' else [sid]}
                if method == 'thread/delete': return {}
                raise AssertionError(method)
        return {'agent': 'codex', 'id': sid, 'home': '.codex'}, Client()

    def test_codex_full_text_native_delete_and_activity_guards(self):
        source, client = self.codex_client()
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            self.assertEqual(result['messages'][-1]['text'], client.answer)
            self.assertNotIn('SECRET_IMAGE', json.dumps(result))
            self.assertTrue(result['canDelete'])
            self.assertTrue(all(p['sourceKinds'] == native.SOURCE_KINDS for m, p in client.calls if m == 'thread/list'))
            original = result['fingerprint']
            for attr, value in [('queued', [{'id': 'queued'}]), ('goal', {'status': 'active'}), ('goal', {'status': 'paused'}),
                                ('status', 'idle'), ('pinned', [{'id': source['id']}])]:
                old = getattr(client, attr); setattr(client, attr, value)
                with self.subTest(guard=attr), self.assertRaises(ValueError):
                    native.handle({'action': 'delete', 'source': source, 'fingerprint': original})
                setattr(client, attr, old)
            client.answer += '변경'
            with self.assertRaises(ValueError):
                native.handle({'action': 'delete', 'source': source, 'fingerprint': original})
            result = native.handle({'action': 'export', 'source': source})
            done = native.handle({'action': 'delete', 'source': source, 'fingerprint': result['fingerprint']})
            self.assertTrue(done['confirmed'])
            self.assertEqual([m for m, p in client.calls].count('thread/delete'), 1)

    def test_codex_unordered_descendants_and_unrelated_thread_refused(self):
        source, client = self.codex_client()
        client.children = [{'id': 'grandchild', 'parentThreadId': 'child'},
                           {'id': 'child', 'parentThreadId': source['id']}]
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            self.assertEqual(set(result['family']), {source['id'], 'child', 'grandchild'})
            client.children.append({'id': 'foreign', 'parentThreadId': 'other'})
            with self.assertRaises(ValueError): native.handle({'action': 'export', 'source': source})

    def test_unverified_native_version_exports_but_never_deletes(self):
        source, client = self.codex_client()
        client.initialized = {'userAgent': 'codex-cli 9.9.9'}
        with patch.object(native.native, '_client', return_value=client):
            result = native.handle({'action': 'export', 'source': source})
            self.assertTrue(result['messages'])
            self.assertFalse(result['canDelete'])
            with self.assertRaises(ValueError):
                native.handle({'action': 'delete', 'source': source, 'fingerprint': result['fingerprint']})
        self.assertNotIn('thread/delete', [m for m, p in client.calls])

    def test_archived_native_stores_must_confirm_no_queue_or_unfinished_goal(self):
        source, _ = self.codex_client()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); account = root / '.codex'; account.mkdir()
            schemas = [('state_5.sqlite', 'CREATE TABLE threads (id TEXT, archived INTEGER)',
                        'INSERT INTO threads VALUES (?,1)'),
                       ('queue_1.sqlite', 'CREATE TABLE queued_items (thread_id TEXT)', None),
                       ('goals_1.sqlite', 'CREATE TABLE thread_goals (thread_id TEXT, status TEXT)', None)]
            for filename, schema, insert in schemas:
                with sqlite3.connect(account / filename) as db:
                    db.execute(schema)
                    if insert: db.execute(insert, (source['id'],))
            self.assertTrue(native._archived_clear(source, root, source['id']))
            with sqlite3.connect(account / 'goals_1.sqlite') as db:
                db.execute('INSERT INTO thread_goals VALUES (?,?)', (source['id'], 'paused'))
            self.assertFalse(native._archived_clear(source, root, source['id']))
            with sqlite3.connect(account / 'goals_1.sqlite') as db: db.execute('DELETE FROM thread_goals')
            with sqlite3.connect(account / 'queue_1.sqlite') as db:
                db.execute('INSERT INTO queued_items VALUES (?)', (source['id'],))
            self.assertFalse(native._archived_clear(source, root, source['id']))
            (account / 'queue_1.sqlite').unlink()
            self.assertFalse(native._archived_clear(source, root, source['id']))

    def test_claude_full_transcript_and_children_omit_all_attachment_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sid = '11111111-1111-4111-8111-111111111111'
            project = root / '.claude/projects/fixture'; project.mkdir(parents=True)
            path = project / (sid + '.jsonl')
            rows = [{'type': 'user' if i % 2 == 0 else 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
                     'message': {'content': [{'type': 'text', 'text': str(i) + '원문' * 2000}]}} for i in range(202)]
            rows[0]['message']['content'].append({'type': 'image', 'source': {'type': 'base64', 'data': 'SECRET_IMAGE_BYTES'}})
            path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n')
            children = project / sid / 'subagents'; children.mkdir(parents=True)
            (children / 'agent-fixture.jsonl').write_text(json.dumps(rows[1]))
            result = native.handle({'action': 'export', 'source': {'agent': 'claude', 'id': sid, 'sessionId': sid}}, home=root)
            self.assertEqual(len(result['messages']), 203)
            self.assertIn('agent-fixture', result['family'])
            self.assertIn('[첨부: image]', result['messages'][0]['text'])
            self.assertNotIn('SECRET_IMAGE_BYTES', json.dumps(result))
            self.assertFalse(result['canDelete'])
            with self.assertRaises(ValueError):
                native.handle({'action': 'delete', 'source': {'agent': 'claude', 'id': sid}, 'fingerprint': result['fingerprint']}, home=root)

    def test_unknown_text_shape_and_malformed_history_are_not_silently_dropped(self):
        with self.assertRaises(ValueError): native.blocks([{'type': 'future_text', 'value': 'must be preserved'}])
        self.assertEqual(native.blocks('x' * 5000), 'x' * 5000, 'long ordinary text must never be guessed to be binary')
        self.assertEqual(native.blocks([{'type': 'document', 'filename': 'fixture.pdf', 'source': {'data': 'private'}}]),
                         '[첨부: document · fixture.pdf]')


if __name__ == '__main__':
    unittest.main()
