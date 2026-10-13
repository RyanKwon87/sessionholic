"""Synthetic service tests: no native, SSH, model, or production access."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goals import Goals
from work_service import WorkService, identity, source_ref


def row(identifier='session-a', phase='working', **extra):
    return {'id': identifier, 'agent': 'codex', 'home': '.codex-example', 'title': '예제 작업',
            'phase': phase, 'updatedAt': 100, **extra}


def host(*rows, name='mini', ok=True, **extra):
    return {'name': name, 'ok': ok, 'paused': False, 'refreshing': False,
            'data': {agent: [item for item in rows if item['agent'] == agent]
                     for agent in ('codex', 'claude')}, **extra}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds=10):
        self.now += seconds


class FakeBoard:
    interval = 60

    def __init__(self, hosts=None):
        self.value = {'version': 1, 'hosts': hosts or [host(row())]}
        self.reads, self.refreshes, self.listeners = [], [], []
        self.answer = {'phase': 'idle', 'messages': [{'role': 'assistant', 'id': 'a1', 'text': '작성 결과'}]}
        self.failure = None
        self.fresh_reads = []

    def snapshot(self):
        return copy.deepcopy(self.value)

    def read(self, name, agent, identifier, home, fresh=False):
        self.reads.append((name, agent, identifier, home))
        self.fresh_reads.append(fresh)
        if self.failure:
            raise self.failure
        return copy.deepcopy(self.answer)

    def request_refresh(self, name, force=False):
        self.refreshes.append((name, force))

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        self.listeners.remove(listener)


class WorkServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        self.board, self.clock = FakeBoard(), FakeClock()
        self.goals = Goals(self.directory)
        self.service = WorkService(self.board, self.goals, self.directory, clock=self.clock)

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def active(self):
        return [goal for goal in self.service.snapshot()['goals'] if not goal.get('automation', {}).get('mergedInto')]

    def test_first_cards_are_immediate_and_do_not_read_conversations(self):
        goals = self.active()
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]['title'], '예제 작업')
        self.assertTrue(goals[0]['automatic'])
        self.assertEqual(self.board.reads, [])
        self.assertEqual(self.board.refreshes, [])

    def test_manual_records_remain_and_same_session_is_not_duplicated(self):
        manual = self.goals.save({'title': '기존 수동 작업', 'sessions': [source_ref('mini', 'codex', row())]})['goal']
        before = self.goals.snapshot()
        goals = self.active()
        self.assertEqual([goal['id'] for goal in goals], [manual['id']])
        self.assertFalse(goals[0]['automatic'])
        self.assertEqual(self.goals.snapshot(), before)

    def test_archived_or_done_manual_record_cannot_hide_current_automatic_work(self):
        manual = self.goals.save({'title': '과거 수동 작업', 'sessions': [source_ref('mini', 'codex', row())], 'archived': True})['goal']
        self.assertTrue(any(goal['automatic'] for goal in self.active()))
        self.goals.save({'revision': manual['revision'], 'archived': False, 'status': 'done',
                         'completionCriteria': '과거 작업 검증', 'evidence': '과거 결과 확인'}, manual['id'])
        self.assertTrue(any(goal['automatic'] for goal in self.active()))

    def test_new_explicit_segment_is_not_hidden_by_older_manual_session_reference(self):
        self.goals.save({'title': '이전 구간 수동 작업', 'sessions': [source_ref('mini', 'codex', row())]})
        self.active()
        self.service.observe(source_ref('mini', 'codex', row()), {'phase': 'idle', 'messages': [
            {'role': 'user', 'messageId': 'u1', 'text': '첫 작업'},
            {'role': 'user', 'messageId': 'u2', 'text': '새 작업: 새 구간의 업무'}]})
        tasks = [goal for goal in self.active() if goal['automatic']]
        self.assertEqual([goal['title'] for goal in tasks], ['새 구간의 업무'])

    def test_manual_and_unrelated_automatic_records_coexist(self):
        self.goals.save({'title': '별개 기존 작업'})
        self.assertEqual(len(self.active()), 2)
        self.assertEqual({goal['automatic'] for goal in self.active()}, {True, False})

    def test_at_most_one_changed_conversation_per_ten_seconds(self):
        self.board.value['hosts'] = [host(row(), row('session-b'), row('session-c'))]
        self.service.step()
        self.assertEqual(len(self.board.reads), 1)
        self.clock.advance(9.999)
        self.service.step()
        self.assertEqual(len(self.board.reads), 1)
        self.clock.advance(.001)
        self.service.step()
        self.assertEqual(len(self.board.reads), 2)
        self.clock.advance(10)
        self.service.step()
        self.assertEqual(len(self.board.reads), 3)
        self.assertEqual(len({entry[2] for entry in self.board.reads}), 3)

    def test_unchanged_data_and_host_heartbeat_do_not_reread(self):
        self.service.step()
        self.clock.advance(120)
        self.board.value['version'] += 1
        self.board.value['hosts'][0].update(fetchedAt=200, durationMs=3, live=True)
        self.service.step()
        self.assertEqual(len(self.board.reads), 1)

    def test_changed_session_becomes_readable_again(self):
        self.service.step()
        self.clock.advance()
        self.board.value['hosts'][0]['data']['codex'][0]['snippet'] = '새 보고'
        self.service.step()
        self.assertEqual(len(self.board.reads), 2)

    def test_changed_digest_requires_fresh_read_instead_of_older_board_cache(self):
        self.service.step()
        self.clock.advance()
        self.board.value['hosts'][0]['data']['codex'][0]['updatedAt'] = 101
        self.service.step()
        self.assertEqual(self.board.fresh_reads, [True, True])

    def test_changed_digest_bypasses_real_board_conversation_cache(self):
        import server
        calls = []
        answer = {'text': '첫 결과'}
        native_row = row(phase='idle')
        def runner(machine, args, timeout):
            if args[0] == 'snapshot':
                return {'codex': [native_row], 'claude': []}
            calls.append(args)
            return {'phase': 'idle', 'messages': [{'role': 'assistant', 'messageId': answer['text'], 'text': answer['text']}]}
        machine = {'name': 'mini', 'label': '합성', 'local': True}
        board = server.Board([machine], 60, runner=runner, live=False)
        board.poll(machine)
        board.request_refresh = Mock(return_value=False)
        self.service = WorkService(board, self.goals, self.directory, clock=self.clock)
        self.service.step()
        self.assertEqual(self.service.store.snapshot()['goals'][0]['latestResult'], '첫 결과')
        answer['text'] = '최신 결과'
        native_row['updatedAt'] = 101
        board.poll(machine)
        self.clock.advance()
        self.service.step()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.service.store.snapshot()['goals'][0]['latestResult'], '최신 결과')

    def test_continuously_changing_active_session_does_not_starve_unread_recent_work(self):
        self.board.value['hosts'] = [host(row('active'), row('recent', phase='idle'))]
        self.service.step()
        self.board.value['hosts'][0]['data']['codex'][0]['updatedAt'] = 101
        self.clock.advance()
        self.service.step()
        self.assertEqual([entry[2] for entry in self.board.reads], ['active', 'recent'])

    def test_failure_digest_is_recorded_before_read_and_not_retried(self):
        self.board.failure = RuntimeError('synthetic failure')
        with self.assertRaises(RuntimeError):
            self.service.step()
        self.assertEqual(len(self.board.reads), 1)
        self.clock.advance(120)
        self.service.step()
        self.assertEqual(len(self.board.reads), 1)
        self.board.value['hosts'][0]['data']['codex'][0]['updatedAt'] = 101
        self.clock.advance()
        with self.assertRaises(RuntimeError):
            self.service.step()
        self.assertEqual(len(self.board.reads), 2)

    def test_offline_paused_refreshing_and_server_off_sessions_are_not_read(self):
        self.board.value['hosts'] = [host(row(), name='offline', ok=False),
                                     host(row(), name='paused', paused=True),
                                     host(row(), name='refreshing', refreshing=True),
                                     host(row(appServer='off'), name='server-off')]
        self.service.step()
        self.assertEqual(self.board.reads, [])
        self.assertIn(('paused', False), self.board.refreshes)
        self.assertNotIn(('refreshing', False), self.board.refreshes)

    def test_readable_history_without_app_server_updates_work_with_unknown_runtime(self):
        self.board.value['hosts'] = [host(row(phase='unknown', appServer='off', canRead=True, readOnly=True))]
        self.board.answer = {'phase': 'unknown', 'readOnly': True, 'runtimeKnown': False,
                             'messages': [{'role': 'assistant', 'messageId': 'a1', 'text': '현재: 저장된 진행 기록'}]}
        self.service.step()
        self.assertEqual(len(self.board.reads), 1)
        work = self.service.store.snapshot()['goals'][0]
        self.assertEqual(work['current'], '저장된 진행 기록')
        self.assertEqual(work['connection']['state'], 'connected')
        self.assertFalse(work['automation']['runtimeKnown'])
        self.assertEqual(work['latestResult'], '')

    def test_background_refresh_is_bounded_and_never_forced(self):
        self.board.value['hosts'] = [host(row(), name='mini'), host(row(), name='air', paused=True)]
        self.service.step()
        self.assertEqual(self.board.refreshes, [('mini', False), ('air', False)])
        self.clock.advance(119)
        self.service.step()
        self.assertEqual(len(self.board.refreshes), 2)
        self.clock.advance(1)
        self.service.step()
        self.assertEqual(self.board.refreshes, [('mini', False), ('air', False), ('mini', False), ('air', False)])

    def test_offline_machine_reconnects_only_after_real_board_backoff(self):
        import server
        calls = []
        reachable = {'value': False}
        machine = {'name': 'air', 'label': '합성 원격', 'local': False}
        def runner(host, args, timeout):
            calls.append(args)
            if not reachable['value']:
                raise RuntimeError('synthetic offline')
            return {'codex': [row(appServer='off')], 'claude': []}
        board = server.Board([machine], 60, runner=runner, live=False)
        board.clock = self.clock
        board.start_polls = lambda queued, full=False: [board.poll(h, full) for h in queued]
        self.assertFalse(board.poll(machine))
        self.assertEqual(board.backoff['air'], (1, 1090.0))
        self.service = WorkService(board, self.goals, self.directory, clock=self.clock)
        self.service.step()
        reachable['value'] = True
        # The board would retry after 90 seconds; this service asks every 120.
        self.clock.advance(119)
        self.service.step()
        self.assertEqual(len(calls), 1, 'offline host must not be repeatedly retried before its backoff')
        self.clock.advance(1)
        self.service.step()
        self.assertEqual(len(calls), 2)
        self.assertTrue(board.snapshot()['hosts'][0]['ok'])
        self.assertFalse(board.snapshot()['hosts'][0]['paused'])
        self.assertEqual(board.backoff, {})

    def test_initial_needs_input_then_working_then_recent_priority(self):
        self.board.value['hosts'] = [host(row('recent', phase='idle', updatedAt=300),
                                            row('active', updatedAt=200), row('attention', phase='needs_input'))]
        self.service.step()
        self.clock.advance()
        self.service.step()
        self.clock.advance()
        self.service.step()
        self.assertEqual([entry[2] for entry in self.board.reads], ['attention', 'active', 'recent'])

    def test_archive_only_changes_board_record_and_persists_after_sync(self):
        reference = source_ref('mini', 'codex', row())
        result = self.service.archive_session(reference)
        self.assertEqual(result, {'archived': True, 'changed': 1, 'executionChanged': False})
        self.assertTrue(self.active()[0]['archived'])
        self.assertEqual(self.board.reads, [])
        self.assertEqual(self.board.refreshes, [])
        self.assertEqual(self.board.value['hosts'][0]['data']['codex'][0]['phase'], 'working')
        result = self.service.archive_session(reference, False)
        self.assertFalse(self.active()[0]['archived'])
        self.assertFalse(result['executionChanged'])

    def test_claude_without_home_is_canonical_in_archive_and_manual_coverage(self):
        claude = row('claude-a', agent='claude', home=None)
        self.board.value['hosts'] = [host(claude)]
        reference = {'host': 'mini', 'agent': 'claude', 'id': 'claude-a'}
        self.assertEqual(source_ref('mini', 'claude', claude)['home'], '.claude')
        self.assertEqual(identity(reference), identity({**reference, 'home': '.claude'}))
        result = self.service.archive_session(reference)
        self.assertEqual(result['changed'], 1)
        self.assertTrue(self.active()[0]['archived'])

    def test_error_or_stale_conversation_does_not_replace_evidence(self):
        self.service.observe(source_ref('mini', 'codex', row()), self.board.answer)
        prior = self.service.store.snapshot()
        self.service.observe(source_ref('mini', 'codex', row()), {'error': 'synthetic', 'messages': []})
        self.service.observe(source_ref('mini', 'codex', row()), {'stale': True, 'messages': []})
        self.assertEqual(self.service.store.snapshot(), prior)

    def test_update_routes_existing_manual_and_automatic_ids(self):
        auto = self.active()[0]
        changed = self.service.update(auto['id'], {'revision': auto['revision'], 'title': '자동 작업 교정'})
        self.assertEqual(changed['goal']['title'], '자동 작업 교정')
        manual = self.goals.save({'title': '수동 작업'})['goal']
        changed = self.service.update(manual['id'], {'revision': manual['revision'], 'title': '수동 교정'})
        self.assertEqual(changed['goal']['title'], '수동 교정')

    def test_observer_lifecycle_has_one_thread_and_removes_listener(self):
        self.service.step = Mock()
        self.service.start()
        first = self.service.thread
        self.service.start()
        self.assertIs(self.service.thread, first)
        self.assertEqual(len(self.board.listeners), 1)
        self.service.close()
        self.assertFalse(first.is_alive())
        self.assertEqual(self.board.listeners, [])
        self.service.thread = None


if __name__ == '__main__':
    unittest.main()
