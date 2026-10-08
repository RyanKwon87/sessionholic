# 설치와 개인 운영

## 로컬 실행

README의 `init → doctor → serve` 순서로 실행합니다. 서버 기본 포트는 8790이고 다음과 같이 바꿀 수 있습니다.

```sh
python3 scripts/sessionholic.py serve --port 8791
```

`serve --help`로 서버 옵션을 확인할 수 있습니다. 설정 파일이 없으면 로컬 한 대로 시작하며, `--hosts`로 별도 기기 목록을 지정할 수 있습니다. 기존 SSH 접속이나 CLI 로그인 문제는 각 도구에서 먼저 해결하세요. 세션홀릭은 기존 인증을 교체하지 않습니다.

`doctor`는 설정 형식뿐 아니라 기본 설정·토큰·상태 경로의 종류와 소유자·권한도 확인합니다. 비밀값을 읽거나 파일·권한을 자동 변경하지 않습니다. 권한 오류가 있으면 표시된 대상만 확인하세요. 기본 권한은 설정·상태 폴더 700, 접속 토큰과 대화 캐시 파일 600입니다. 다른 프로그램의 인증 파일이나 홈 폴더 전체에 재귀적으로 권한을 적용하지 마세요. 빈 토큰 파일은 기존 토큰을 복구해야 하며, 토큰이 아예 없는 첫 실행에서는 서버가 새로 만듭니다.

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

경로나 포트를 바꿔 설치 파일을 다시 만들 때는 서비스를 중지한 뒤 기존 파일을 백업 이름으로 옮깁니다. 백업 파일이 이미 있다면 덮어쓰지 말고 다른 이름을 사용하세요.

```sh
launchctl bootout gui/$(id -u)/io.github.sessionholic
mv -i ~/Library/LaunchAgents/io.github.sessionholic.plist ~/Library/LaunchAgents/io.github.sessionholic.plist.backup
python3 scripts/sessionholic.py doctor
python3 scripts/sessionholic.py service-file
```

이전 설치에서 `--port` 또는 `--tailscale-user-file`을 사용했다면 필요한 옵션을 다시 지정하고, 새로 출력된 시작 명령을 실행하세요. 재생성 실패 시 백업 파일을 원래 이름으로 복구할 수 있습니다.

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


## 인계 실패 뒤 임시 파일 정리

이 기능을 처음 적용하는 업데이트에서는 진행 중인 인계가 끝난 뒤 보드를 재시작하세요. 이전 버전의 프로세스와 새 정리 명령을 함께 실행하지 마세요.

인계가 끝나거나 실패하면 해당 인계의 임시 압축 파일만 정리합니다. 가져온 작업 폴더와 native 실행 기록은 보존합니다. 연결 오류 등으로 정리가 남은 경우, 로컬 상태 폴더의 `transfers/<TRANSFER_ID>/transfer.json`에 `archiveCleanupPending`과 대상 기기가 기록됩니다.

```sh
python3 scripts/sessionholic.py cleanup-transfer <TRANSFER_ID>
```

사용자 지정 설치는 `--hosts /path/to/hosts.json --state-dir /path/to/state`를 함께 지정합니다. 이 명령은 등록된 기기에 접속해 **지정한 인계 한 건**만 정리하고, 남은 기기를 출력합니다. 아직 진행 중인 인계는 정리하지 않습니다. 대상 준비 결과가 미확인이면 대상 worker가 준비 완료를 확인하기 전까지 수신 파일을 남기며, 사용 중인 파일의 정리도 보류합니다. 대상 목록과 실행 기록을 먼저 확인하세요.
