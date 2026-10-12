"""Authenticated automatic-work routes against an inert local HTTP server."""
import copy
import unittest
from unittest.mock import patch

import test_goals_http as fixture

REF = {'host': 'fixture', 'agent': 'codex', 'home': '.codex-example', 'id': 'fixture-session'}
ROW = {**REF, 'title': '자동 발견한 작업', 'phase': 'working'}
SNAPSHOT = {'version': 1, 'hosts': [{'name': 'fixture', 'ok': True, 'data': {'codex': [ROW], 'claude': []}}]}


# The same isolated configuration folder as the goal routes' tests.
setUpModule = fixture.setUpModule


class WorkHTTPTest(unittest.TestCase):
    setUp = fixture.GoalsHTTPTest.setUp
    tearDown = fixture.GoalsHTTPTest.tearDown
    request = fixture.GoalsHTTPTest.request

    def work(self):
        response, result = self.request('GET', '/api/work')
        self.assertEqual(response.status, 200, result)
        return result

    def test_authentication_csrf_and_cross_origin_are_required(self):
        response, _ = self.request('GET', '/api/work', authenticated=False)
        self.assertEqual(response.status, 401)
        with patch.object(self.board, 'snapshot', return_value=SNAPSHOT):
            work = self.work()['goals'][0]
            path = '/api/work/' + work['id']
            response, _ = self.request('POST', path, {'revision': work['revision'], 'title': '무인증'}, authenticated=False)
            self.assertEqual(response.status, 401)
            response, result = self.request('POST', path, {'revision': work['revision'], 'title': '무CSRF'}, csrf=False)
            self.assertEqual((response.status, result['code']), (403, 'csrf_expired'))
            response, _ = self.request('POST', path, {'revision': work['revision'], 'title': '외부'}, extra={'Origin': 'https://example.invalid'})
            self.assertEqual(response.status, 403)

    def test_snapshot_creates_cards_without_conversation_reads_and_keeps_manual(self):
        manual = self.httpd.goals.save({'title': '기존 수동 작업'})['goal']
        before = self.httpd.goals.path.read_bytes()
        with patch.object(self.board, 'snapshot', return_value=SNAPSHOT), \
                patch.object(self.board, 'read', side_effect=AssertionError('native read')):
            result = self.work()
        self.assertEqual(len(result['goals']), 2)
        self.assertEqual({g['automatic'] for g in result['goals']}, {False, True})
        self.assertIn(manual['id'], [g['id'] for g in result['goals']])
        self.assertEqual(self.httpd.goals.path.read_bytes(), before)

    def test_partial_edit_conflict_and_archival_preserve_execution(self):
        with patch.object(self.board, 'snapshot', return_value=SNAPSHOT):
            work = self.work()['goals'][0]
            path = '/api/work/' + work['id']
            response, result = self.request('POST', path, {'revision': work['revision'], 'title': '교정한 작업', 'pinned': True})
            self.assertEqual(response.status, 200, result)
            self.assertEqual(result['goal']['current'], work['current'])
            changed = result['goal']
            response, result = self.request('POST', path, {'revision': work['revision'], 'title': '오래된 교정'})
            self.assertEqual((response.status, result['code']), (409, 'goal_conflict'))
            response, result = self.request('POST', path, {'revision': changed['revision'], 'archived': True})
            self.assertEqual(response.status, 200, result)
            self.assertTrue(self.work()['goals'][0]['archived'])
            self.assertEqual(self.work()['goals'][0]['title'], '교정한 작업')
            self.assertEqual(SNAPSHOT['hosts'][0]['data']['codex'][0]['phase'], 'working')

    def test_chat_state_observes_result_but_completed_turn_never_completes_work(self):
        state = {'route': REF, 'phase': 'idle', 'messages': [
            {'role': 'user', 'messageId': 'user-one', 'text': '새 기능을 만들어 줘'},
            {'role': 'assistant', 'messageId': 'answer-one', 'turnId': 'turn-one', 'turnStatus': 'completed', 'text': '첫 구현 결과입니다.'}],
            'receipts': [{'delivery': 'completed'}]}
        with patch.object(self.board, 'snapshot', return_value=SNAPSHOT), patch.object(self.httpd.chat, 'state', return_value=state):
            response, returned = self.request('POST', '/api/chat/state', {'source': REF})
            self.assertEqual((response.status, returned), (200, state))
            goals = self.httpd.work.store.snapshot()['goals']
            self.assertEqual(goals[0]['latestResult'], '첫 구현 결과입니다.')
            self.assertNotEqual(goals[0]['status'], 'done')
            self.assertEqual(goals[0]['automation']['fieldEvidence']['title']['messageId'], 'user-one')

    def test_standalone_read_observes_native_idle_as_result_not_completion(self):
        read = {'phase': 'idle', 'messages': [{'role': 'assistant', 'messageId': 'answer-one', 'text': '읽기 결과'}]}
        with patch.object(self.board, 'read', return_value=read):
            response, result = self.request('GET', '/api/read?host=fixture&agent=codex&home=.codex-example&id=fixture-session')
        self.assertEqual((response.status, result), (200, read))
        work = self.httpd.work.store.snapshot()['goals'][0]
        self.assertEqual(work['activity'], 'result')
        self.assertEqual(work['latestResult'], '읽기 결과')
        self.assertNotEqual(work['status'], 'done')

    def test_corrupt_automatic_store_preserved_and_conversation_still_reads(self):
        with patch.object(self.board, 'snapshot', return_value=SNAPSHOT):
            self.work()
        path = self.httpd.work.store.path
        path.write_text('invalid private fixture')
        response, result = self.request('GET', '/api/work')
        self.assertEqual((response.status, result['code']), (503, 'automatic_work_unavailable'))
        self.assertNotIn('invalid private fixture', str(result))
        state = {'route': REF, 'phase': 'idle', 'messages': [{'role': 'assistant', 'text': '대화는 계속 열림'}]}
        with patch.object(self.httpd.chat, 'state', return_value=state):
            response, result = self.request('POST', '/api/chat/state', {'source': REF})
        self.assertEqual((response.status, result), (200, state))
        self.assertEqual(path.read_text(), 'invalid private fixture')
        self.assertEqual(self.httpd.work.health['state'], 'limited')

    def test_bearer_read_does_not_cache_private_work(self):
        response, result = self.request('GET', '/api/work', authenticated=False, extra={'Authorization': 'Bearer fixture-token'})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertEqual(result['goals'], [])

    def test_same_partial_route_edits_manual_record(self):
        work = self.httpd.goals.save({'title': '기존 수동 작업'})['goal']
        response, result = self.request('POST', '/api/work/' + work['id'], {'revision': work['revision'], 'title': '수동 작업 교정'})
        self.assertEqual(response.status, 200)
        self.assertEqual(result['goal']['title'], '수동 작업 교정')
        self.assertFalse(self.work()['goals'][0]['automatic'])


if __name__ == '__main__':
    unittest.main()
