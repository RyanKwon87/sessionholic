import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automatic_work import AutomaticWork, _labels
from goals import GoalError


def source(identifier='session-a', **extra):
    return {'host': 'mini', 'agent': 'codex', 'home': '.codex-example', 'id': identifier, **extra}


def row(identifier='session-a', **extra):
    return {**source(identifier), 'title': '목록의 작업', 'phase': 'working', 'project': '예제', **extra}


def snapshot(*rows, ok=True, host='mini'):
    return {'hosts': [{'name': host, 'ok': ok, 'data': {
        agent: [r for r in rows if r['agent'] == agent] for agent in ('codex', 'claude')}}]}


def msg(role, text, identifier, **extra):
    return {'role': role, 'text': text, 'id': identifier, **extra}


class AutomaticWorkTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        self.store = AutomaticWork(self.directory)

    def tearDown(self):
        self.temporary.cleanup()

    def active(self, result=None):
        return [goal for goal in (result or self.store.snapshot())['goals']
                if not goal['automation'].get('mergedInto')]

    def goal(self):
        return self.active()[0]

    def observe(self, messages, phase='idle', **extra):
        return self.store.observe(source(), {'phase': phase, 'messages': messages, **extra})

    def assert_error(self, code, fn):
        with self.assertRaises(GoalError) as error:
            fn()
        self.assertEqual(error.exception.payload['code'], code)

    def test_immediate_tasks_without_registration_or_conversation(self):
        got = self.store.sync(snapshot(row(), row('session-b')))
        self.assertEqual(len(self.active(got)), 2)
        self.assertEqual(self.goal()['title'], '목록의 작업')
        self.assertEqual(self.goal()['status'], 'in_progress')
        self.assertEqual(self.goal()['activity'], 'active')
        self.assertTrue(self.goal()['automatic'])
        self.assertEqual(self.goal()['next'], '')

    def test_stable_identity_and_repeated_snapshot_does_not_write(self):
        self.store.sync(snapshot(row()))
        prior = self.store.snapshot()
        with patch.object(self.store, '_write', side_effect=AssertionError('duplicate write')):
            self.assertEqual(self.store.sync(snapshot(row())), prior)
        another = AutomaticWork(self.directory)
        self.assertEqual(another.sync(snapshot(row())), prior)

    def test_exact_host_agent_home_identity_keeps_four_tasks(self):
        self.store.sync([row(), row(home='.codex-other'), row(host='air'), row(agent='claude', home='.claude')])
        self.assertEqual(len(self.active()), 4)

    def test_same_project_and_title_never_merge(self):
        self.store.sync(snapshot(row(), row('session-b', cwd='/same/project')))
        self.assertEqual(len(self.active()), 2)

    def test_worker_parents_merge_only_same_scope_even_out_of_order(self):
        self.store.sync([row('child', parentSessionId='parent'), row('parent'),
                         row('elsewhere', parentSessionId='parent', home='.codex-other')])
        active = self.active()
        self.assertEqual(len(active), 2)
        joined = next(g for g in active if len(g['sessions']) == 2)
        self.assertEqual({r['id'] for r in joined['sessions']}, {'child', 'parent'})
        self.assertEqual(joined['automation']['relation']['kind'], 'parent_session')
        before = self.store.snapshot()
        self.assertEqual(self.store.sync([row('child', parentSessionId='parent'), row('parent'),
                                        row('elsewhere', parentSessionId='parent', home='.codex-other')]), before)

    def test_parent_chain_and_cycle(self):
        self.store.sync([row('grandchild', parentSessionId='child'), row('child', parentSessionId='parent'), row('parent')])
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(len(self.goal()['sessions']), 3)
        separate = AutomaticWork(self.directory / 'separate')
        separate.sync([row('cycle-a', parentSessionId='cycle-b'), row('cycle-b', parentSessionId='cycle-a')])
        self.assertEqual(len([g for g in separate.snapshot()['goals'] if not g['archived']]), 2)

    def test_late_parent_keeps_human_correction(self):
        self.store.sync([row('child', parentSessionId='parent')])
        old = self.goal()
        self.store.update(old['id'], {'revision': old['revision'], 'title': '내가 정한 이름', 'pinned': True})
        self.store.sync([row('parent'), row('child', parentSessionId='parent')])
        self.assertEqual(self.goal()['title'], '내가 정한 이름')
        self.assertTrue(self.goal()['pinned'])

    def test_first_request_title_and_progress_labels_have_source_evidence(self):
        self.store.sync(snapshot(row()))
        self.observe([msg('user', '모바일 화면 수정해줘\n검색 동작까지 확인', 'u1'),
                      msg('assistant', '현재: 검색창 수정 중\n\n다음 단계:\n태블릿 검증', 'a1')], 'working')
        goal = self.goal()
        self.assertEqual(goal['title'], '모바일 화면 수정해줘')
        self.assertEqual(goal['current'], '검색창 수정 중')
        self.assertEqual(goal['next'], '태블릿 검증')
        self.assertEqual(goal['automation']['fieldEvidence']['next']['messageId'], 'a1')
        self.store.sync(snapshot(row(title='목록 제목 바뀜', preview='과거 미리보기')))
        self.assertEqual(self.goal()['current'], '검색창 수정 중')
        self.assertEqual(self.goal()['title'], '모바일 화면 수정해줘')

    def test_plain_answer_is_result_never_automatic_completion(self):
        self.observe([msg('user', '기능 만들어줘', 'u1'), msg('assistant', '모두 완료했습니다.', 'a1')])
        goal = self.goal()
        self.assertEqual(goal['status'], 'review')
        self.assertEqual(goal['activity'], 'result')
        self.assertEqual(goal['latestResult'], '모두 완료했습니다.')
        self.assertEqual(goal['next'], '')
        self.assertEqual(goal['completionCriteria'], '')

    def test_identical_conversation_and_unrelated_receipts_do_not_write(self):
        messages = [msg('user', '작성해줘', 'u1'), msg('assistant', '초안입니다.', 'a1')]
        self.observe(messages)
        before = self.store.snapshot()
        with patch.object(self.store, '_write', side_effect=AssertionError('duplicate write')):
            self.assertEqual(self.observe(messages, receipts=[{'delivery': 'completed'}]), before)

    def test_later_user_message_does_not_rename_or_create_result(self):
        self.observe([msg('user', '첫 작업', 'u1'), msg('assistant', '첫 결과', 'a1')])
        self.observe([msg('user', '첫 작업', 'u1'), msg('assistant', '첫 결과', 'a1'),
                      msg('user', '한 가지만 더 고쳐줘', 'u2')], 'working')
        self.assertEqual(self.goal()['title'], '첫 작업')
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(self.goal()['latestResult'], '첫 결과')

    def test_structured_completion_requires_criteria_and_evidence(self):
        self.observe([], work={'completed': True})
        self.assertNotEqual(self.goal()['status'], 'done')
        self.observe([], work={'status': 'done', 'completionCriteria': '기능 검증', 'evidence': '합성 검증 통과'})
        self.assertEqual(self.goal()['status'], 'done')
        self.store.sync(snapshot(row()))
        self.observe([msg('user', '고마워', 'u2')], 'working')
        self.assertEqual(self.goal()['status'], 'done')

    def test_final_answer_precedes_later_commentary_as_latest_result(self):
        self.observe([msg('user', '고쳐줘', 'u1', turnId='turn-one'),
                      msg('assistant', '구현 결과', 'a1', phase='final_answer', turnId='turn-one'),
                      msg('assistant', '추가 확인 중', 'a2', phase='commentary', turnId='turn-one')])
        self.assertEqual(self.goal()['latestResult'], '구현 결과')
        self.assertEqual(self.goal()['automation']['fieldEvidence']['current']['turnId'], 'turn-one')
        self.observe([msg('user', '계속 확인해줘', 'u2'),
                      msg('assistant', '조사 중', 'a3', phase='commentary')])
        self.assertEqual(self.goal()['latestResult'], '구현 결과')

    def test_receipts_and_phase_done_never_complete(self):
        self.observe([], phase='done', receipts=[{'delivery': 'completed'}])
        self.assertNotEqual(self.goal()['status'], 'done')

    def test_explicit_wait_reason_and_answer_prompt(self):
        self.store.sync(snapshot(row(phase='idle', updatedAt=1)))
        self.observe([msg('assistant', '대기 이유: 외부 자료 도착 대기', 'a1')],
                     work={'status': 'waiting'})
        self.assertEqual(self.goal()['activity'], 'waiting')
        self.observe([msg('assistant', '대기 이유: 외부 자료 도착 대기', 'a1')], 'needs_input',
                     prompt={'question': '두 초안 중 하나를 선택해 주세요.'})
        self.assertEqual(self.goal()['activity'], 'attention')
        self.assertEqual(self.goal()['waitingReason'], '두 초안 중 하나를 선택해 주세요.')
        self.store.sync(snapshot(row()))
        self.assertEqual(self.goal()['waitingReason'], '')

    def test_repeated_old_list_cannot_undo_new_conversation_phase(self):
        self.store.sync(snapshot(row(phase='working', updatedAt=100)))
        self.observe([msg('assistant', '새 결과', 'a1')], 'idle')
        self.store.sync(snapshot(row(phase='working', updatedAt=100)))
        self.assertEqual(self.goal()['activity'], 'result')
        self.assertEqual(self.goal()['status'], 'review')
        self.store.sync(snapshot(row(phase='working', updatedAt=101)))
        self.assertEqual(self.goal()['activity'], 'active')

    def test_first_list_does_not_undo_conversation_read_before_list(self):
        self.observe([msg('assistant', '새 결과', 'a1')], 'idle')
        self.store.sync(snapshot(row(phase='working', updatedAt=100)))
        self.assertEqual(self.goal()['activity'], 'result')

    def test_offline_preserves_last_work_status_and_result(self):
        self.observe([msg('assistant', '검토할 초안', 'a1')])
        prior = self.goal()
        self.store.sync(snapshot(row(phase='working'), ok=False))
        goal = self.goal()
        self.assertEqual(goal['status'], prior['status'])
        self.assertEqual(goal['activity'], prior['activity'])
        self.assertEqual(goal['latestResult'], prior['latestResult'])
        self.assertEqual(goal['connection']['state'], 'offline')
        self.assertTrue(goal['connection']['lastKnown'])

    def test_readable_history_with_unknown_runtime_updates_evidence_without_new_result(self):
        self.store.sync(snapshot(row(phase='unknown')))
        self.observe([msg('user', 'PC 기능 점검', 'u1'), msg('assistant', '현재: 기록으로 확인한 진행', 'a1')],
                     'unknown', readOnly=True, runtimeKnown=False)
        goal = self.goal()
        self.assertEqual(goal['title'], 'PC 기능 점검')
        self.assertEqual(goal['current'], '기록으로 확인한 진행')
        self.assertEqual(goal['connection']['state'], 'connected')
        self.assertFalse(goal['automation']['runtimeKnown'])
        self.assertEqual(goal['activity'], 'recent')
        self.assertEqual(goal['latestResult'], '')
        self.assertNotEqual(goal['status'], 'done')

    def test_unknown_runtime_preserves_prior_result_without_claiming_new_completion(self):
        self.observe([msg('assistant', '기존 결과', 'a1')])
        before = self.goal()
        self.observe([msg('assistant', '현재: 새 기록 내용', 'a2')], 'unknown', readOnly=True)
        goal = self.goal()
        self.assertEqual(goal['latestResult'], before['latestResult'])
        self.assertEqual(goal['resultAt'], before['resultAt'])
        self.assertEqual(goal['current'], '새 기록 내용')
        self.assertEqual(goal['connection']['state'], 'connected')
        self.assertEqual(goal['activity'], 'recent')

    def test_unavailable_conversation_keeps_previous_content(self):
        self.observe([msg('assistant', '작성 결과', 'a1')])
        self.observe([], 'unavailable')
        self.assertEqual(self.goal()['latestResult'], '작성 결과')
        self.assertEqual(self.goal()['status'], 'review')

    def test_user_corrections_archive_pin_and_status_survive_refresh(self):
        self.store.sync(snapshot(row()))
        goal = self.goal()
        self.store.update(goal['id'], {'revision': goal['revision'], 'title': '직접 이름', 'current': '직접 메모',
                                      'pinned': True, 'archived': True, 'status': 'done'})
        self.observe([msg('user', '자동 이름', 'u1'), msg('assistant', '현재: 자동 내용', 'a1')], 'working')
        goal = self.goal()
        self.assertEqual((goal['title'], goal['current'], goal['status']), ('직접 이름', '직접 메모', 'done'))
        self.assertTrue(goal['pinned'] and goal['archived'])

    def test_revision_conflict_does_not_overwrite(self):
        self.store.sync(snapshot(row()))
        goal = self.goal()
        self.store.update(goal['id'], {'revision': goal['revision'], 'title': '새 이름'})
        before = self.store.snapshot()
        self.assert_error('goal_conflict', lambda: self.store.update(goal['id'], {'revision': goal['revision'], 'title': '오래된 이름'}))
        self.assertEqual(self.store.snapshot(), before)

    def test_user_merge_and_split_survive_parent_refresh(self):
        self.store.sync([row('parent'), row('child', parentSessionId='parent')])
        goal = self.goal()
        split = self.store.update(goal['id'], {'revision': goal['revision'], 'action': 'split', 'session': source('child')})['goal']
        self.store.sync([row('parent'), row('child', parentSessionId='parent')])
        self.assertEqual(len(self.active()), 2)
        parent = next(g for g in self.active() if g['id'] != split['id'])
        current_split = next(g for g in self.active() if g['id'] == split['id'])
        self.store.update(parent['id'], {'revision': parent['revision'], 'action': 'merge',
                                       'targetId': split['id'], 'targetRevision': current_split['revision']})
        self.store.sync([row('parent'), row('child', parentSessionId='parent')])
        self.assertEqual(len(self.active()), 1)

    def test_confirmed_handoff_cross_host_is_explicit_and_idempotent(self):
        self.store.sync([row()])
        target = source('destination', host='air', home='.codex-other')
        self.assertEqual(self.store.link_sessions(source(), target), 1)
        self.assertEqual(len(self.goal()['sessions']), 2)
        before = self.store.snapshot()
        self.assertEqual(self.store.link_sessions(source(), target), 0)
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.store.link_sessions(source('missing'), target), 0)

    def test_new_task_command_creates_segment_and_replay_is_stable(self):
        initial = [msg('user', '첫 작업', 'u1'), msg('assistant', '첫 결과', 'a1')]
        self.observe(initial)
        messages = initial + [msg('user', '새 작업: 두 번째 작업', 'u2'), msg('assistant', '둘째 결과', 'a2')]
        self.observe(messages)
        tasks = self.active()
        self.assertEqual(len(tasks), 2)
        self.assertEqual({g['title'] for g in tasks}, {'첫 작업', '두 번째 작업'})
        new = next(g for g in tasks if g['title'] == '두 번째 작업')
        self.assertEqual(new['sessions'][0]['point'], 'message:u2')
        self.observe(messages + [msg('user', '두 번째의 후속 지시', 'u3')], 'working')
        self.assertEqual(len(self.active()), 2)
        new = next(g for g in self.active() if g['title'] == '두 번째 작업')
        self.assertEqual(new['latestResult'], '둘째 결과')
        self.assertEqual(new['status'], 'in_progress')
        self.assertEqual(new['sessions'][0]['point'], 'message:u2')
        self.assertNotEqual(new['automation']['previousWorkId'], new['id'])

    def test_fenced_or_quoted_labels_cannot_change_work(self):
        labels = _labels('```\n다음: 실행하지 않음\n```\n> 완료 근거: 인용\n현재: 실제 보고\n\n## Next steps\n확인하기')
        self.assertEqual(labels, {'current': '실제 보고', 'next': '확인하기'})

    def test_answer_request_uses_limited_inference_with_visible_evidence(self):
        self.observe([msg('assistant', '어느 초안을 사용할까요?', 'a1')])
        self.assertEqual(self.goal()['activity'], 'attention')
        self.assertEqual(self.goal()['automation']['fieldEvidence']['waitingReason']['confidence'], 'inferred')
        self.observe([msg('assistant', '어느 초안을 사용할까요?', 'a1'), msg('user', '첫 번째', 'u1')], 'working')
        self.assertEqual(self.goal()['activity'], 'active')
        self.assertEqual(self.goal()['waitingReason'], '')
        self.observe([msg('assistant', '원인은 무엇일까? 조사 결과는 아래와 같습니다.', 'a2')])
        self.assertEqual(self.goal()['activity'], 'result')

    def test_offline_host_with_no_cached_rows_marks_prior_tasks_offline(self):
        self.store.sync(snapshot(row()))
        self.store.sync({'hosts': [{'name': 'mini', 'ok': False, 'data': None}]})
        self.assertEqual(self.goal()['connection']['state'], 'offline')
        self.assertEqual(self.goal()['status'], 'in_progress')

    def test_reset_override_restores_latest_observed_suggestion(self):
        self.observe([msg('assistant', '현재: 첫 자동 내용', 'a1')], 'working')
        goal = self.goal()
        self.store.update(goal['id'], {'revision': goal['revision'], 'current': '사람의 내용'})
        self.observe([msg('assistant', '현재: 나중 자동 내용', 'a2')], 'working')
        goal = self.goal()
        self.assertEqual(goal['current'], '사람의 내용')
        result = self.store.update(goal['id'], {'revision': goal['revision'], 'resetOverrides': ['current']})
        self.assertEqual(result['goal']['current'], '나중 자동 내용')

    def test_private_storage_and_no_full_transcript(self):
        self.observe([msg('user', '요청', 'u1'), msg('tool', 'PRIVATE TOOL TRACE', 't1'), msg('assistant', '보고', 'a1')])
        for path in (self.store.path, self.store.lock_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn('PRIVATE TOOL TRACE', self.store.path.read_text())
        self.assertFalse((self.directory / 'goals.json').exists())

    def test_corrupt_or_insecure_storage_is_preserved(self):
        self.store.path.write_text('{broken')
        self.store.path.chmod(0o600)
        before = self.store.path.read_bytes()
        self.assert_error('automatic_work_unavailable', lambda: self.store.sync(snapshot(row())))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.path.unlink()
        self.store.sync(snapshot(row()))
        self.store.path.chmod(0o644)
        self.assert_error('automatic_work_unavailable', self.store.snapshot)

    def test_structurally_corrupt_record_is_not_replaced(self):
        self.store.sync(snapshot(row()))
        data = json.loads(self.store.path.read_text())
        data['goals'][0]['createdAt'] = None
        self.store.path.write_text(json.dumps(data))
        before = self.store.path.read_bytes()
        self.assert_error('automatic_work_unavailable', lambda: self.store.sync(snapshot(row())))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_symlink_or_shared_lock_does_not_modify_other_files(self):
        victim = self.directory / 'external-record'
        victim.write_text('unchanged')
        victim.chmod(0o600)
        self.store.path.symlink_to(victim)
        self.assert_error('automatic_work_unavailable', self.store.snapshot)
        self.assertEqual(victim.read_text(), 'unchanged')
        self.store.path.unlink()
        self.store.lock_path.unlink(missing_ok=True)
        os.link(victim, self.store.lock_path)
        self.assert_error('automatic_work_unavailable', self.store.snapshot)
        self.assertEqual(victim.read_text(), 'unchanged')

    def test_atomic_failed_save_keeps_old_file(self):
        self.store.sync(snapshot(row()))
        before = self.store.path.read_bytes()
        with patch('automatic_work.os.replace', side_effect=OSError('fixture failure')):
            self.assert_error('automatic_work_unavailable', lambda: self.store.sync(snapshot(row(), row('second'))))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob('.automatic-work-*.tmp')), [])

    def test_two_instances_do_not_lose_independent_sessions(self):
        stores = [AutomaticWork(self.directory), AutomaticWork(self.directory)]
        errors = []
        def add(index):
            try:
                for n in range(5):
                    stores[index].sync([row('session-%s-%s' % (index, n))])
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=add, args=(index,)) for index in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.active()), 10)

    def test_external_claude_interactive_uses_valid_native_session_id(self):
        native = row('', agent='claude', home=None, kind='interactive', sessionId='019aaaaa-0000-7000-8000-000000000003')
        before = copy.deepcopy(native)
        self.store.sync([native, row('', agent='claude', kind='interactive', sessionId='../bad'),
                         row('', agent='claude', kind='background', sessionId='019aaaaa-0000-7000-8000-000000000004')])
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(self.goal()['sessions'][0]['id'], native['sessionId'])
        self.assertEqual(native, before)

    def test_malformed_rows_are_skipped_without_guessing_identity(self):
        self.store.sync([{'host': 'mini', 'agent': 'codex'}, row(home='../bad'), row()])
        self.assertEqual(len(self.active()), 1)


if __name__ == '__main__':
    unittest.main()
