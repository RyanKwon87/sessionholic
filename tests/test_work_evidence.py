"""Native evidence preservation and exact-read boundaries, all synthetic."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector
import chat
import server
from automatic_work import AutomaticWork

SID = '019aaaaa-0000-7000-8000-000000000005'
REF = {'host': 'fixture', 'agent': 'codex', 'home': '.codex', 'id': SID}


class WorkEvidenceTest(unittest.TestCase):
    def test_codex_message_and_turn_ids_survive_and_missing_ids_are_not_fabricated(self):
        rows = collector.codex_messages([{'id': 'turn-one', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'id': 'user-one', 'content': [{'type': 'text', 'text': '수정해줘'}]},
            {'type': 'agentMessage', 'id': 'answer-one', 'text': '수정 결과', 'phase': 'final_answer'},
            {'type': 'agentMessage', 'text': '이전 형식 결과'}]}])
        self.assertEqual(rows[1]['phase'], 'final_answer')
        self.assertEqual([row.get('messageId') for row in rows], ['user-one', 'answer-one', None])
        self.assertTrue(all(row['turnId'] == 'turn-one' and row['turnStatus'] == 'completed' for row in rows))

    def test_claude_uuid_and_parent_reference_survive(self):
        rows = collector.claude_messages([{'type': 'user', 'uuid': 'user-one', 'message': {'content': '수정해줘'}},
            {'type': 'assistant', 'uuid': 'answer-one', 'parentUuid': 'user-one',
             'message': {'content': [{'type': 'text', 'text': '현재 보고'}, {'type': 'text', 'text': '결과 보고'}]}}])
        self.assertEqual([row['messageId'] for row in rows], ['user-one', 'answer-one', 'answer-one'])
        self.assertTrue(all(row['parentMessageId'] == 'user-one' for row in rows[1:]))

    def test_unsafe_or_unbounded_native_ids_are_omitted(self):
        for invalid in ('unsafe\nline', 'x' * 1024, '/unsafe/path', ''):
            with self.subTest(invalid_length=len(invalid)):
                codex = collector.codex_messages([{'id': invalid, 'items': [
                    {'type': 'agentMessage', 'id': invalid, 'text': '보고'}]}])[0]
                self.assertNotIn('turnId', codex)
                self.assertNotIn('messageId', codex)
                claude = collector.claude_messages([{'type': 'assistant', 'uuid': invalid, 'parentUuid': invalid,
                    'message': {'content': '보고'}}])[0]
                self.assertNotIn('messageId', claude)
                self.assertNotIn('parentMessageId', claude)

    def read(self, response):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            socket = root / '.codex' / collector.SOCKET_REL
            socket.parent.mkdir(parents=True)
            socket.touch()
            native = Mock()
            native.__enter__ = Mock(return_value=native)
            native.__exit__ = Mock(return_value=False)
            thread = response.get('thread')
            def call(method, params):
                # The metadata read, then history pages newest first, as the app server answers.
                if method == 'thread/read':
                    return {**response, 'thread': {**thread, 'turns': []}} if isinstance(thread, dict) else response
                turns = list(reversed(thread['turns']))
                if method == 'thread/items/list':
                    data = [{'item': item, 'turnId': turn['id']} for turn in turns for item in reversed(turn['items'])]
                else:
                    data = [{**turn, 'items': [], 'itemsView': 'notLoaded'} for turn in turns]
                return {'data': data[:params['limit']], 'nextCursor': None}
            native.call.side_effect = call
            with patch.object(collector, 'HOME', root), patch.object(collector, 'AppServer', return_value=native):
                result = collector.read('codex', SID, '.codex')
            self.assertEqual(native.call.call_args_list[0].args, ('thread/read', {'threadId': SID, 'includeTurns': False}))
            self.assertNotIn(True, [c.args[1].get('includeTurns') for c in native.call.call_args_list])
            return result

    def test_read_rejects_mismatched_or_missing_native_thread_identity(self):
        for thread in ({'id': 'different-session', 'turns': []}, {'turns': []}, None):
            result = self.read({'thread': thread})
            self.assertIn('error', result)
            self.assertEqual(result['messages'], [])

    def test_read_phase_comes_from_thread_status_not_last_turn_completion(self):
        for status, expected in (({'type': 'active', 'activeFlags': []}, 'working'),
                                 ({'type': 'active', 'activeFlags': ['waitingOnUserInput']}, 'needs_input'),
                                 ({'type': 'idle'}, 'idle'), ({'type': 'notLoaded'}, 'unavailable')):
            with self.subTest(status=status):
                result = self.read({'thread': {'id': SID, 'status': status, 'turns': [
                    {'id': 'turn-one', 'status': 'completed', 'items': [{'type': 'agentMessage', 'id': 'answer-one', 'text': '이전 결과'}]}]}})
                self.assertEqual(result['phase'], expected)
                self.assertEqual(result['messages'][0]['turnStatus'], 'completed')

    def test_idle_read_does_not_complete_automatic_work(self):
        result = self.read({'thread': {'id': SID, 'status': {'type': 'idle'}, 'turns': [
            {'id': 'turn-one', 'status': 'completed', 'items': [{'type': 'agentMessage', 'id': 'answer-one', 'text': '완료했습니다.'}]}]}})
        with tempfile.TemporaryDirectory() as temp:
            work = AutomaticWork(temp)
            goal = work.observe(REF, result)['goals'][0]
            self.assertEqual(goal['latestResult'], '완료했습니다.')
            self.assertEqual(goal['status'], 'review')
            self.assertNotEqual(goal['status'], 'done')

    def test_chat_preserves_native_evidence_and_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            native_source = {**REF, 'cwd': str(root), 'phase': 'idle', 'title': '예제'}
            host = {'name': 'fixture', 'label': '합성 기기', 'local': True}
            board = server.Board([host], 60, runner=lambda *_: {'codex': [native_source], 'claude': []}, live=False)
            board.poll(host)
            flow = server.Workflow(board, SimpleNamespace(list=lambda: []), root)
            flow.transfers.rpc = Mock(return_value={'phase': 'idle', 'capabilities': {'canSend': True},
                'messages': [{'role': 'assistant', 'messageId': 'answer-one', 'turnId': 'turn-one', 'text': '현재 보고'}],
                'activeTurnId': 'turn-one', 'activeFlags': [], 'turns': [{'id': 'turn-one', 'status': 'completed'}], 'updatedAt': 1234})
            view = chat.Chat(board, flow, root).state({'source': REF})
            self.assertEqual(view['route']['id'], SID)
            self.assertEqual(view['messages'][0]['messageId'], 'answer-one')
            self.assertEqual(view['messages'][0]['turnId'], 'turn-one')
            self.assertEqual(view['activeTurnId'], 'turn-one')
            self.assertEqual(view['turns'], [{'id': 'turn-one', 'status': 'completed'}])
            self.assertEqual(view['updatedAt'], 1234)
            flow.transfers.rpc.assert_called_once()


if __name__ == '__main__':
    unittest.main()
