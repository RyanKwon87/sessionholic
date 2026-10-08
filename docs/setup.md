# 설치와 개인 운영

## 로컬 실행

README의 `init → doctor → serve` 순서로 실행합니다. 서버 기본 포트는 8790이고 다음과 같이 바꿀 수 있습니다.

```sh
python3 scripts/sessionholic.py serve --port 8791
```

`serve --help`로 서버 옵션을 확인할 수 있습니다. 설정 파일이 없으면 로컬 한 대로 시작하며, `--hosts`로 별도 기기 목록을 지정할 수 있습니다. 기존 SSH 접속이나 CLI 로그인 문제는 각 도구에서 먼저 해결하세요. 세션홀릭은 기존 인증을 교체하지 않습니다.

## 원격 기기

1. 원격 기기에 Python 3.9 이상, Git, tmux, 사용할 native CLI를 설치합니다.
2. 같은 원격 OS 사용자로 native CLI에 로그인하고 작업을 시작합니다.
3. 보드 서버 기기의 SSH 설정에 연결 별칭을 등록하고 비대화식 연결을 직접 확인합니다.
4. `hosts.json`에 SSH 별칭과 원격 Python 경로를 추가합니다. 설정 예시는 `examples/hosts.json`입니다.
5. 원격 계정 라벨·프로젝트·환경 그룹이 필요하면 원격 기기 자신의 `~/.config/sessionholic/config.json`에 등록합니다.

환경 그룹은 두 기기에서 같은 의미의 이름을 사용해야 합니다. 원격 설정·로그인·인증 파일은 자동 생성하거나 동기화하지 않습니다. 전송 기능은 필요한 실행 도우미만 원격 사용자 상태 폴더에 저장합니다.

## macOS 백그라운드 실행

포그라운드 서버를 종료한 뒤 다음 명령으로 LaunchAgent 파일을 생성합니다.

```sh
python3 scripts/sessionholic.py service-file
```

이 명령은 서비스를 자동 시작하지 않습니다. 출력된 `launchctl bootstrap` 명령을 실행하면 로그인 시 시작하는 사용자 서비스가 등록됩니다. 같은 포트의 포그라운드 서버와 동시에 실행하지 마세요. 파일에는 현재 저장소와 Python의 절대 경로가 들어가므로, 두 경로를 옮기면 서비스를 중지하고 설치 파일을 다시 만들어야 합니다. 기존 plist를 자동 덮어쓰지 않습니다.

서비스 중지:

```sh
launchctl bootout gui/$(id -u)/io.github.sessionholic
```

기록은 `~/.local/state/sessionholic/service.stdout.log`와 `service.stderr.log`에 저장합니다. 문제를 제보할 때 원본 기록이나 대화를 그대로 올리지 말고 필요한 오류만 익명화하세요. 서버 재시작은 보드의 tmux 접속 클라이언트를 분리하며, 유지 중인 실행을 종료하는 명령을 보내지 않습니다.

## Tailscale로 모바일 접속

Tailscale의 설치·로그인·접근 정책은 사용자가 관리합니다. 보드의 loopback HTTP 포트를 본인 tailnet의 HTTPS Serve로 연결하세요. 다른 서비스가 이미 사용 중인 Serve 설정을 덮어쓰지 않도록 현재 구성을 먼저 확인하세요.

기본 모드는 HTTPS에서도 보드 토큰으로 로그인합니다. 개인 신원 기반 자동 로그인을 사용하려면 허용할 본인 Tailscale 로그인 식별자 한 줄을 권한 600의 비공개 파일에 저장하고 서버에 `--tailscale-user-file`로 지정합니다. 파일 내용이나 토큰을 URL에 넣지 마세요. LaunchAgent에도 같은 옵션을 추가할 수 있습니다.

```sh
python3 scripts/sessionholic.py service-file --tailscale-user-file ~/.config/sessionholic/tailscale-user
```

이 옵션은 Tailscale Serve가 전달하는 HTTPS 신원 헤더와 허용 사용자를 검사합니다. 직접 인터넷 공개나 임의 reverse proxy에서 인증 우회 옵션으로 사용하지 마세요. 자세한 신뢰 경계는 `SECURITY.md`에 있습니다.

## 업데이트와 복구

처음 공개판은 자동 업데이트를 제공하지 않습니다. 설치한 버전과 사용자 설정·상태 폴더를 보존하고, 새 버전의 테스트와 변경 내용을 확인한 뒤 서버만 중지해 소스를 교체합니다. 업데이트 중 기존 설정을 예시 파일로 덮어쓰거나 native 인증을 복사하지 마세요. 실패하면 이전 소스로 복구해 같은 사용자 설정과 상태 경로로 시작합니다.

기존 다른 제품 이름으로 운영하던 개발판의 상태·프로필은 자동 이전하지 않습니다. 세션홀릭은 독립된 설정·상태·tmux·브라우저 저장 영역을 사용합니다. 실제 데이터 이전이 필요하면 대상과 범위를 확인해 별도로 진행하세요.
