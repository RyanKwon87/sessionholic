"""Transfer UI QA: two synthetic online hosts and an isolated echo PTY.

No model, native interruption, SSH, credential discovery, or file transfer runs.
The real server.Workflow builds transfer plans against an in-memory fake adapter.
"""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launch
import server
from terminal import TerminalManager


PROFILES = [
    {'id': 'codex:.codex', 'agent': 'codex', 'home': '.codex',
     'label': '검증 Codex', 'available': True},
    {'id': 'claude:default', 'agent': 'claude', 'home': '.claude',
     'label': '검증 Claude', 'available': True},
]
TITLE = 'Local ↔ Remote 작업 넘기기 · 모델 없는 검증'
ECHO_CODE = (
    "import sys\n"
    "print('TRANSFER_QA_READY · 가상 전송 완료 / 실제 echo PTY', flush=True)\n"
    "print('입력은 echo로 돌아옵니다. 모델·SSH·실제 중단은 실행하지 않습니다.', flush=True)\n"
    "for line in sys.stdin:\n"
    " if line.strip() == 'exit': break\n"
    " print('받은 입력: ' + line.rstrip(), flush=True)\n"
)


def echo_spec(directory):
    return {'argv': [sys.executable, '-u', '-c', ECHO_CODE], 'cwd': str(directory),
            'env': {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'en_US.UTF-8'}}


class FakeTransfers:
    def __init__(self, directory):
        self.directory = directory
        self.started = {}

    def profiles(self, host, refresh=False):
        return [dict(profile) for profile in PROFILES]

    def preview(self, source, target, source_host, target_host):
        return {
            'sourceHostLabel': source_host['label'], 'targetHostLabel': target_host['label'],
            'workspace': {
                'root': '/tmp/qa-source', 'projectName': 'qa-source', 'cwdRelative': '.',
                'git': True, 'head': '1' * 40, 'branch': 'qa', 'fileCount': 3, 'bytes': 100,
                'excluded': ['node_modules/', '.env'], 'warnings': [], 'fingerprint': 'a' * 64, 'sourceEnvironment': 'default',
            },
            'requiresInterrupt': True,
            'destinationNote': '검증용 가상 대상 경로 /tmp/qa-target · 실제 파일 전송은 없습니다.',
        }

    def execute(self, source, target, source_host, target_host, profile, request_id, read_messages):
        # Only the synthetic Board runner supplies these messages. Never call
        # a transfer worker, native interrupt API, network, or destination path.
        read_messages()
        transfer_id = hashlib.sha256(request_id.encode('utf-8')).hexdigest()[:32]
        return {**echo_spec(self.directory), 'transferId': transfer_id,
                'destinationCwd': '/tmp/qa-target', 'warnings': []}

    def mark_started(self, transfer_id, terminal_id):
        self.started[transfer_id] = terminal_id


def serve(args, directory, token, manager):
    if not args.token_file:
        server.private_json(directory / 'access.json', {'token': token})
    hosts = [
        {'name': 'local', 'label': 'Local · 전송 검증', 'local': True},
        {'name': 'remote', 'label': 'Remote · 전송 검증', 'local': False, 'ssh': 'not-used'},
    ]
    now = int(time.time())
    source = {
        'id': '019aaaaa-0000-7000-8000-000000000001', 'agent': 'codex', 'home': '.codex',
        'phase': 'working', 'cwd': '/tmp/qa-source', 'title': TITLE,
        'project': '전송 UI 검증', 'account': '검증 Codex', 'updatedAt': now,
        'startedAt': now - 1000, 'snippet': '코드와 미커밋 변경을 상대 기기로 넘기는 가상 작업입니다.',
    }
    def runner(host, arguments, timeout):
        if arguments[0] == 'snapshot':
            return {'claude': [], 'codex': [dict(source)], 'tmux': [], 'errors': []}
        return {'messages': [
            {'role': 'user', 'text': '하던 작업을 상대 기기로 넘겨 계속하고 싶어.', 'ts': now - 60},
            {'role': 'assistant', 'text': '작업 중인 응답을 중단하고 코드·변경 파일·대화를 넘기는 화면을 확인합니다. 이 예시는 실제 중단이나 전송을 실행하지 않습니다.', 'ts': now - 30},
        ]}
    board = server.Board(hosts, 60, runner, state_dir=directory)
    for host in hosts:
        board.poll(host)
    flow = server.Workflow(board, manager, directory)
    flow.profiles = lambda: [dict(profile) for profile in PROFILES]
    flow.transfers = FakeTransfers(directory)
    # Every alternative launch selection is also confined to this echo fixture.
    launch.build_launch = lambda *a, **kw: echo_spec(directory)
    flow.remote_attach = lambda *a, **kw: echo_spec(directory)
    httpd = server.make_server('127.0.0.1', args.port, board, token, terminals=manager,
                              workflow=flow, state_dir=directory)
    print(f'전송 검증 보드: http://127.0.0.1:{httpd.server_address[1]}/ (모델·SSH·실제 중단 없음)', flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='두 가상 온라인 기기의 작업 중단·전송 UI와 실제 echo PTY 검증')
    parser.add_argument('--port', type=int, default=8796, help='loopback HTTP 포트 (기본: 8796)')
    parser.add_argument('--state-dir', type=Path, help='비공개 검증 상태 경로 (기본: 임시 디렉터리)')
    parser.add_argument('--socket-name', default='sb-browser-transfer-qa', help='sb-browser- 접두사의 격리 tmux socket')
    parser.add_argument('--token-file', type=Path, help='선택한 기존 검증 접속 토큰 파일; 내용은 출력하지 않음')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('포트는 0~65535 범위여야 합니다.')
    if not args.socket_name.startswith('sb-browser-'):
        parser.error('검증용 socket은 sb-browser-로 시작해야 합니다.')
    directory = args.state_dir.expanduser().absolute() if args.state_dir else Path(tempfile.mkdtemp(prefix='sessionholic-transfer-browser-'))
    if directory.is_symlink():
        parser.error('검증 상태 경로는 심볼릭 링크일 수 없습니다.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid():
        parser.error('검증 상태 경로의 소유자가 현재 사용자와 다릅니다.')
    directory.chmod(0o700)
    if args.token_file:
        try:
            token = args.token_file.expanduser().read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            parser.error('선택한 검증 접속 토큰 파일을 읽지 못했습니다.')
        if not token:
            parser.error('검증 접속 토큰 파일이 비어 있습니다.')
    else:
        token = secrets.token_urlsafe(32)
    manager = TerminalManager(state_dir=directory / 'terminals', socket_name=args.socket_name)
    try:
        return serve(args, directory, token, manager)
    finally:
        manager.close()


if __name__ == '__main__':
    main()
