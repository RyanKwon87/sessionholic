"""Automatic work/session UI preview; every native/control response is synthetic.

Use a fresh private SESSIONHOLIC_INSTANCE and --state-dir, e.g. port 8796.
Existing goal files are never deleted or rewritten by this fixture. Native chat,
original-session control and snapshots use the in-memory fake below; no model,
SSH, app-server or real native control can run. The reused terminal is echo only.
"""
import copy
import hashlib
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_preview_fixture as fixture

ONE_SHOT_HISTORY = {'code': 'not_persisted', 'message': '대화 기록을 저장하지 않는 일회성 실행입니다. 실행 상태를 확인하고 결과는 요청한 에이전트에서 확인해 주세요.'}


class AutomaticPreviewNative(fixture.FakeNative):
    def __init__(self, rows):
        now = int(time.time())
        directory = Path(rows[0]['cwd']).parent
        sample_titles = ['모바일 입력 개선 결과', '검색과 복귀 동작 개선', '콘텐츠 초안 검토']
        for index, row in enumerate(rows):
            row.update(title=sample_titles[index] if index < len(sample_titles) else row['title'],
                       initiator='human' if index < 2 else 'unknown', initiatorEvidence='explicit' if index < 2 else 'unknown',
                       parentSessionId=None, project='합성 UI 검수', fixtureHost='local',
                       source='cli' if index < 2 else 'background')
        parent = rows[1]
        for number, title, phase, parent_id, host in [
            (101, '검색 회귀 확인 워커', 'working', parent['id'], 'local'),
            (102, '자료 수신 후 제안서 작성', 'idle', None, 'local'),
            (103, '다른 기기에서 진행하던 정리', 'working', None, 'offline-demo'),
        ]:
            cwd = directory / ('workspace-' + str(number)); cwd.mkdir(exist_ok=True, mode=0o700)
            rows.append({'id': f'019aaaaa-0000-7000-8000-{number:012d}', 'agent': 'codex', 'home': '.codex-isolated',
                         'title': title, 'phase': phase, 'cwd': str(cwd), 'kind': 'interactive',
                         'account': '검증 계정', 'project': '합성 UI 검수', 'updatedAt': now - number,
                         'snippet': '모델·SSH·native 제어 없는 합성 검수 세션입니다.', 'fixtureHost': host,
                         'initiator': 'agent' if parent_id else 'human', 'initiatorEvidence': 'explicit',
                         'parentSessionId': parent_id, 'agentName': '회귀 검토' if parent_id else None,
                         'initiatorName': '회귀 검토 에이전트' if parent_id else None,
                         'source': 'subagent' if parent_id else 'cli'})
        if os.environ.get('SESSIONHOLIC_RECOVERY_PREVIEW') == '1':
            for number, title, closed_by_board in [(104, '연결이 끝난 재개 가능 세션', False), (105, '웹보드에서 명시 종료한 세션', True)]:
                cwd = directory / ('workspace-' + str(number)); cwd.mkdir(exist_ok=True, mode=0o700)
                rows.append({'id': f'019aaaaa-0000-7000-8000-{number:012d}', 'agent': 'codex', 'home': '.codex-isolated',
                             'title': title, 'phase': 'closed', 'cwd': str(cwd), 'kind': 'interactive',
                             'account': '검증 계정', 'project': '합성 재개 검수', 'updatedAt': now - number,
                             'fixtureHost': 'local', 'fixtureClosedByBoard': closed_by_board,
                             'initiator': 'human', 'initiatorEvidence': 'explicit', 'source': 'vscode'})
        if os.environ.get('SESSIONHOLIC_ONESHOT_PREVIEW') == '1':
            cwd = directory / 'workspace-oneshot'; cwd.mkdir(exist_ok=True, mode=0o700)
            rows.append({'id': 'oneshot-native-000106', 'agent': 'claude', 'home': '.claude',
                         'sessionId': 'oneshot-native-000106', 'title': '일회성 리뷰 실행 샘플',
                         'phase': 'working', 'runtimeKnown': True, 'readOnly': True,
                         'cwd': str(cwd), 'kind': 'background', 'account': '검증 Claude',
                         'project': '합성 일회성 검수', 'updatedAt': now, 'fixtureHost': 'local',
                         'initiator': 'agent', 'initiatorName': 'Codex', 'initiatorEvidence': 'process_ancestry',
                         'executionMode': 'one-shot', 'transcriptAvailability': 'disabled',
                         'historyNotice': copy.deepcopy(ONE_SHOT_HISTORY)})
        super().__init__(rows)
        self.control_calls = []
        self.control_receipts = {}
        self.control_proofs = {}
        for index, row in enumerate(rows):
            if row.get('executionMode') == 'one-shot':
                self.messages[row['id']] = []
                continue
            self.messages[row['id']][0]['text'] = row['title']
            work = {'title': row['title'], 'current': '합성 화면에서 진행 상황을 확인하고 있어요.'}
            if index == 0:
                work.update(current='한글 입력과 첨부 표시를 확인했어요.', next='다음 화면에서 읽던 위치 확인')
            elif row['id'].endswith('000000000102'):
                work.update(status='waiting', waitingReason='파트너가 보낼 제품 자료를 기다립니다.',
                            current='필요한 자료 목록을 전달했어요.', next='자료 수신 후 제안서 구성')
            elif row['phase'] == 'needs_input':
                work.update(status='waiting', waitingReason='표현 두 가지 중 사용할 문구를 골라 주세요.',
                            current='콘텐츠 초안 두 가지를 준비했어요.', next='선택한 표현으로 초안 정리')
            elif row.get('parentSessionId'):
                work = {'current': '하위 워커가 검색 회귀를 확인하고 있어요.', 'next': '검토 결과를 부모 세션에 전달'}
            else:
                work.update(next='확인 결과를 대화에 남기기')
            self.messages[row['id']].append({'id': 'fixture-report-' + row['id'], 'role': 'assistant',
                                             'text': work['current'], 'work': work, 'ts': now - 5})

    def rpc(self, host, command, payload, timeout=20):
        if command not in ('control_preview', 'control_execute', 'resume_preview'):
            result = super().rpc(host, command, payload, timeout)
            if command == 'chat' and payload.get('action') == 'read' and self.rows[payload['source']['id']]['phase'] == 'closed':
                result['capabilities'] = {'canSend': False, 'reason': '연결이 끝난 합성 세션입니다.'}
            if command == 'chat' and payload.get('action') == 'read' and self.rows[payload['source']['id']].get('executionMode') == 'one-shot':
                result.update(messages=[], executionMode='one-shot', transcriptAvailability='disabled',
                              historyNotice=copy.deepcopy(ONE_SHOT_HISTORY), readOnly=True,
                              capabilities={'canSend': False, 'reason': '일회성 실행에는 후속 메시지를 보낼 수 없습니다.'})
            return result
        source = payload['source']; row = self.rows[source['id']]
        if source.get('agent') != row['agent'] or source.get('home') != row['home'] or host['name'] != row['fixtureHost']:
            raise ValueError('합성 제어 대상이 다릅니다.')
        with self.lock:
            if command == 'resume_preview':
                blocked = row.get('fixtureClosedByBoard', False)
                return {'allowed': not blocked, 'blockedByBoard': blocked,
                        'reason': '웹보드에서 명시적으로 종료한 세션입니다.' if blocked else None}
            if command == 'control_preview':
                proof = 'synthetic.' + hashlib.sha256((row['id'] + row['phase'] + str(time.time_ns())).encode()).hexdigest()
                self.control_proofs[proof] = (row['id'], row['phase'])
                return {'source': copy.deepcopy(source), 'targetProof': proof, 'expiresAt': time.time() + 45,
                        'capabilities': {
                            'interrupt': {'supported': row['agent'] == 'codex' and row['phase'] == 'working',
                                          'reason': None if row['agent'] == 'codex' and row['phase'] == 'working' else '현재 합성 세션은 응답 중단을 지원하지 않습니다.'},
                            'terminate': {'supported': row['phase'] != 'closed', 'reason': None,
                                          'effect': '선택한 원본 일회성 실행을 종료합니다.' if row.get('executionMode') == 'one-shot' else '선택한 세션과 하위 에이전트를 종료하고 대화 기록을 보관합니다.'}}}
            rid = payload['requestId']
            if rid in self.control_receipts:
                return copy.deepcopy(self.control_receipts[rid])
            if self.control_proofs.get(payload.get('targetProof')) != (row['id'], row['phase']):
                return {'confirmed': False, 'applied': False, 'status': 'stale', 'reason': '합성 대상 상태가 바뀌었습니다.'}
            action = payload['action']
            if action not in ('interrupt', 'terminate'):
                raise ValueError('합성 제어 동작이 올바르지 않습니다.')
            if action == 'interrupt' and (row['agent'] != 'codex' or row['phase'] != 'working'):
                raise ValueError('합성 응답 중단을 지원하지 않습니다.')
            self.control_calls.append({'requestId': rid, 'source': copy.deepcopy(source), 'action': action})
            row['phase'] = 'idle' if action == 'interrupt' else 'closed'
            if action == 'terminate': row['fixtureClosedByBoard'] = True
            row['updatedAt'] = int(time.time())
            self.running.pop(row['id'], None)
            result = {'requestId': rid, 'confirmed': True, 'applied': True,
                      'status': 'interrupted' if action == 'interrupt' else 'terminated', 'synthetic': True}
            self.control_receipts[rid] = result
            return copy.deepcopy(result)


class SyntheticBotRequests:
    """Metadata-only source fixtures; refresh never starts a native process."""
    def __init__(self, native):
        self.native = native
        self.reads = 0
        self.refreshes = 0

    def snapshot(self, force=False):
        self.reads += 1
        self.refreshes += int(force)
        now = int(time.time())
        linked = next(row for row in self.native.rows.values() if row['id'].endswith('000000000001'))
        requests = [
            {'id': 'holic-team-request', 'sourceId': 'holic-team', 'botName': '홀릭이 팀 봇', 'host': 'local',
             'channelLabel': '팀 업무 채널', 'requesterLabel': '합성 팀원', 'authorityLabel': '팀원 권한',
             'scopeLabel': '공유된 업무 프로젝트', 'title': '팀 제품 자료 정리 요청', 'status': 'running', 'updatedAt': now,
             'nativeRef': {'agent': 'codex', 'id': linked['id'], 'home': linked['home']}},
            {'id': 'hermes-slack-request', 'sourceId': 'hermes-slack', 'botName': 'Hermes', 'host': 'local',
             'channelLabel': 'Slack · 콘텐츠 채널', 'requesterLabel': '합성 요청자', 'authorityLabel': '콘텐츠 열람',
             'scopeLabel': '콘텐츠 채널 자료', 'title': '행사 안내 문구 확인', 'status': 'completed', 'updatedAt': now - 30,
             'nativeRef': None},
            {'id': 'studio-pc-request', 'sourceId': 'studio-pc', 'botName': '스튜디오', 'host': 'offline-demo',
             'channelLabel': 'PC 봇 요청', 'requesterLabel': None, 'authorityLabel': None,
             'scopeLabel': '스튜디오 제품 작업공간', 'title': '제품 설명 번역 요청', 'status': 'pending', 'updatedAt': now - 60,
             'nativeRef': None, 'stale': True},
        ]
        sources = [{'id': key, 'label': label, 'host': host, 'ok': True, 'error': None, 'updatedAt': now}
                   for key, label, host in [('holic-team', '홀릭이 팀 봇', 'local'), ('hermes-slack', 'Hermes Slack', 'local'), ('studio-pc', '작업실 PC', 'offline-demo')]]
        sources[-1].update(ok=False, stale=True, error='합성 봇 연결을 확인하지 못했습니다.')
        sources.append({'id': 'schema-error-demo', 'label': '형식 확인 필요 샘플', 'host': 'local', 'ok': False,
                        'error': '요청 목록 형식을 확인하지 못했습니다.', 'code': 'schema_error', 'updatedAt': now})
        return {'requests': requests, 'sources': sources, 'fetchedAt': now}


def main():
    make_server = fixture.server.make_server

    def seeded(*args, **kwargs):
        # Configure the inert runner before the board's observer can start.
        kwargs['automatic_work'] = False
        httpd = make_server(*args, **kwargs)
        fake = httpd.workflow.transfers.rpc.__self__
        httpd.bot_requests = SyntheticBotRequests(fake)
        httpd.workflow.bot_requests = httpd.bot_requests
        if os.environ.get('SESSIONHOLIC_ONESHOT_PREVIEW') == '1':
            httpd.board.hosts[0]['label'] = '합성 Windows PC'
            httpd.board.state['local']['label'] = '합성 Windows PC'
        offline = {'name': 'offline-demo', 'label': '오프라인 샘플', 'local': True}
        httpd.board.hosts.append(offline)
        httpd.board.host_locks[offline['name']] = threading.Lock()

        def snapshot(host_name):
            rows = [row for row in fake.rows.values() if row['fixtureHost'] == host_name]
            return {'codex': [row for row in rows if row['agent'] == 'codex'],
                    'claude': [row for row in rows if row['agent'] == 'claude'], 'tmux': [], 'errors': []}

        def synthetic_runner(host, command, timeout):
            if host['name'] == offline['name']:
                raise RuntimeError('합성 오프라인 기기입니다.')
            if command[0] == 'snapshot':
                return snapshot(host['name'])
            if command[0] == 'read':
                row = fake.rows[command[2]]
                result = {'messages': copy.deepcopy(fake.messages[row['id']]), 'phase': row['phase']}
                if row.get('executionMode') == 'one-shot':
                    result.update(executionMode='one-shot', transcriptAvailability='disabled', historyNotice=copy.deepcopy(ONE_SHOT_HISTORY), readOnly=True)
                return result
            raise ValueError('합성 수집 범위 밖입니다.')

        httpd.board.runner = synthetic_runner
        for host in httpd.board.state.values():
            host['data'] = snapshot(host['name'])
        httpd.board.state[offline['name']] = {
            'name': offline['name'], 'label': offline['label'], 'ok': False, 'error': '합성 오프라인 기기',
            'data': snapshot(offline['name']), 'fetchedAt': int(time.time()) - 1800, 'durationMs': None,
            'paused': True, 'refreshing': False, 'live': False,
        }
        # Seed through the real automatic observer contract, never Goals.save.
        # Observe the offline sample once online, then retain its prior work state.
        observed = [{**row, 'host': row['fixtureHost'], 'online': True} for row in fake.rows.values()]
        httpd.work.store.sync(observed)
        for row in observed:
            source = {key: row[key] for key in ('host', 'agent', 'home', 'id')}
            httpd.work.observe(source, {'phase': row['phase'], 'messages': copy.deepcopy(fake.messages[row['id']])})
        httpd.work.store.sync(httpd.board.snapshot())
        httpd.work.start()
        return httpd

    with patch.object(fixture, 'FakeNative', AutomaticPreviewNative), patch.object(fixture.server, 'make_server', seeded):
        fixture.main()


if __name__ == '__main__':
    main()
