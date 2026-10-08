"""Loopback browser QA: native chat RPC is synthetic; attachments use the real store.

No credentials, model process, native daemon or SSH is opened. The terminal is
an ordinary local echo PTY. Never use this fixture as a production server.
"""
import argparse
import base64
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import attachments
import launch
import server
from terminal import TerminalManager


class FakeNative:
    def __init__(self, rows):
        self.rows = {row['id']: row for row in rows}
        self.messages = {row['id']: [
            {'role': 'user', 'text': '휴대폰에서 한글과 이미지를 함께 보낼 수 있게 해줘.', 'ts': time.time() - 20},
            {'role': 'tool', 'text': '검증용 파일 읽기\n선택한 텍스트와 펼친 기록은 상태 갱신 중에도 유지됩니다.', 'ts': time.time() - 15},
            {'role': 'assistant', 'text': '검증 화면입니다. 실제 모델 호출 없이 접수와 대기열을 확인할 수 있습니다.', 'ts': time.time() - 10},
        ] for row in rows}
        self.receipts = {}
        self.lock = threading.RLock()

    def finish(self, identifier, request_id, text):
        with self.lock:
            self.messages[identifier].append({'role': 'assistant', 'text': '검증 응답: ' + text, 'ts': time.time()})
            self.rows[identifier]['phase'] = 'idle'
            self.receipts[request_id]['delivery'] = 'completed'

    def rpc(self, host, command, payload, timeout=20):
        if command == 'attachment':
            return attachments.store(payload['cwd'], payload['name'], base64.b64decode(payload['data']), payload['sha256'])
        if command != 'chat':
            raise ValueError('검증 RPC 범위 밖입니다.')
        source = payload['source']
        row = self.rows[source['id']]
        action = payload['action']
        with self.lock:
            if action == 'read':
                supported = row['agent'] == 'codex'
                return {'phase': row['phase'], 'cwd': row['cwd'], 'messages': list(self.messages[row['id']]),
                        'capabilities': {'canSend': supported, 'reason': None if supported else '이 Claude 실행은 실제 터미널에서 입력해 주세요.'}}
            if action == 'receipt':
                return self.receipts.get(payload['requestId'], {'delivery': 'unknown'})
            if action != 'send':
                raise ValueError('검증 RPC 작업이 올바르지 않습니다.')
            rid = payload['requestId']
            if rid in self.receipts:
                return self.receipts[rid]
            if row['agent'] != 'codex':
                return {'delivery': 'failed', 'reason': '검증 Claude는 터미널만 지원합니다.'}
            text = '\n'.join(item['text'] for item in payload.get('input', []) if item['type'] == 'text')
            for aid in payload.get('attachmentIds', []):
                record = attachments.resolve(row['cwd'], aid)
                text += '\n[실제 첨부 경로] ' + record['path']
            queued = row['phase'] == 'working'
            self.messages[row['id']].append({'role': 'user', 'text': text, 'ts': time.time()})
            self.receipts[rid] = {'delivery': 'queued' if queued else 'accepted', 'confirmed': True, 'turnId': rid}
            row['phase'] = 'working'
            timer = threading.Timer(8 if queued else 5, self.finish, args=(row['id'], rid, text))
            timer.daemon = True
            timer.start()
            if '[unknown]' in text:
                return {'delivery': 'unknown', 'confirmed': False}
            return self.receipts[rid]


def main(argv=None):
    parser = argparse.ArgumentParser(description='실제 모델 없는 메시지·첨부 검증 보드')
    parser.add_argument('--port', type=int, default=8794)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--tailscale-user-file', type=Path)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('포트는 0~65535 범위여야 합니다.')
    directory = args.state_dir.expanduser().absolute() if args.state_dir else Path(tempfile.mkdtemp(prefix='sessionholic-chat-qa-'))
    if directory.is_symlink():
        parser.error('검증 상태 경로는 심볼릭 링크일 수 없습니다.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid():
        parser.error('검증 상태 폴더의 소유자가 다릅니다.')
    directory.chmod(0o700)
    token = args.token_file.expanduser().read_text().strip() if args.token_file else secrets.token_urlsafe(32)
    if not token:
        parser.error('접속 토큰이 비어 있습니다.')
    if not args.token_file:
        server.private_json(directory / 'access.json', {'token': token})
    tailscale_user = server.load_tailscale_user(args.tailscale_user_file) if args.tailscale_user_file else None
    now = int(time.time())
    rows = []
    for index, (agent, home, title, phase) in enumerate([
        ('codex', '.codex-isolated', '실제 세션 입력 · 한글과 첨부 검증', 'idle'),
        ('codex', '.codex-isolated', '진행 중인 작업 · 대기열 검증', 'working'),
        ('claude', '.claude', 'Claude · 터미널 입력과 첨부 경로 검증', 'needs_input'),
    ], 1):
        cwd = directory / ('workspace-' + str(index))
        cwd.mkdir(exist_ok=True, mode=0o700)
        rows.append({'id': f'019aaaaa-0000-7000-8000-{index:012d}', 'agent': agent, 'home': home,
                     'title': title, 'phase': phase, 'cwd': str(cwd), 'kind': 'interactive',
                     'account': '검증 계정', 'project': '메시지 검증', 'updatedAt': now - index,
                     'snippet': '모델·SSH 호출 없는 검증용 세션입니다.'})
    fake = FakeNative(rows)
    hosts = [{'name': 'local', 'label': '입력 검증용', 'local': True}]

    def runner(host, command, timeout):
        if command[0] == 'snapshot':
            return {'codex': [row for row in rows if row['agent'] == 'codex'],
                    'claude': [row for row in rows if row['agent'] == 'claude'], 'tmux': [], 'errors': []}
        return {'messages': fake.messages[command[2]]}

    board = server.Board(hosts, 60, runner, state_dir=directory)
    board.poll(hosts[0])
    manager = TerminalManager(state_dir=directory / 'terminals', socket_name='sb-browser-' + secrets.token_hex(6))
    flow = server.Workflow(board, manager, directory)
    def profiles():
        result = []
        for key, scope, scope_label, account in [
                ('.codex-isolated', 'environment', '격리 환경', '기본 Codex'),
                ('.codex-isolated-sample', 'environment', '격리 환경', 'sample'),
                ('.codex-sample', 'default', '기본 환경', 'sample')]:
            result.append({'id': 'codex:' + key, 'agent': 'codex', 'home': key,
                           'label': scope_label + ' · ' + account, 'available': True,
                           'scope': scope, 'environment': 'default', 'scopeLabel': scope_label, 'accountLabel': account,
                           'identityVerified': False})
        result.append({'id': 'claude:default', 'agent': 'claude', 'home': '.claude',
                       'label': '검증 Claude', 'available': True})
        return result
    flow.profiles = profiles
    flow.transfers.rpc = fake.rpc
    echo = "import sys\nprint('검증용 실제 PTY · 모델 호출 없음',flush=True)\nfor line in sys.stdin:\n print('받은 입력: '+line.rstrip(),flush=True)\n"
    launch.build_launch = lambda *a, **kw: {'argv': [sys.executable, '-u', '-c', echo], 'cwd': str(directory), 'env': {'PATH': os.environ['PATH'], 'LANG': 'en_US.UTF-8'}}
    httpd = server.make_server('127.0.0.1', args.port, board, token, terminals=manager, workflow=flow,
                              state_dir=directory, tailscale_user=tailscale_user)
    print(f'메시지 검증용 보드: http://127.0.0.1:{httpd.server_address[1]}/ · 상태 경로: {directory}', flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        manager.close()


if __name__ == '__main__':
    main()
