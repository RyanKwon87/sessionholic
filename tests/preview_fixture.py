"""Isolated browser QA. Synthetic tasks and a real echo PTY; no model, account or SSH calls."""
import argparse
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


def main(argv=None):
    parser = argparse.ArgumentParser(description='모델 호출 없는 웹터미널 입력 검증용 보드')
    parser.add_argument('--port', type=int, default=8793, help='loopback HTTP 포트 (기본: 8793)')
    parser.add_argument('--token-file', type=Path, help='기존 접속 토큰 파일; 토큰은 출력하거나 복사하지 않음')
    parser.add_argument('--state-dir', type=Path, help='검증 상태 경로 (기본: 임시 디렉터리)')
    parser.add_argument('--tailscale-user-file', type=Path, help='HTTPS Serve 자동 인증을 허용할 비공개 사용자 파일')
    parser.add_argument('--socket-name', help='기존 검증용 tmux socket 재사용 (sb-browser- 접두사)')
    arguments = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(arguments)
    if not 0 <= args.port <= 65535:
        parser.error('포트는 0~65535 범위여야 합니다.')
    if args.socket_name and not args.socket_name.startswith('sb-browser-'):
        parser.error('검증용 socket은 sb-browser-로 시작해야 합니다.')
    try:
        args.tailscale_user = server.load_tailscale_user(args.tailscale_user_file) if args.tailscale_user_file else None
    except ValueError as exc:
        parser.error(str(exc))
    directory = args.state_dir.expanduser().absolute() if args.state_dir else Path(tempfile.mkdtemp(prefix='sessionholic-browser-'))
    if args.token_file:
        try:
            token = args.token_file.expanduser().read_text(encoding='utf-8').strip()
        except (OSError, UnicodeError):
            parser.error('기존 접속 토큰 파일을 읽지 못했습니다.')
        if not token:
            parser.error('기존 접속 토큰 파일이 비어 있습니다.')
    else:
        token = secrets.token_urlsafe(32)
    if directory.is_symlink():
        parser.error('검증 상태 경로는 심볼릭 링크일 수 없습니다.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid():
        parser.error('검증 상태 경로의 소유자가 현재 사용자와 다릅니다.')
    directory.chmod(0o700)
    manager = TerminalManager(state_dir=directory / 'terminals', socket_name=args.socket_name or 'sb-browser-' + secrets.token_hex(6))
    try:
        return serve(args, directory, token, manager, publish_path=not arguments)
    finally:
        manager.close()


def serve(args, directory, token, manager, publish_path=False):
    if not args.token_file:
        server.private_json(directory / 'access.json', {'token': token})
    if publish_path:
        Path('/tmp/sessionholic-preview-path').write_text(str(directory))
    hosts = [{'name': 'local', 'label': '입력 검증용', 'local': True},
             {'name': 'remote', 'label': '원격 기기 · 검증용', 'local': False, 'ssh': 'not-used'}]
    now = int(time.time())
    source = {'id': '019aaaaa-0000-7000-8000-000000000001', 'agent': 'codex', 'home': '.codex',
              'phase': 'idle', 'cwd': str(directory), 'title': '웹터미널 한글 입력과 재연결 검증',
              'project': '세션 보드', 'account': '검증 계정 A', 'updatedAt': now, 'startedAt': now - 1000,
              'snippet': '모델 호출 없이 실제 터미널 연결을 검증하고 있습니다.'}
    def runner(host, args, timeout):
        if host['name'] == 'remote':
            raise RuntimeError('검증용 오프라인 상태 · 실제 미니에는 연결하지 않았습니다')
        if args[0] == 'snapshot':
            return {'claude': [], 'codex': [source,
                {**source, 'id': '019aaaaa-0000-7000-8000-000000000002', 'title': '문서 정리 · 승인 대기 예시', 'phase': 'needs_input', 'updatedAt': now - 120}],
                'tmux': [], 'errors': []}
        return {'messages': [
            {'role': 'user', 'text': '휴대폰에서도 최근 맥락을 보고 하던 일을 이어가고 싶어.', 'ts': now - 700},
            {'role': 'assistant', 'text': '작업을 고르면 최근 대화가 보이고, 에이전트와 계정을 선택해 실제 터미널을 열 수 있습니다. 이 화면은 검증용 예시입니다.', 'ts': now - 680},
            {'role': 'user', 'text': '한글 입력과 줄바꿈, 화면을 닫았다 돌아왔을 때도 확인해줘.', 'ts': now - 50},
            {'role': 'assistant', 'text': '아래에서 터미널을 열어보세요. 입력한 내용을 되돌려주는 실제 프로세스로 검증하며, 에이전트나 외부 서비스는 호출하지 않습니다.', 'ts': now - 20}]}
    profiles = [{'id': 'codex:.codex', 'agent': 'codex', 'home': '.codex', 'label': '검증 계정 A', 'available': True},
                {'id': 'codex:.codex-alt', 'agent': 'codex', 'home': '.codex-alt', 'label': '검증 계정 B', 'available': True},
                {'id': 'claude:default', 'agent': 'claude', 'home': '.claude', 'label': '검증 Claude', 'available': True}]
    code = "import sys\nprint('검증용 실제 PTY · 모델 호출 없음', flush=True)\nprint('한글을 입력해 보세요. exit를 입력하면 종료합니다.', flush=True)\nfor line in sys.stdin:\n if line.strip()=='exit': break\n print('받은 입력: '+line.rstrip(), flush=True)\n"
    launch.build_launch = lambda *a, **kw: {'argv': [sys.executable, '-u', '-c', code], 'cwd': str(directory), 'env': {'PATH': os.environ['PATH'], 'LANG': 'en_US.UTF-8'}}
    board = server.Board(hosts, 60, runner, state_dir=directory)
    for host in hosts:
        board.poll(host)
    flow = server.Workflow(board, manager, directory)
    flow.profiles = lambda: profiles
    httpd = server.make_server('127.0.0.1', args.port, board, token, terminals=manager, workflow=flow,
                              state_dir=directory, tailscale_user=args.tailscale_user)
    print(f'검증용 보드: http://127.0.0.1:{httpd.server_address[1]}/ (실제 모델·원격 연결 없음)', flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
