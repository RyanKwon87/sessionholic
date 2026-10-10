"""Loopback browser QA: native chat RPC is synthetic; attachments use the real store.

No credentials, model process, native daemon or SSH is opened. The terminal is
an ordinary local echo PTY, or with --menu-demo a small numbered menu that reads
arrow keys and Enter like the agent TUIs' approval menus. Never use this fixture
as a production server.
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

# Drawn like an approval menu ("❯ 1. Yes"), answered with ESC [ A / ESC [ B and Enter.
# After a choice it shows the result and an input line, then asks the next question.
MENU_DEMO = r'''
import os, sys, termios, time, tty
fd = sys.stdin.fileno()
saved = termios.tcgetattr(fd)
options = ['Yes', 'No']

def draw(round_number, current):
    rows = ['\x1b[2J\x1b[H', ' 검증 질문 #%d' % round_number, '', ' 계속 진행할까요?', '']
    for index, label in enumerate(options):
        rows.append((' ❯ ' if index == current else '   ') + '%d. %s' % (index + 1, label))
    rows += ['', ' ↑/↓ 이동 · Enter 선택']
    sys.stdout.write('\n'.join(rows))
    sys.stdout.flush()

try:
    tty.setcbreak(fd)
    round_number, current, pending = 1, 0, b''
    draw(round_number, current)
    while True:
        data = os.read(fd, 64)
        if not data:
            break
        pending += data
        while pending:
            if pending[:3] in (b'\x1b[A', b'\x1bOA'):
                current, pending = max(current - 1, 0), pending[3:]
            elif pending[:3] in (b'\x1b[B', b'\x1bOB'):
                current, pending = min(current + 1, len(options) - 1), pending[3:]
            elif pending[:1] in (b'\r', b'\n'):
                pending = b''
                sys.stdout.write('\x1b[2J\x1b[H 선택 결과: %d. %s (질문 #%d)\n\n> '
                                 % (current + 1, options[current], round_number))
                sys.stdout.flush()
                time.sleep(4)
                termios.tcflush(fd, termios.TCIFLUSH)
                round_number, current = round_number + 1, 0
                break
            elif pending[:1] == b'\x1b' and len(pending) < 3:
                break  # wait for the rest of the arrow sequence
            else:
                pending = pending[1:]
        draw(round_number, current)
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
'''


class FakeNative:
    def __init__(self, rows):
        self.rows = {row['id']: row for row in rows}
        self.messages = {row['id']: [
            {'role': 'user', 'text': '휴대폰에서 한글과 이미지를 함께 보낼 수 있게 해줘.', 'ts': time.time() - 20,
             'attachments': [{'id': None, 'name': '이미지 첨부', 'type': 'image/*', 'size': None,
                              'kind': 'image', 'source': 'native'}]},
            {'role': 'tool', 'text': '검증용 파일 읽기\n선택한 텍스트와 펼친 기록은 상태 갱신 중에도 유지됩니다.', 'ts': time.time() - 15},
            {'role': 'assistant', 'text': (
                '**검증 화면**입니다. 실제 모델 호출 없이 메시지 접수 상태를 확인할 수 있습니다.\n\n'
                '아래 문구를 복사해 보세요.\n\n'
                '> 미리보기 화면의 안내 문구를 쉽게 바꿔주세요.\n>\n'
                '> 제목은 **작업 미리보기**로 바꾸고, 휴대폰에서도 잘 읽히게 해주세요.\n\n'
                '코드의 기호와 들여쓰기는 그대로 복사됩니다.\n\n'
                '```python\nif a > b:\n    print("값 확인")\n```\n\n'
                '- 입력창은 줄바꿈에 맞춰 늘어납니다.\n'
                '- [프로젝트 문서](https://example.com/docs)를 참고하세요.'
            ), 'ts': time.time() - 10},
        ] for row in rows}
        self.receipts = {}
        self.pending = {row['id']: 0 for row in rows}
        self.running = {row['id']: 'running-' + row['id'] for row in rows if row['phase'] == 'working'}
        self.lock = threading.RLock()

    def finish(self, identifier, request_id, text):
        with self.lock:
            self.messages[identifier].append({'role': 'assistant', 'text': '검증 응답: ' + text, 'ts': time.time()})
            self.pending[identifier] -= 1
            if self.pending[identifier]:
                return
            # The running turn ends only after every input it absorbed was answered.
            turn_id = self.running.pop(identifier, None)
            self.rows[identifier]['phase'] = 'idle'
            for message in self.messages[identifier]:
                if message.get('turnId') == turn_id:
                    message['turnStatus'] = 'completed'
            for receipt in self.receipts.values():
                if receipt.get('turnId') == turn_id:
                    receipt['delivery'] = 'completed'

    def rpc(self, host, command, payload, timeout=20):
        if command == 'attachment':
            return attachments.store(payload['cwd'], payload['name'], base64.b64decode(payload['data']), payload['sha256'])
        if command != 'chat':
            raise ValueError('검증 RPC 범위 밖입니다.')
        source = payload['source']
        row = self.rows[source['id']]
        action = payload['action']
        with self.lock:
            if action == 'capabilities':
                return {'phase': row['phase'], 'cwd': row['cwd'], 'kind': row.get('kind')}
            if action == 'read':
                supported = row['agent'] == 'codex'
                ids = list(dict.fromkeys(([payload['requestId']] if payload.get('requestId') else []) + payload.get('requestIds', [])))
                return {'phase': row['phase'], 'cwd': row['cwd'], 'messages': list(self.messages[row['id']]),
                        'receipts': [{**self.receipts.get(identifier, {'delivery': 'unknown'}), 'requestId': identifier} for identifier in ids],
                        'capabilities': {'canSend': supported, 'reason': None if supported else 'Claude 입력은 보드가 연결한 터미널로 전달합니다.'}}
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
            sent_files = []
            for aid in payload.get('attachmentIds', []):
                record = attachments.resolve(row['cwd'], aid)
                kind = 'image' if record['mime'] in ('image/png', 'image/jpeg', 'image/gif', 'image/webp') else 'file'
                sent_files.append({'id': record['id'], 'name': record['name'], 'type': record['mime'],
                                   'size': record['size'], 'kind': kind, 'source': 'sessionholic'})
                if kind == 'file':
                    text += '\n첨부 파일: ' + record['path']
            # Like native turn/start: a running turn absorbs the input, otherwise a new turn starts.
            joined = row['phase'] == 'working'
            turn_id = self.running.setdefault(row['id'], 'running-' + row['id']) if joined else rid
            self.running[row['id']] = turn_id
            self.messages[row['id']].append({'role': 'user', 'text': text, 'ts': time.time(), 'clientId': rid,
                                             'turnId': turn_id, 'turnStatus': 'inProgress', 'attachments': sent_files})
            self.receipts[rid] = {'delivery': 'accepted', 'confirmed': True, 'turnId': turn_id,
                                  'readbackConfirmed': True, 'currentSnapshotConfirmed': True,
                                  'acknowledged': True, 'attachments': sent_files}
            row['phase'] = 'working'
            self.pending[row['id']] += 1
            timer = threading.Timer(8 if joined else 5, self.finish, args=(row['id'], rid, text))
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
    parser.add_argument('--menu-demo', action='store_true',
                        help='보드 터미널을 방향키·Enter로 답하는 번호 메뉴로 실행하고 Claude 백그라운드 세션을 추가')
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
    sessions = [
        ('codex', '.codex-isolated', '실제 세션 입력 · 한글과 첨부 검증', 'idle', 'interactive'),
        ('codex', '.codex-isolated', '진행 중인 작업 · 실행 중 입력 검증', 'working', 'interactive'),
        ('claude', '.claude', 'Claude · 터미널 입력과 첨부 경로 검증', 'needs_input', 'interactive'),
    ]
    if args.menu_demo:
        # No board terminal until "질문 불러오기" attaches it (promptOpenable).
        sessions.append(('claude', '.claude', 'Claude 백그라운드 · 질문 응답 검증', 'needs_input', 'background'))
    for index, (agent, home, title, phase, kind) in enumerate(sessions, 1):
        cwd = directory / ('workspace-' + str(index))
        cwd.mkdir(exist_ok=True, mode=0o700)
        rows.append({'id': f'019aaaaa-0000-7000-8000-{index:012d}', 'agent': agent, 'home': home,
                     'title': title, 'phase': phase, 'cwd': str(cwd), 'kind': kind,
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
    program = MENU_DEMO if args.menu_demo else echo
    launch.build_launch = lambda *a, **kw: {'argv': [sys.executable, '-u', '-c', program], 'cwd': str(directory), 'env': {'PATH': os.environ['PATH'], 'LANG': 'en_US.UTF-8'}}
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
