"""Recovery preserves native identity, explicit closure and message-free launch."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat
import server
from test_workflow import FakeTerminals, HOST, SOURCE, PROFILE, OTHER, REF


class SessionRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.row = {**SOURCE, 'phase': 'closed'}
        self.board = server.Board([HOST], 60, runner=lambda *_: {'codex': [self.row], 'claude': []}, live=False)
        self.board.poll(HOST)
        self.terms = FakeTerminals()
        self.flow = server.Workflow(self.board, self.terms, self.temp.name)
        self.flow.profiles = lambda: [PROFILE, OTHER]
        self.service = chat.Chat(self.board, self.flow, self.temp.name)
        self.target = {'host': HOST['name'], 'agent': 'codex', 'account': PROFILE['id']}

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        return self.flow.plan({'source': REF, 'target': self.target, 'resume': True})

    def test_closed_conversation_offers_recovery_without_launch_or_input(self):
        with patch.object(self.service, 'rpc', return_value={'phase': 'closed', 'messages': [], 'capabilities': {'canSend': False}}), \
                patch.object(self.flow, 'resume_preview', return_value={'allowed': True, 'blockedByBoard': False}) as preview:
            result = self.service.state({'source': REF})
        self.assertEqual(result['capability']['action'], 'resume_session')
        self.assertEqual(result['recovery']['action'], 'resume_session')
        self.assertFalse(self.terms.calls)
        preview.assert_called_once()

    def test_explicit_web_close_does_not_offer_resume(self):
        with patch.object(self.service, 'rpc', return_value={'phase': 'closed', 'messages': [], 'capabilities': {'canSend': False}}), \
                patch.object(self.flow, 'resume_preview', return_value={'allowed': False, 'blockedByBoard': True, 'reason': '웹보드에서 종료한 세션입니다.'}):
            result = self.service.state({'source': REF})
        self.assertTrue(result['recovery']['closedByBoard'])
        self.assertFalse(result['recovery']['supported'])
        self.assertNotIn('action', result['capability'])

    def test_missing_daemon_uses_saved_transcript_with_recovery(self):
        with patch.object(self.service, 'rpc', side_effect=RuntimeError('daemon disconnected')), \
                patch.object(self.board, 'read', return_value={'messages': [{'role': 'assistant', 'text': '합성 대화'}]}), \
                patch.object(self.flow, 'resume_preview', return_value={'allowed': True}):
            result = self.service.state({'source': REF})
        self.assertEqual(result['messages'][0]['text'], '합성 대화')
        self.assertEqual(result['recovery']['action'], 'resume_session')

    def test_saved_transcript_read_keeps_the_window_and_says_older_rows_were_left_out(self):
        # A question every third row: the 80 newest start at row 60.
        rows = [{'role': 'user' if index % 3 == 0 else 'tool', 'text': '합성 %d' % index, 'ts': index} for index in range(300)]
        self.board.runner = lambda host, args, timeout: {'messages': rows, 'olderOmitted': True}
        with patch.object(self.service, 'rpc', side_effect=RuntimeError('daemon disconnected')), \
                patch.object(self.flow, 'resume_preview', return_value={'allowed': True}):
            result = self.service.state({'source': REF})
        self.assertEqual((len(result['messages']), result['messages'][0]['text']), (240, '합성 60'))
        self.assertIs(result['olderOmitted'], True)

    def test_board_read_keeps_the_hosts_window_and_bounds_any_other(self):
        sent = {}
        self.board.runner = lambda host, args, timeout: dict(sent)
        def read(messages, older=None):
            sent.clear()
            sent['messages'] = messages
            if older is not None:
                sent['olderOmitted'] = older
            result = self.board.read(HOST['name'], 'codex', REF['id'], REF['home'], fresh=True)
            return len(result['messages']), result['olderOmitted']
        tools = [{'role': 'tool', 'text': 'ls'}] * 100
        self.assertEqual(read(tools + [{'role': 'user', 'text': '질문'}] * 20, False), (120, False))
        self.assertEqual(read([{'role': 'user', 'text': '질문'}] * 3, True), (3, True))
        # More than any host sends: the same window, said to be cut.
        self.assertEqual(read([{'role': 'assistant', 'text': '답변'}] * 1000), (80, True))
        self.assertEqual(read(tools * 10), (400, True))
        self.assertEqual(read('not a list'), (0, False))

    def test_failed_recovery_probe_offers_connection_check_only(self):
        with patch.object(self.service, 'rpc', return_value={'phase': 'closed', 'messages': []}), \
                patch.object(self.flow, 'resume_preview', side_effect=RuntimeError('offline')):
            result = self.service.state({'source': REF})
        self.assertEqual(result['recovery']['action'], 'refresh_host')
        self.assertTrue(result['recovery']['supported'])
        self.assertNotIn('action', result['capability'])

    def test_missing_daemon_and_transcript_still_offer_same_session_recovery(self):
        with patch.object(self.service, 'rpc', side_effect=RuntimeError('daemon disconnected')), \
                patch.object(self.board, 'read', side_effect=RuntimeError('history unavailable')), \
                patch.object(self.flow, 'resume_preview', return_value={'allowed': True}):
            result = self.service.state({'source': REF})
        self.assertEqual(result['messages'], [])
        self.assertTrue(result['conversationUnavailable'])
        self.assertEqual(result['recovery']['action'], 'resume_session')
        self.assertFalse(self.terms.calls)

    def test_resume_plan_does_not_create_terminal_and_launch_keeps_uuid(self):
        spec = {'argv': ['/fixture/codex', 'resume', SOURCE['id']], 'cwd': SOURCE['cwd'], 'env': {}, 'mode': 'resume'}
        with patch('server.shutil.which', return_value='/fixture/tmux'), \
                patch.object(self.flow, 'resume_preview', return_value={'allowed': True}), \
                patch('launch.build_launch', return_value=spec) as build:
            plan = self.plan()
            self.assertEqual(plan['mode'], 'resume')
            self.assertFalse(self.terms.calls)
            result = self.flow.launch(plan['id'], 'fixture-recovery-once')
            self.assertTrue(build.call_args.args[0]['resume'])
            self.assertEqual(result['terminal']['nativeSource']['id'], SOURCE['id'])
            duplicate = self.flow.launch(plan['id'], 'fixture-recovery-once')
            self.assertEqual(result['terminal']['id'], duplicate['terminal']['id'])
            self.assertEqual(build.call_count, 1)

    def test_explicit_termination_between_preview_and_launch_blocks(self):
        with patch('server.shutil.which', return_value='/fixture/tmux'), \
                patch.object(self.flow, 'resume_preview', side_effect=[{'allowed': True}, {'allowed': False, 'blockedByBoard': True, 'reason': '웹보드에서 종료했습니다.'}]), \
                patch('launch.build_launch', side_effect=AssertionError('resumed after close')):
            plan = self.plan()
            with self.assertRaisesRegex(ValueError, '종료'):
                self.flow.launch(plan['id'], 'fixture-recovery-race')
        self.assertFalse(self.terms.calls)

    def test_resume_cannot_switch_account_or_bypass_offline(self):
        with self.assertRaises(ValueError):
            self.flow.plan({'source': REF, 'target': {**self.target, 'account': OTHER['id']}, 'resume': True})
        self.board.state[HOST['name']]['ok'] = False
        with patch('server.shutil.which', return_value='/fixture/tmux'), \
                patch.object(self.flow, 'resume_preview', side_effect=AssertionError('offline RPC')):
            self.assertFalse(self.plan()['allowed'])

    def test_only_explicit_terminal_close_is_recovery_block(self):
        native = {**REF, 'cwd': SOURCE['cwd']}
        # "released": the board freed an idle connection for another terminal.
        for reason, blocked in ((None, False), ('connection_ended', False), ('released', False), ('user', True)):
            with self.subTest(reason=reason):
                self.terms.records = [{'id': 'fixture', 'closed': True, 'alive': False,
                                       'closedReason': reason, 'nativeSource': native}]
                with patch.object(self.flow.transfers, 'rpc', return_value={'allowed': True, 'blockedByBoard': False}) as rpc:
                    result = self.flow.resume_preview({**self.row, 'host': HOST['name']})
                self.assertEqual(result['blockedByBoard'], blocked)
                self.assertEqual(rpc.call_count, 0 if blocked else 1)


if __name__ == '__main__':
    unittest.main()
