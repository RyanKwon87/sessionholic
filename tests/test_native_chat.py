import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import attachments
import collector
import native_chat as chat


QUEUE_METHODS = ('thread/queue/list', 'thread/queue/add', 'thread/queue/start', 'turn/steer')
PAGED_READS = ('thread/items/list', 'thread/turns/list')


def paged(thread, method, params):
    """app-server history pages, newest first: items with their turn id, or turns with no items."""
    turns = thread['turns']
    if method == 'thread/items/list':
        rows = [{'item': item, 'turnId': turn['id']} for turn in turns if isinstance(turn, dict) for item in turn.get('items') or []]
    else:
        # A malformed turn is passed on as it is, for the reader to reject.
        rows = [{**turn, 'items': [], 'itemsView': 'notLoaded'} if isinstance(turn, dict) else turn for turn in turns]
    rows.reverse()
    start = int(params.get('cursor') or 0)
    end = start + params['limit']
    return {'data': rows[start:end], 'nextCursor': str(end) if end < len(rows) else None}


def snapshot(thread):
    """Outcomes answering every read from one fixed snapshot, whatever the client writes later."""
    return {'thread/read': lambda params: {'thread': thread if params.get('includeTurns') else {**thread, 'turns': []}},
            **{method: (lambda params, method=method: paged(thread, method, params)) for method in PAGED_READS}}


def methods(client):
    """Calls in order, a metadata read with its history pages counted as one thread/read."""
    return [method for method, _ in client.calls if method not in PAGED_READS]


class FakeClient:
    """app-server turn/start contract: idle starts a turn, a running turn absorbs the input.

    Any other write (queue, steer, approval answers) is unexpected and fails loudly.
    """
    def __init__(self, thread, outcomes=None):
        self.thread = thread
        self.outcomes = outcomes or {}
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def call(self, method, params):
        self.calls.append((method, params))
        if method in self.outcomes:
            value = self.outcomes[method]
            if isinstance(value, Exception):
                raise value
            if callable(value):
                return value(params)
            return value
        if method == 'thread/read':
            if params.get('includeTurns'):
                return {'thread': self.thread}
            return {'thread': {**self.thread, 'turns': []}}
        if method in PAGED_READS:
            return paged(self.thread, method, params)
        if method == 'turn/start':
            item = {'type': 'userMessage', 'id': 'item-' + str(len(self.calls)),
                    'clientId': params['clientUserMessageId'], 'content': params['input']}
            running = [turn for turn in self.thread['turns'] if turn.get('status') == 'inProgress']
            if running:
                running[-1].setdefault('items', []).append(item)
                return {'turn': running[-1]}
            turn = {'id': 'turn-' + str(len(self.thread['turns']) + 1), 'status': 'inProgress',
                    'items': [item], 'startedAt': 1}
            self.thread['turns'].append(turn)
            self.thread['status'] = {'type': 'active', 'activeFlags': []}
            return {'turn': turn}
        raise AssertionError(method)


class NativeChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cwd = self.home / 'project'
        self.cwd.mkdir()
        self.source = {'agent': 'codex', 'home': '.codex-isolated',
                       'id': str(uuid.uuid4()), 'cwd': str(self.cwd)}
        self.request = 'board-test-' + uuid.uuid4().hex
        self.args = {'action': 'send', 'source': self.source, 'requestId': self.request,
                     'input': [{'type': 'text', 'text': 'fixture message'}]}

    def thread(self, kind='idle', direct=True):
        return {'id': self.source['id'], 'cwd': str(self.cwd), 'turns': [],
                'status': {'type': kind, 'activeFlags': []}, 'canAcceptDirectInput': direct,
                'source': 'cli', 'updatedAt': 1}

    def handle(self, client, args=None):
        with patch.object(chat, '_client', return_value=client) as connect:
            result = chat.handle(args or self.args, home=self.home)
        identity = connect.call_args.args[0]
        self.assertEqual(identity['home'], self.source['home'])
        self.assertEqual(identity['id'], self.source['id'])
        return result

    def writes(self, client):
        return [(method, params) for method, params in client.calls if method not in ('thread/read',) + PAGED_READS]

    def running(self, items=None):
        thread = self.thread('active')
        thread['turns'] = [{'id': 'active-1', 'status': 'inProgress', 'items': list(items or [])}]
        return thread

    def test_combined_read_receipt_reuses_one_exact_thread_snapshot(self):
        thread = self.thread()
        thread['turns'] = [{'id': 'completed-turn', 'status': 'completed', 'items': [{
            'type': 'userMessage', 'clientId': self.request, 'content': self.args['input']}]}]
        client = FakeClient(thread)
        result = self.handle(client, {'action': 'read', 'source': self.source, 'requestId': self.request})
        self.assertEqual(result['messages'][0]['clientId'], self.request)
        self.assertEqual(result['receipts'][0]['requestId'], self.request)
        self.assertEqual(result['receipts'][0]['delivery'], 'completed')
        self.assertTrue(result['receipts'][0]['readbackConfirmed'])
        self.assertEqual(methods(client), ['thread/read'])
        self.assertEqual(self.writes(client), [])

    def test_combined_receipt_matches_independent_unknown_rejected_and_acknowledged(self):
        cases = [TimeoutError(), chat.NativeRejected({'code': -32600, 'message': 'failed to prepare attachment'}), None]
        for index, error in enumerate(cases):
            with self.subTest(error=type(error).__name__):
                rid = self.request + '-' + str(index)
                args = {**self.args, 'requestId': rid}
                # None: native acknowledges turn/start, but its follow-up read still lags.
                outcomes = {'turn/start': error} if error else snapshot(self.thread())
                self.handle(FakeClient(self.thread(), outcomes=outcomes), args)
                clean = FakeClient(self.thread())
                combined = self.handle(clean, {'action': 'read', 'source': self.source, 'requestId': rid})
                standalone = self.handle(FakeClient(self.thread()), {'action': 'receipt', 'source': self.source, 'requestId': rid})
                receipt = combined['receipts'][0]
                for key in ('requestId', 'delivery', 'confirmed', 'readbackConfirmed', 'acknowledged', 'canRetry'):
                    self.assertEqual(receipt.get(key), standalone.get(key))
                self.assertEqual(receipt['delivery'], ('unknown', 'rejected', 'accepted')[index])
                self.assertEqual(combined['phase'], 'idle')
                self.assertEqual(methods(clean), ['thread/read'])
                self.assertEqual(self.writes(clean), [])
                retry = FakeClient(self.thread())
                self.handle(retry, args)
                self.assertEqual(self.writes(retry), [])

    def test_combined_receipt_matches_exact_client_id_in_running_turn(self):
        thread = self.running([{'type': 'userMessage', 'clientId': self.request, 'content': self.args['input']}])
        read = self.handle(FakeClient(thread), {'action': 'read', 'source': self.source, 'requestId': self.request})
        self.assertEqual(read['receipts'][0]['delivery'], 'accepted')
        self.assertEqual(read['receipts'][0]['turnId'], 'active-1')
        self.assertTrue(read['receipts'][0]['readbackConfirmed'])
        self.assertNotIn('queueId', read['receipts'][0])
        other = self.handle(FakeClient(thread), {
            'action': 'read', 'source': self.source, 'requestId': self.request + '-other'})
        self.assertEqual(other['receipts'][0]['delivery'], 'unknown')
        self.assertFalse(other['receipts'][0]['confirmed'])
        for status in ('completed', 'interrupted', 'failed'):
            with self.subTest(status=status):
                thread['turns'][0]['status'] = status
                thread['status'] = {'type': 'idle', 'activeFlags': []}
                read = self.handle(FakeClient(thread), {'action': 'read', 'source': self.source, 'requestId': self.request})
                self.assertEqual(read['receipts'][0]['delivery'], status)

    def test_invalid_combined_request_id_is_rejected_before_native_connection(self):
        with patch.object(chat, '_client') as connect:
            with self.assertRaises(ValueError):
                chat.handle({'action': 'read', 'source': self.source, 'requestId': 'x'}, home=self.home)
        connect.assert_not_called()

    def test_read_has_no_queue_fields_and_hides_native_image_sources(self):
        thread = self.running([{'type': 'userMessage', 'clientId': self.request, 'content': [
            {'type': 'text', 'text': '  본문\n둘째 줄  '},
            {'type': 'image', 'url': 'data:image/png;base64,PRIVATE'},
            {'type': 'localImage', 'path': '/example/private/image.png'}]}])
        client = FakeClient(thread)
        result = self.handle(client, {'action': 'read', 'source': self.source})
        for key in ('queue', 'queuedMessages', 'queueState'):
            self.assertNotIn(key, result)
        self.assertEqual(set(result['capabilities']), {'canRead', 'canSend', 'canAttachImages', 'canAnswerApprovals',
                                                       'canAnswerQuestions', 'reason', 'fallback'})
        self.assertTrue(result['capabilities']['canSend'])
        self.assertEqual(result['phase'], 'working')
        self.assertEqual(result['activeTurnId'], 'active-1')
        self.assertIn('둘째 줄', result['messages'][0]['text'])
        self.assertEqual([a['source'] for a in result['messages'][0]['attachments']], ['native', 'native'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('/example/private', json.dumps(result))
        self.assertEqual(methods(client), ['thread/read'])
        self.assertFalse((self.home / '.sessionholic').exists())

    def test_joined_input_recovers_proven_attachment_metadata_without_loading_files(self):
        image = attachments.store(str(self.cwd), 'picture.png', b'\x89PNG\r\n\x1a\nfixture')
        file = attachments.store(str(self.cwd), 'document.txt', b'PRIVATE-FILE-CONTENT')
        client = FakeClient(self.running())
        sent = self.handle(client, {**self.args, 'attachmentIds': [image['id'], file['id']]})
        self.assertEqual(sent['turnId'], 'active-1')
        with patch.object(attachments, 'resolve', side_effect=AssertionError('history read must not read files')):
            read = self.handle(FakeClient(client.thread), {'action': 'read', 'source': self.source})
        row = next(message for message in read['messages'] if message.get('clientId') == self.request)
        self.assertEqual(row['attachments'], sent['attachments'])
        self.assertNotIn('PRIVATE-FILE-CONTENT', json.dumps(read))
        self.assertNotIn(str(self.cwd), json.dumps(row['attachments']))

    def test_batch_receipts_reuse_one_snapshot_beyond_display_window(self):
        completed, unknown, running = self.request + '-done', self.request + '-unknown', self.request + '-running'
        thread = self.thread('active')
        thread['turns'] = [{'id': 'completed-turn', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'clientId': completed, 'content': self.args['input']},
            *[{'type': 'agentMessage', 'text': str(i)} for i in range(100)]]},
            {'id': 'active-1', 'status': 'inProgress', 'items': [
                {'type': 'userMessage', 'clientId': running, 'content': self.args['input']}]}]
        client = FakeClient(thread)
        read = self.handle(client, {'action': 'read', 'source': self.source, 'requestId': completed,
                                    'requestIds': [completed, unknown, running]})
        self.assertEqual([r['delivery'] for r in read['receipts']], ['completed', 'unknown', 'accepted'])
        self.assertEqual([r['turnId'] for r in read['receipts']], ['completed-turn', None, 'active-1'])
        self.assertNotIn(completed, [m.get('clientId') for m in read['messages']])
        for key in ('queue', 'queuedMessages', 'queueState'):
            self.assertNotIn(key, read)
        self.assertEqual(methods(client), ['thread/read'])
        for ids in ([self.request] * 21, ['invalid'], None):
            with patch.object(chat, '_client') as connect:
                with self.assertRaises(ValueError):
                    chat.handle({'action': 'read', 'source': self.source, 'requestIds': ids}, home=self.home)
                connect.assert_not_called()

    def test_acknowledged_send_missing_from_later_snapshot_is_not_fresh_readback(self):
        client = FakeClient(self.thread())
        sent = self.handle(client)
        self.assertTrue(sent['readbackConfirmed'])
        clean = FakeClient(self.thread())
        read = self.handle(clean, {'action': 'read', 'source': self.source, 'requestIds': [self.request]})
        receipt = read['receipts'][0]
        self.assertTrue(receipt['confirmed'])
        self.assertEqual(receipt['delivery'], 'accepted')
        self.assertFalse(receipt['currentSnapshotConfirmed'])
        self.assertFalse(receipt['readbackConfirmed'])
        self.assertEqual(self.writes(clean), [])

    def test_send_and_read_never_consult_native_queue_or_steer(self):
        failing = collector.RpcError('fixture queue unavailable')
        outcomes = {method: failing for method in QUEUE_METHODS}
        client = FakeClient(self.thread(), outcomes=outcomes)
        sent = self.handle(client)
        self.assertEqual(sent['delivery'], 'accepted')
        self.assertTrue(sent['readbackConfirmed'])
        reader = FakeClient(client.thread, outcomes=outcomes)
        read = self.handle(reader, {'action': 'read', 'source': self.source, 'requestId': self.request})
        self.assertEqual(read['receipts'][0]['delivery'], 'accepted')
        for key in ('queue', 'queuedMessages', 'queueState'):
            self.assertNotIn(key, read)
        self.assertEqual(methods(client), ['thread/read', 'turn/start', 'thread/read'])
        self.assertEqual(methods(reader), ['thread/read'])

    def test_unknown_turn_status_keeps_conversation_readable_but_locks_input(self):
        thread = self.thread()
        thread['turns'] = [{'id': 'future-turn', 'status': 'pausedForReview', 'items': [{
            'type': 'userMessage', 'clientId': self.request, 'content': self.args['input']}]}]
        read = self.handle(FakeClient(thread), {'action': 'read', 'source': self.source, 'requestId': self.request})
        self.assertEqual(read['messages'][0]['text'], 'fixture message')
        self.assertEqual(read['messages'][0]['turnStatus'], 'pausedForReview')
        self.assertEqual(read['phase'], 'unknown')
        self.assertFalse(read['capabilities']['canSend'])
        self.assertFalse(read['capabilities']['canAttachImages'])
        self.assertIn('새 실행 상태', read['capabilities']['reason'])
        # The user item itself was received even though the board cannot name the new state.
        self.assertEqual(read['receipts'][0]['delivery'], 'accepted')
        client = FakeClient(thread)
        sent = self.handle(client, {**self.args, 'requestId': self.request + '-new'})
        self.assertEqual(sent['delivery'], 'rejected')
        self.assertEqual(sent['reason'], read['capabilities']['reason'])
        self.assertEqual(self.writes(client), [])
        for turns in ([{'id': 'x', 'status': 5, 'items': []}], [{'id': 'x', 'items': []}], ['not-a-turn']):
            with self.subTest(turns=turns):
                bad = self.thread()
                bad['turns'] = turns
                with self.assertRaises(ValueError):
                    self.handle(FakeClient(bad), {'action': 'read', 'source': self.source})

    def test_attachment_manifest_survives_read_and_does_not_resolve_files_again(self):
        image = attachments.store(str(self.cwd), 'picture.png', b'\x89PNG\r\n\x1a\nfixture')
        file = attachments.store(str(self.cwd), 'document.txt', b'FILE-CONTENT-PRIVATE')
        client = FakeClient(self.thread())
        sent = self.handle(client, {**self.args, 'attachmentIds': [image['id'], file['id']]})
        expected = sent['attachments']
        self.assertEqual([a['name'] for a in expected], ['picture.png', 'document.txt'])
        self.assertEqual([a['kind'] for a in expected], ['image', 'file'])
        with patch.object(attachments, 'resolve', side_effect=AssertionError('history must not load attachments')):
            read = self.handle(FakeClient(client.thread), {'action': 'read', 'source': self.source})
            receipt = self.handle(FakeClient(client.thread), {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertEqual(read['messages'][0]['attachments'], expected)
        self.assertEqual(receipt['attachments'], expected)
        self.assertTrue(receipt['readbackConfirmed'])
        records = list((self.home / '.sessionholic/chat-receipts').glob('*.json'))
        self.assertNotIn(str(self.cwd), records[0].read_text())
        self.assertNotIn('FILE-CONTENT-PRIVATE', records[0].read_text())
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)

    def test_unknown_attachment_receipt_is_not_history_until_exact_client_readback(self):
        file = attachments.store(str(self.cwd), 'document.txt', b'fixture')
        client = FakeClient(self.thread(), outcomes={'turn/start': TimeoutError()})
        args = {**self.args, 'attachmentIds': [file['id']]}
        sent = self.handle(client, args)
        self.assertEqual(sent['delivery'], 'unknown')
        self.assertFalse(sent['confirmed'])
        clean = FakeClient(self.thread())
        self.assertEqual(self.handle(clean, {'action': 'read', 'source': self.source})['messages'], [])
        receipt = self.handle(clean, {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertEqual(receipt['attachments'], sent['attachments'])
        submitted = next(params['input'] for method, params in client.calls if method == 'turn/start')
        same_text = {'type': 'userMessage', 'clientId': self.request + '-different', 'content': submitted}
        exact = {**same_text, 'clientId': self.request}
        text_only = {**exact, 'content': self.args['input']}
        clean.thread['turns'] = [{'id': 'same-turn', 'status': 'completed', 'items': [same_text, text_only, exact]}]
        rows = self.handle(clean, {'action': 'read', 'source': self.source})['messages']
        self.assertNotIn('attachments', rows[0])
        self.assertNotIn('attachments', rows[1])
        self.assertEqual(rows[2]['attachments'], sent['attachments'])
        self.assertEqual(self.writes(clean), [])

    def test_legacy_mixed_image_read_is_read_only_and_malformed_manifest_falls_back(self):
        content = self.args['input'] + [{'type': 'localImage', 'path': '/example/private/image.png'}]
        thread = self.thread()
        thread['turns'] = [{'id': 'legacy-turn', 'status': 'completed', 'items': [{'type': 'userMessage', 'clientId': self.request, 'content': content}]}]
        rows = self.handle(FakeClient(thread), {'action': 'read', 'source': self.source})['messages']
        self.assertEqual(rows[0]['text'], 'fixture message')
        self.assertEqual(rows[0]['attachments'][0]['source'], 'native')
        self.assertFalse((self.home / '.sessionholic').exists())
        identity = {k: self.source[k] for k in ('agent', 'home', 'id')}
        with chat._Ledger(identity, self.request, self.home) as ledger:
            ledger.save({'attachments': [{'path': '/example/private/bad', 'name': 'wrong'}]})
        before = sorted((self.home / '.sessionholic/chat-receipts').iterdir())
        rows = self.handle(FakeClient(thread), {'action': 'read', 'source': self.source})['messages']
        self.assertEqual(rows[0]['attachments'][0]['source'], 'native')
        self.assertNotIn('private', json.dumps(rows))
        self.assertEqual(sorted((self.home / '.sessionholic/chat-receipts').iterdir()), before)

    def test_attachment_chips_require_each_exact_native_input_not_similar_paths(self):
        first = attachments.store(str(self.cwd), 'first.txt', b'first fixture')
        second = attachments.store(str(self.cwd), 'second.txt', b'second fixture')
        client = FakeClient(self.thread())
        sent = self.handle(client, {**self.args, 'attachmentIds': [first['id'], second['id']]})
        submitted = client.thread['turns'][0]['items'][0]['content']
        item = client.thread['turns'][0]['items'][0]
        # One retained marker proves only that file, never the other attempted file.
        item['content'] = submitted[:2]
        read = self.handle(FakeClient(client.thread), {'action': 'read', 'source': self.source})
        self.assertEqual([a['id'] for a in read['messages'][0]['attachments']], [first['id']])
        # A marker appearing inside another text block is not an exact input.
        item['content'] = [{'type': 'text', 'text': 'quoted example: ' + submitted[1]['text']}]
        read = self.handle(FakeClient(client.thread), {'action': 'read', 'source': self.source})
        self.assertNotIn('attachments', read['messages'][0])
        receipt = self.handle(FakeClient(client.thread), {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertEqual(receipt['attachments'], sent['attachments'])

    def test_image_named_chip_needs_exact_local_image_and_legacy_proof_is_optional(self):
        image = attachments.store(str(self.cwd), 'picture.png', b'\x89PNG\r\n\x1a\nfixture')
        client = FakeClient(self.thread())
        self.handle(client, {**self.args, 'attachmentIds': [image['id']]})
        item = client.thread['turns'][0]['items'][0]
        original = list(item['content'])
        for unrelated in ({'type': 'localImage', 'path': '/example/other/image.png'},
                          {'type': 'image', 'url': 'https://example.com/image.png'}):
            with self.subTest(unrelated=unrelated['type']):
                item['content'] = self.args['input'] + [unrelated]
                read = self.handle(FakeClient(client.thread), {'action': 'read', 'source': self.source})
                self.assertEqual(read['messages'][0]['attachments'][0]['source'], 'native')
                self.assertIsNone(read['messages'][0]['attachments'][0]['id'])
        # Old records without input evidence remain readable receipts, but cannot name an image.
        record_path = next((self.home / '.sessionholic/chat-receipts').glob('*.json'))
        record = json.loads(record_path.read_text())
        record.pop('attachmentEvidence')
        record_path.write_text(json.dumps(record))
        item['content'] = original
        read = self.handle(FakeClient(client.thread), {'action': 'read', 'source': self.source})
        self.assertEqual(read['messages'][0]['attachments'][0]['source'], 'native')
        receipt = self.handle(FakeClient(client.thread), {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertEqual(receipt['attachments'][0]['id'], image['id'])

    def test_duplicate_user_text_cannot_stand_in_for_missing_attachment_input(self):
        file = attachments.store(str(self.cwd), 'document.txt', b'fixture')
        marker = {'type': 'text', 'text': '첨부 파일: ' + json.dumps(file['path'], ensure_ascii=False)}
        client = FakeClient(self.thread())
        self.handle(client, {**self.args, 'input': [marker], 'attachmentIds': [file['id']]})
        item = client.thread['turns'][0]['items'][0]
        self.assertEqual(item['content'], [marker, marker])
        self.assertEqual(self.handle(FakeClient(client.thread), {'action': 'read', 'source': self.source})['messages'][0]['attachments'][0]['id'], file['id'])
        item['content'] = [marker]
        self.assertNotIn('attachments', self.handle(FakeClient(client.thread), {'action': 'read', 'source': self.source})['messages'][0])

    def test_final_ledger_save_failure_after_acceptance_reports_pending_without_raising(self):
        file = attachments.store(str(self.cwd), 'document.txt', b'fixture')
        client = FakeClient(self.thread())
        args = {**self.args, 'attachmentIds': [file['id']]}
        original = chat._Ledger.save
        count = [0]
        def save(ledger, value):
            count[0] += 1
            if count[0] == 2:
                raise OSError('fixture disk failure after native acceptance')
            return original(ledger, value)
        with patch.object(chat._Ledger, 'save', save):
            sent = self.handle(client, args)
        self.assertEqual(count[0], 2)
        self.assertIs(sent['ledgerPending'], True)
        self.assertTrue(sent['acknowledged'])
        self.assertTrue(sent['confirmed'])
        self.assertEqual(sent['delivery'], 'accepted')
        self.assertEqual(sent['attachments'][0]['id'], file['id'])
        self.assertEqual([m for m, _ in self.writes(client)], ['turn/start'])
        fresh = FakeClient(client.thread)
        receipt = self.handle(fresh, {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertTrue(receipt['confirmed'])
        self.assertEqual(receipt['attachments'][0]['id'], file['id'])
        self.assertEqual(self.writes(fresh), [])
        # The pre-send reservation still forbids a resend while native history lags.
        lagging = FakeClient(self.thread())
        again = self.handle(lagging, args)
        self.assertEqual(again['delivery'], 'unknown')
        self.assertEqual(self.writes(lagging), [])

    def test_metadata_lookups_are_bounded_to_display_window(self):
        thread = self.thread()
        thread['turns'] = [{'id': str(index), 'status': 'completed', 'items': [{
            'type': 'userMessage', 'clientId': 'fixture-request-' + str(index),
            'content': [{'type': 'text', 'text': str(index)}]}]} for index in range(200)]
        with patch.object(chat, '_history_attachments', return_value=[]) as lookup:
            rows = self.handle(FakeClient(thread), {'action': 'read', 'source': self.source})['messages']
        self.assertEqual(lookup.call_count, collector.MESSAGE_LIMIT)
        self.assertEqual([r['text'] for r in rows], [str(i) for i in range(200 - collector.MESSAGE_LIMIT, 200)])

    def test_idle_send_starts_new_turn_with_exact_client_id(self):
        client = FakeClient(self.thread())
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'accepted')
        self.assertTrue(result['acknowledged'])
        self.assertTrue(result['readbackConfirmed'])
        self.assertEqual(result['turnId'], 'turn-1')
        self.assertEqual(result['phase'], 'working')
        self.assertNotIn('queueId', result)
        self.assertEqual([m for m, _ in self.writes(client)], ['turn/start'])
        params = self.writes(client)[0][1]
        self.assertEqual(set(params), {'threadId', 'input', 'clientUserMessageId'})
        self.assertEqual(params['threadId'], self.source['id'])
        self.assertEqual(params['clientUserMessageId'], self.request)
        self.assertEqual(params['input'], self.args['input'])
        self.assertEqual(len(client.thread['turns']), 1)

    def test_running_turn_send_joins_same_turn_with_one_turn_start(self):
        thread = self.running([{'type': 'userMessage', 'clientId': 'earlier-request-1',
                                'content': [{'type': 'text', 'text': 'earlier'}]}])
        client = FakeClient(thread)
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'accepted')
        self.assertTrue(result['acknowledged'])
        self.assertTrue(result['readbackConfirmed'])
        self.assertEqual(result['turnId'], 'active-1')
        self.assertEqual(result['phase'], 'working')
        self.assertEqual([m for m, _ in self.writes(client)], ['turn/start'])
        self.assertEqual(set(self.writes(client)[0][1]), {'threadId', 'input', 'clientUserMessageId'})
        self.assertEqual(len(thread['turns']), 1)
        self.assertEqual([item['clientId'] for item in thread['turns'][0]['items']], ['earlier-request-1', self.request])
        read = self.handle(FakeClient(thread), {'action': 'read', 'source': self.source})
        self.assertEqual([m['text'] for m in read['messages']], ['earlier', 'fixture message'])
        self.assertEqual({m['turnId'] for m in read['messages']}, {'active-1'})

    def test_not_steerable_running_turn_is_rejected_once_and_never_resent(self):
        errors = [{'code': -32600, 'message': 'private fixture /review',
                   'data': {'codexErrorInfo': {'activeTurnNotSteerable': {'turnKind': 'review'}}}},
                  {'code': -32600, 'message': 'Active turn is not steerable: private fixture'}]
        for index, error in enumerate(errors):
            with self.subTest(index=index):
                rid = self.request + '-steer-' + str(index)
                args = {**self.args, 'requestId': rid}
                thread = self.running()
                client = FakeClient(thread, outcomes={'turn/start': chat.NativeRejected(error)})
                result = self.handle(client, args)
                self.assertEqual(result['delivery'], 'rejected')
                self.assertEqual(result['reason'], chat.NOT_STEERABLE)
                self.assertEqual(result['nativeErrorCategory'], 'notSteerable')
                self.assertFalse(result['acknowledged'])
                self.assertFalse(result['confirmed'])
                self.assertFalse(result['canRetry'])
                self.assertNotIn('private fixture', json.dumps(result))
                self.assertEqual([m for m, _ in self.writes(client)], ['turn/start'])
                self.assertEqual(thread['turns'][0]['items'], [])
                clean = FakeClient(thread)
                self.assertEqual(self.handle(clean, args)['delivery'], 'rejected')
                receipt = self.handle(clean, {'action': 'receipt', 'source': self.source, 'requestId': rid})
                self.assertEqual(receipt['delivery'], 'rejected')
                self.assertEqual(receipt['reason'], chat.NOT_STEERABLE)
                self.assertEqual(self.writes(clean), [])

    def test_native_rejection_categories_never_echo_native_text(self):
        cases = [({'code': -32600, 'message': 'private', 'data': {'kind': 'activeTurnNotSteerable'}}, 'notSteerable'),
                 ({'code': -32600, 'message': 'Turn is NOT STEERABLE private'}, 'notSteerable'),
                 ({'code': -32602, 'message': 'failed to prepare attachment private'}, 'attachment'),
                 ({'code': -32602, 'message': 'input is too large private'}, 'inputTooLarge'),
                 ({'code': 'not-int', 'message': 'private', 'data': ['other']}, 'nativeRejected')]
        for error, category in cases:
            with self.subTest(category=category, error=error):
                rejected = chat.NativeRejected(error)
                self.assertEqual(rejected.category, category)
                self.assertNotIn('private', str(rejected))
        self.assertIsNone(chat.NativeRejected({'code': 'not-int'}).code)

    def test_legacy_mode_fields_never_select_steer_or_expected_turn(self):
        client = FakeClient(self.running())
        result = self.handle(client, {**self.args, 'mode': 'steer', 'expectedTurnId': 'wrong-turn'})
        self.assertEqual(result['delivery'], 'accepted')
        self.assertEqual(result['turnId'], 'active-1')
        self.assertEqual([m for m, _ in self.writes(client)], ['turn/start'])
        self.assertNotIn('expectedTurnId', self.writes(client)[0][1])
        # The digest covers only the input, so the same request with other legacy fields stays read-only.
        again = FakeClient(self.running())
        self.assertEqual(self.handle(again, {**self.args, 'mode': 'queue'})['delivery'], 'accepted')
        self.assertEqual(self.writes(again), [])

    def test_unknown_delivery_stays_read_only_on_repeated_request(self):
        client = FakeClient(self.thread(), outcomes={'turn/start': TimeoutError()})
        first = self.handle(client)
        self.assertEqual(first['delivery'], 'unknown')
        self.assertFalse(first['canRetry'])
        clean = FakeClient(self.thread())
        second = self.handle(clean)
        self.assertEqual(second['delivery'], 'unknown')
        self.assertEqual(self.writes(clean), [])
        records = list((self.home / '.sessionholic/chat-receipts').glob('*.json'))
        self.assertEqual(len(records), 1)
        self.assertNotIn('fixture message', records[0].read_text())
        self.assertNotIn(str(self.cwd), records[0].read_text())

    def test_explicit_native_attachment_rejection_is_preserved_without_retry(self):
        error = chat.NativeRejected({'code': -32600, 'message': 'failed to prepare attachment: fixture private path'})
        client = FakeClient(self.thread(), outcomes={'turn/start': error})
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'rejected')
        self.assertEqual(result['nativeErrorCode'], -32600)
        self.assertEqual(result['nativeErrorCategory'], 'attachment')
        self.assertNotIn('fixture private path', json.dumps(result))
        clean = FakeClient(self.thread())
        self.assertEqual(self.handle(clean)['delivery'], 'rejected')
        receipt = self.handle(clean, {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertEqual(receipt['delivery'], 'rejected')
        self.assertEqual(self.writes(clean), [])

    def test_acknowledged_receipt_survives_native_history_lag_without_retry(self):
        # The follow-up read is a static snapshot that does not yet contain the input.
        client = FakeClient(self.thread(), outcomes=snapshot(self.thread()))
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'accepted')
        self.assertTrue(result['acknowledged'])
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['readbackConfirmed'])
        self.assertEqual(result['turnId'], 'turn-1')
        self.assertEqual([m for m, _ in self.writes(client)], ['turn/start'])
        clean = FakeClient(self.thread('active'))
        receipt = self.handle(clean, {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertTrue(receipt['confirmed'])
        self.assertFalse(receipt['readbackConfirmed'])
        self.assertEqual(self.writes(clean), [])
        retry = FakeClient(self.thread())
        self.assertEqual(self.handle(retry)['delivery'], 'accepted')
        self.assertEqual(self.writes(retry), [])

    def test_same_request_different_content_is_rejected(self):
        self.handle(FakeClient(self.thread()))
        altered = {**self.args, 'input': [{'type': 'text', 'text': 'other fixture'}]}
        client = FakeClient(self.thread())
        with self.assertRaises(ValueError):
            self.handle(client, altered)
        self.assertEqual(self.writes(client), [])

    def test_existing_native_history_for_request_prevents_mutation(self):
        thread = self.running([{'type': 'userMessage', 'clientId': self.request, 'content': []}])
        client = FakeClient(thread)
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'accepted')
        self.assertEqual(result['turnId'], 'active-1')
        self.assertEqual(self.writes(client), [])
        thread['turns'] = [{'id': 'prior-turn', 'status': 'completed', 'items': [{
            'type': 'userMessage', 'clientId': self.request, 'content': []}]}]
        client = FakeClient(thread)
        self.assertEqual(self.handle(client)['delivery'], 'completed')
        self.assertEqual(self.writes(client), [])

    def test_read_returns_message_turn_status_and_cwd(self):
        thread = self.thread('active')
        thread['turns'] = [{'id': 't', 'status': 'inProgress', 'items': [{
            'type': 'userMessage', 'id': 'i', 'clientId': self.request,
            'content': self.args['input']}]}]
        client = FakeClient(thread)
        result = self.handle(client, {'action': 'read', 'source': self.source})
        self.assertEqual(result['cwd'], str(self.cwd))
        self.assertEqual(result['messages'][0]['clientId'], self.request)
        self.assertEqual(result['messages'][0]['turnStatus'], 'inProgress')
        self.assertEqual(self.writes(client), [])

    def test_exact_identity_profile_and_thread_are_required(self):
        for change in ({'id': '../other'}, {'home': '.codex/../other'}):
            with self.assertRaises(ValueError):
                chat.handle({**self.args, 'source': {**self.source, **change}}, home=self.home)
        thread = self.thread()
        thread['id'] = str(uuid.uuid4())
        client = FakeClient(thread)
        with self.assertRaises(ValueError):
            self.handle(client)
        self.assertEqual(self.writes(client), [])

    def test_unloaded_missing_direct_input_and_vendor_remote_fail_closed(self):
        for kind, direct, source in [('notLoaded', True, 'cli'), ('idle', None, 'cli'),
                                     ('idle', False, 'cli'), ('idle', True, {'custom': 'vendorRemote'})]:
            thread = self.thread(kind, direct)
            thread['source'] = source
            client = FakeClient(thread)
            result = self.handle(client)
            self.assertFalse(result['capabilities']['canSend'])
            self.assertEqual(result['delivery'], 'rejected')
            self.assertEqual(self.writes(client), [])

    def test_stale_cwd_never_submits(self):
        client = FakeClient(self.thread())
        args = {**self.args, 'source': {**self.source, 'cwd': str(self.home)}}
        with self.assertRaises(ValueError):
            self.handle(client, args)
        self.assertEqual(self.writes(client), [])

    def test_attachments_resolve_by_id_on_actual_host_and_use_native_images(self):
        image = attachments.store(str(self.cwd), 'picture.png', b'\x89PNG\r\n\x1a\nfixture')
        file = attachments.store(str(self.cwd), 'document.txt', b'file fixture')
        client = FakeClient(self.thread())
        result = self.handle(client, {**self.args, 'attachmentIds': [image['id'], file['id']]})
        self.assertTrue(result['readbackConfirmed'])
        inputs = self.writes(client)[0][1]['input']
        self.assertEqual(inputs[1], {'type': 'localImage', 'path': image['path']})
        self.assertEqual(inputs[2]['type'], 'text')
        self.assertIn(json.dumps(file['path']), inputs[2]['text'])

    def test_missing_or_tampered_attachment_never_submits(self):
        client = FakeClient(self.thread())
        with self.assertRaises(ValueError):
            self.handle(client, {**self.args, 'attachmentIds': ['f' * 32]})
        self.assertEqual(self.writes(client), [])
        file = attachments.store(str(self.cwd), 'doc.txt', b'fixture')
        os.chmod(file['path'], 0o600)
        Path(file['path']).write_text('tampered')
        with self.assertRaises(ValueError):
            self.handle(client, {**self.args, 'attachmentIds': [file['id']]})
        self.assertEqual(self.writes(client), [])

    def test_attachment_only_message_is_supported(self):
        file = attachments.store(str(self.cwd), 'doc.txt', b'fixture')
        client = FakeClient(self.thread())
        result = self.handle(client, {**self.args, 'input': [], 'attachmentIds': [file['id']]})
        self.assertTrue(result['readbackConfirmed'])
        self.assertEqual(len(self.writes(client)[0][1]['input']), 1)

    def test_rpc_notification_request_does_not_answer_approval_or_echo_error(self):
        client = object.__new__(chat.ChatAppServer)
        client.next_id, client.native_requests = 0, []
        messages = iter([json.dumps({'id': 51, 'method': 'item/commandExecution/requestApproval',
                                    'params': {'command': 'private fixture'}}).encode(),
                         json.dumps({'id': 1, 'error': {'message': 'private fixture'}}).encode()])
        with patch.object(client, '_send') as send, patch.object(client, '_message', side_effect=lambda: next(messages)):
            with self.assertRaises(collector.RpcError) as error:
                client.call('initialize', {'clientInfo': {'name': 'fixture'}})
        self.assertNotIn('private fixture', str(error.exception))
        self.assertEqual(send.call_count, 1)
        sent = json.loads(send.call_args.args[1])
        self.assertTrue(sent['params']['capabilities']['experimentalApi'])
        self.assertEqual(client.native_requests, ['item/commandExecution/requestApproval'])

    def test_board_claude_uuid_binding_requires_unique_interactive_session_and_cwd(self):
        sid = str(uuid.uuid4())
        source = {'agent': 'claude', 'home': '.claude', 'id': sid, 'sessionId': sid,
                  'kind': 'interactive', 'cwd': str(self.cwd)}
        folder = self.home / '.claude/projects/p'
        folder.mkdir(parents=True)
        (folder / (sid + '.jsonl')).write_text(json.dumps({'type': 'user', 'message': {'content': 'fixture'}}) + '\n')
        row = {'id': 'tty-other123', 'sessionId': sid, 'kind': 'interactive',
               'state': 'idle', 'status': 'idle', 'cwd': str(self.cwd)}
        with patch.object(chat.native, '_claude_binary', return_value='/fixture/claude'), \
                patch.object(chat.native, '_claude_run', return_value=json.dumps([row]).encode()):
            result = chat.handle({'action': 'read', 'source': source}, home=self.home)
            self.assertEqual(result['messages'][0]['text'], 'fixture')
            self.assertEqual(result['cwd'], str(self.cwd))
            for changed in ({'sessionId': 'other'}, {'cwd': str(self.home)}, {'kind': 'background'}):
                with self.assertRaises(ValueError):
                    chat.handle({'action': 'read', 'source': {**source, **changed}}, home=self.home)
        with patch.object(chat.native, '_claude_binary', return_value='/fixture/claude'), \
                patch.object(chat.native, '_claude_run', return_value=json.dumps([row, {**row, 'id': 'duplicate'}]).encode()):
            with self.assertRaises(ValueError):
                chat.handle({'action': 'read', 'source': source}, home=self.home)

    def test_claude_exact_background_read_maps_id_to_session_not_resume(self):
        source = {'agent': 'claude', 'home': '.claude', 'id': 'back1234'}
        sid = 'session-12345'
        folder = self.home / '.claude/projects/p'
        folder.mkdir(parents=True)
        (folder / (sid + '.jsonl')).write_text(json.dumps({'type': 'user', 'message': {'content': 'fixture'}}) + '\n')
        row = {'id': source['id'], 'sessionId': sid, 'kind': 'background',
               'state': 'idle', 'status': 'idle', 'cwd': str(self.cwd)}
        with patch.object(chat.native, '_claude_binary', return_value='/fixture/claude'), \
                patch.object(chat.native, '_claude_run', return_value=json.dumps([row]).encode()) as run:
            read = chat.handle({'action': 'read', 'source': source}, home=self.home)
            combined = chat.handle({'action': 'read', 'source': source, 'requestId': self.request}, home=self.home)
            capabilities = chat.handle({'action': 'capabilities', 'source': source}, home=self.home)
            send = chat.handle({**self.args, 'source': source}, home=self.home)
        self.assertEqual(read['cwd'], str(self.cwd))
        self.assertEqual(read['messages'][0]['text'], 'fixture')
        # The owning host never types into Claude; the board's own terminal does.
        self.assertFalse(read['capabilities']['canSend'])
        self.assertEqual(read['capabilities']['reason'], 'Claude 입력은 보드가 연결한 터미널로 전달합니다.')
        self.assertTrue(read['requiresNativeTerminal'])
        for key in ('canQueue', 'canSteer'):
            self.assertNotIn(key, read['capabilities'])
        for key in ('receipts', 'queue', 'queuedMessages', 'queueState'):
            self.assertNotIn(key, read)
            self.assertNotIn(key, combined)
        self.assertEqual(combined['messages'], read['messages'])
        self.assertEqual(capabilities['phase'], read['phase'])
        self.assertEqual(capabilities['kind'], 'background')
        self.assertNotIn('messages', capabilities)
        self.assertEqual(send['delivery'], 'rejected')
        self.assertFalse(send['canRetry'])
        self.assertEqual(send['requestId'], self.request)
        self.assertEqual(len(run.call_args_list), 4)
        self.assertTrue(all(call.args[1] == ['agents', '--json', '--all'] for call in run.call_args_list))

    def test_native_read_times_answers_by_their_whole_turn_and_reports_the_running_turn_start(self):
        thread = self.thread('active')
        thread['turns'] = [
            {'id': 'done', 'status': 'completed', 'startedAt': 1000, 'completedAt': 1600, 'items': [
                {'type': 'userMessage', 'id': 'u1', 'content': [{'type': 'text', 'text': '질문'}]},
                {'type': 'agentMessage', 'id': 'a1', 'text': '중간 보고', 'phase': 'commentary'},
                {'type': 'commandExecution', 'id': 'c1', 'command': 'ls'},
                {'type': 'agentMessage', 'id': 'a2', 'text': '최종 답', 'phase': 'final_answer'}]},
            {'id': 'live', 'status': 'inProgress', 'startedAt': 2000, 'items': [
                {'type': 'userMessage', 'id': 'u2', 'content': [{'type': 'text', 'text': '다음 질문'}]},
                {'type': 'agentMessage', 'id': 'a3', 'text': '진행 중'}]}]
        result = self.handle(FakeClient(thread), {'action': 'read', 'source': self.source})
        self.assertEqual([(m['itemId'], m['ts']) for m in result['messages']],
                         [('u1', 1000), ('a1', None), ('c1', None), ('a2', 1600), ('u2', 2000), ('a3', None)])
        self.assertEqual(result['messages'][2]['toolKind'], 'command')
        # Marked as the list's reader marks them (collector.codex_messages): which answer is final.
        self.assertEqual([(m.get('messageId'), m.get('phase')) for m in result['messages'][1:4]],
                         [('a1', 'commentary'), ('c1', None), ('a2', 'final_answer')])
        self.assertEqual(result['workingSince'], 2000)
        thread['turns'] = thread['turns'][:1]
        thread['status'] = {'type': 'idle', 'activeFlags': []}
        self.assertNotIn('workingSince', self.handle(FakeClient(thread), {'action': 'read', 'source': self.source}))

    def test_working_claude_read_counts_from_the_newest_prompt_as_an_approximation(self):
        source = {'agent': 'claude', 'home': '.claude', 'id': 'back1234'}
        sid = 'session-12345'
        folder = self.home / '.claude/projects/p'
        folder.mkdir(parents=True)
        lines = [{'type': 'user', 'timestamp': '2026-10-11T01:00:00Z', 'message': {'content': '시작해 주세요'}},
                 {'type': 'assistant', 'timestamp': '2026-10-11T01:05:00Z', 'message': {'content': [
                     {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'ls'}}]}},
                 {'type': 'user', 'timestamp': '2026-10-11T01:05:01Z', 'message': {'content': [
                     {'type': 'tool_result', 'content': 'ok'}]}}]
        (folder / (sid + '.jsonl')).write_text(''.join(json.dumps(line) + '\n' for line in lines))
        for state, expected in (('working', collector.to_epoch('2026-10-11T01:00:00Z')), ('idle', None)):
            row = {'id': source['id'], 'sessionId': sid, 'kind': 'background',
                   'state': state, 'status': 'busy' if state == 'working' else 'idle', 'cwd': str(self.cwd)}
            with patch.object(chat.native, '_claude_binary', return_value='/fixture/claude'), \
                    patch.object(chat.native, '_claude_run', return_value=json.dumps([row]).encode()):
                read = chat.handle({'action': 'read', 'source': source}, home=self.home)
            self.assertEqual(read.get('workingSince'), expected, state)
            self.assertEqual(read.get('workingSinceApprox'), True if expected else None)
            self.assertEqual([m['role'] for m in read['messages']], ['user', 'tool'])


class StoppedOnErrorTest(unittest.TestCase):
    def test_a_codex_stopped_on_an_error_reads_as_waiting_and_says_why_input_is_locked(self):
        source = {'agent': 'codex', 'home': '.codex-isolated', 'id': str(uuid.uuid4()), 'cwd': '/tmp/project'}
        thread = {'id': source['id'], 'cwd': '/tmp/project', 'status': {'type': 'systemError'}, 'canAcceptDirectInput': True,
                  'source': 'cli', 'updatedAt': 1, 'turns': [{'id': 't-1', 'status': 'failed', 'items': []}]}
        with patch.object(chat, '_client', return_value=FakeClient(thread)):
            result = chat.handle({'action': 'read', 'source': source})
        self.assertEqual(result['phase'], 'needs_input', 'as the list says (답 필요)')
        self.assertFalse(result['capabilities']['canSend'])
        self.assertIn('오류로 멈췄습니다', result['capabilities']['reason'])


class RecentHistoryTests(unittest.TestCase):
    """A conversation read pages the latest history instead of loading the whole rollout."""
    def setUp(self):
        self.source = {'agent': 'codex', 'home': '.codex-isolated', 'id': str(uuid.uuid4()), 'cwd': '/tmp/project'}

    def thread(self, turns, kind='idle'):
        return {'id': self.source['id'], 'cwd': self.source['cwd'], 'turns': turns,
                'status': {'type': kind, 'activeFlags': []}, 'canAcceptDirectInput': True,
                'source': 'cli', 'updatedAt': 1}

    @staticmethod
    def turn(number, tools, status='completed'):
        items = [{'type': 'userMessage', 'id': 'u%d' % number, 'clientId': 'request-%04d' % number,
                  'content': [{'type': 'text', 'text': 'ask %d' % number}]}]
        items += [{'type': 'commandExecution', 'id': 'c%d-%d' % (number, index), 'command': 'ls'} for index in range(tools)]
        items.append({'type': 'agentMessage', 'id': 'a%d' % number, 'text': 'answer %d' % number})
        return {'id': 'turn-%d' % number, 'status': status, 'startedAt': number, 'completedAt': number, 'items': items}

    def read(self, client, **args):
        with patch.object(chat, '_client', return_value=client):
            return chat.handle({'action': 'read', 'source': self.source, **args})

    def test_long_conversation_reads_only_its_latest_turns(self):
        client = FakeClient(self.thread([self.turn(number, 30) for number in range(1, 41)]))
        result = self.read(client, requestId='request-0040')
        self.assertNotIn(True, [params.get('includeTurns') for method, params in client.calls if method == 'thread/read'])
        self.assertEqual(len(result['messages']), collector.MESSAGE_LIMIT)
        self.assertEqual(result['messages'][-1]['text'], 'answer 40')
        self.assertEqual(result['receipts'][0]['delivery'], 'completed')
        # 32 rows per turn: the 80th newest row is in turn 38, whose user message is on page one.
        self.assertEqual(len([m for m, _ in client.calls if m == 'thread/items/list']), 1)
        self.assertEqual([turn['id'] for turn in result['turns']], ['turn-37', 'turn-38', 'turn-39', 'turn-40'])
        self.assertEqual((result['messages'][0]['role'], result['messages'][0]['turnId']), ('tool', 'turn-38'))

    def test_kept_turn_reaches_back_to_its_user_message(self):
        # One turn longer than a page: its request is still found, its start time kept.
        client = FakeClient(self.thread([self.turn(1, 5), self.turn(2, 250)]))
        result = self.read(client, requestId='request-0002')
        self.assertEqual(result['receipts'][0]['delivery'], 'completed')
        self.assertEqual([turn['id'] for turn in result['turns']], ['turn-1', 'turn-2'])

    def test_just_started_turn_without_items_is_the_active_one(self):
        started = {'id': 'turn-3', 'status': 'inProgress', 'startedAt': 3, 'items': []}
        client = FakeClient(self.thread([self.turn(1, 1), self.turn(2, 1), started], kind='active'))
        result = self.read(client)
        self.assertEqual(result['phase'], 'working')
        self.assertEqual(result['activeTurnId'], 'turn-3')
        self.assertEqual(result['messages'][-1]['text'], 'answer 2')

    def test_history_pages_are_bounded(self):
        client = FakeClient(self.thread([self.turn(1, 5000)]))
        result = self.read(client)
        self.assertEqual(len([m for m, _ in client.calls if m == 'thread/items/list']), collector.HISTORY_PAGES)
        self.assertEqual(len(result['messages']), collector.MESSAGE_LIMIT)

    def test_daemon_without_paged_history_is_read_whole(self):
        unknown = chat.NativeRejected({'code': -32600, 'message': 'Invalid request: unknown variant `thread/items/list`, '
                                                                  'expected one of `thread/attachment/add`'})
        self.assertEqual((unknown.category, unknown.unknown_variant), ('unknownVariant', 'thread/items/list'))
        thread = self.thread([self.turn(1, 1)])
        client = FakeClient(thread, outcomes={'thread/items/list': unknown})
        result = self.read(client)
        self.assertEqual([(m, p.get('includeTurns')) for m, p in client.calls],
                         [('thread/read', False), ('thread/items/list', None), ('thread/read', True)])
        self.assertEqual(result['messages'][-1]['text'], 'answer 1')
        for other in (chat.NativeRejected({'code': -32603, 'message': 'internal error'}),
                      # A rejected parameter value names its variant too; that is not an older daemon.
                      chat.NativeRejected({'code': -32600, 'message': 'Invalid request: unknown variant `notloaded`, expected one of `summary`'}),
                      chat.NativeRejected({'code': -32600, 'message': 'Invalid request: unknown variant `thread/items/list`'})):
            with self.subTest(error=other.unknown_variant), self.assertRaises(chat.NativeRejected):
                self.read(FakeClient(thread, outcomes={'thread/turns/list': other}))

    def test_thread_in_the_legacy_format_is_read_whole(self):
        # codex app-server: ThreadStoreError::Unsupported -> method_not_found("... is not supported yet").
        thread = self.thread([self.turn(1, 1)])
        for method in ('thread/items/list', 'thread/turns/list'):
            with self.subTest(method=method):
                legacy = chat.NativeRejected({'code': -32601, 'message': method + ' is not supported yet'})
                client = FakeClient(thread, outcomes={method: legacy})
                result = self.read(client)
                self.assertEqual([(m, p.get('includeTurns')) for m, p in client.calls if m == 'thread/read'],
                                 [('thread/read', False), ('thread/read', True)])
                self.assertEqual(result['messages'][-1]['text'], 'answer 1')
        self.assertTrue(collector.paging_unsupported(collector.RpcReplyError('thread/items/list is not supported yet', -32601), 'thread/items/list'))

    def test_collector_fallback_names_an_older_daemon(self):
        older = collector.RpcReplyError('Invalid request: unknown variant `thread/items/list`, expected one of `x`', -32600)
        self.assertTrue(collector.paging_unsupported(older, 'thread/items/list'))
        self.assertFalse(collector.paging_unsupported(older, 'thread/turns/list'))
        self.assertFalse(collector.paging_unsupported(collector.RpcReplyError('Invalid request: unknown variant `notLoaded`', -32600), 'thread/turns/list'))
        self.assertFalse(collector.paging_unsupported(collector.RpcReplyError('Invalid request: missing field', -32600), 'thread/items/list'))
        self.assertFalse(collector.paging_unsupported(collector.RpcReplyError('unknown variant `thread/items/list`', -32603), 'thread/items/list'))


if __name__ == '__main__':
    unittest.main()
