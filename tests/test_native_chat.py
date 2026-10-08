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


class FakeClient:
    def __init__(self, thread, queue=None, outcomes=None):
        self.thread = thread
        self.queue = list(queue or [])
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
            return {'thread': self.thread}
        if method == 'thread/queue/list':
            return {'data': self.queue, 'nextCursor': None}
        if method == 'thread/queue/add':
            item = {'id': 'queue-1', 'input': params['input'],
                    'clientUserMessageId': params['clientUserMessageId']}
            self.queue.append(item)
            return {'queuedSubmission': item}
        if method in ('thread/queue/start', 'turn/steer'):
            item = self.queue.pop(0) if self.queue else {
                'input': params['input'], 'clientUserMessageId': params['clientUserMessageId']}
            turn = {'id': 'turn-1', 'status': 'inProgress', 'items': [{
                'type': 'userMessage', 'id': 'item-1', 'clientId': item['clientUserMessageId'],
                'content': item['input']}], 'startedAt': 1}
            self.thread['turns'].append(turn)
            self.thread['status'] = {'type': 'active', 'activeFlags': []}
            return {'turn': turn, 'turnId': turn['id']}
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
        return [(method, params) for method, params in client.calls if method not in ('thread/read', 'thread/queue/list')]

    def test_idle_atomically_starts_queued_input_and_readback_exact_client_id(self):
        client = FakeClient(self.thread())
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'accepted')
        self.assertTrue(result['readbackConfirmed'])
        self.assertEqual(result['turnId'], 'turn-1')
        self.assertEqual([m for m, _ in self.writes(client)], ['thread/queue/add', 'thread/queue/start'])
        params = self.writes(client)[0][1]
        self.assertEqual(set(params), {'threadId', 'input', 'clientUserMessageId'})
        self.assertEqual(params['clientUserMessageId'], self.request)
        self.assertFalse(any(m == 'turn/start' for m, _ in client.calls))

    def test_busy_defaults_queue_and_never_steers(self):
        thread = self.thread('active')
        thread['turns'] = [{'id': 'active-1', 'status': 'inProgress', 'items': []}]
        client = FakeClient(thread)
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'queued')
        self.assertTrue(result['readbackConfirmed'])
        self.assertEqual([m for m, _ in self.writes(client)], ['thread/queue/add'])

    def test_idle_race_or_autodispatch_reject_keeps_native_queue_no_retry(self):
        client = FakeClient(self.thread(), outcomes={
            'thread/queue/start': collector.RpcError('fixture concurrent active')})
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'queued')
        self.assertTrue(result['readbackConfirmed'])
        self.assertEqual([m for m, _ in self.writes(client)], ['thread/queue/add', 'thread/queue/start'])

    def test_unknown_delivery_stays_read_only_on_repeated_request(self):
        client = FakeClient(self.thread(), outcomes={'thread/queue/add': TimeoutError()})
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
        client = FakeClient(self.thread(), outcomes={'thread/queue/add': error})
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'rejected')
        self.assertEqual(result['nativeErrorCode'], -32600)
        self.assertNotIn('fixture private path', json.dumps(result))
        clean = FakeClient(self.thread())
        self.assertEqual(self.handle(clean)['delivery'], 'rejected')
        receipt = self.handle(clean, {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertEqual(receipt['delivery'], 'rejected')
        self.assertEqual(self.writes(clean), [])

    def test_acknowledged_receipt_survives_native_history_lag_without_retry(self):
        client = FakeClient(self.thread('active'), outcomes={
            'thread/read': {'thread': self.thread('active')},
            'thread/queue/list': {'data': []}})
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'queued')
        self.assertTrue(result['acknowledged'])
        self.assertFalse(result['readbackConfirmed'])
        clean = FakeClient(self.thread('active'))
        receipt = self.handle(clean, {'action': 'receipt', 'source': self.source, 'requestId': self.request})
        self.assertTrue(receipt['confirmed'])
        self.assertFalse(receipt['readbackConfirmed'])
        self.assertEqual(self.writes(clean), [])

    def test_same_request_different_content_is_rejected(self):
        self.handle(FakeClient(self.thread('active')))
        altered = {**self.args, 'input': [{'type': 'text', 'text': 'other fixture'}]}
        client = FakeClient(self.thread())
        with self.assertRaises(ValueError):
            self.handle(client, altered)
        self.assertEqual(self.writes(client), [])

    def test_duplicate_native_queue_or_history_prevents_mutation(self):
        thread = self.thread('active')
        item = {'id': 'prior-q', 'clientUserMessageId': self.request}
        client = FakeClient(thread, [item])
        self.assertEqual(self.handle(client)['queueId'], 'prior-q')
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

    def test_queue_api_unavailable_disables_input_even_if_turn_start_exists(self):
        client = FakeClient(self.thread(), outcomes={'thread/queue/list': collector.RpcError('missing')})
        result = self.handle(client)
        self.assertEqual(result['delivery'], 'rejected')
        self.assertEqual(self.writes(client), [])

    def test_explicit_steer_requires_matching_turn_and_avoids_approval_states(self):
        thread = self.thread('active')
        thread['turns'] = [{'id': 'active-1', 'status': 'inProgress', 'items': []}]
        args = {**self.args, 'mode': 'steer', 'expectedTurnId': 'active-1'}
        for expected, flags in [('wrong', []), ('active-1', ['waitingOnApproval'])]:
            thread['status']['activeFlags'] = flags
            client = FakeClient(thread)
            self.assertEqual(self.handle(client, {**args, 'expectedTurnId': expected})['delivery'], 'rejected')
            self.assertEqual(self.writes(client), [])
        thread['status']['activeFlags'] = []
        client = FakeClient(thread)
        result = self.handle(client, args)
        self.assertEqual(result['delivery'], 'accepted')
        self.assertEqual([m for m, _ in self.writes(client)], ['turn/steer'])
        self.assertEqual(self.writes(client)[0][1]['expectedTurnId'], 'active-1')

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
            send = chat.handle({**self.args, 'source': source}, home=self.home)
        self.assertEqual(read['cwd'], str(self.cwd))
        self.assertEqual(read['messages'][0]['text'], 'fixture')
        self.assertFalse(read['capabilities']['canSend'])
        self.assertTrue(read['requiresNativeTerminal'])
        self.assertEqual(send['delivery'], 'rejected')
        self.assertTrue(all(call.args[1] == ['agents', '--json', '--all'] for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
