# 세션홀릭 - 에이전트 세션 매니저

[![Test](https://github.com/RyanKwon87/sessionholic/actions/workflows/test.yml/badge.svg)](https://github.com/RyanKwon87/sessionholic/actions/workflows/test.yml)

**Sessionholic — 내 AI 작업, 어디서든 이어서.**

여러 컴퓨터의 Codex·Claude Code 작업을 모아 보고, 대화를 확인하고, 실제 터미널에 연결하는 개인용 세션 매니저입니다. 작업 파일과 최근 대화를 다른 컴퓨터로 인계할 수 있습니다. 각자 자신의 컴퓨터에서 실행하는 self-hosted 앱이며, 첫 공개판은 macOS를 대상으로 합니다.

<img src="docs/screenshots/mobile.jpg" width="320" alt="합성 세션으로 검수한 세션홀릭 모바일 대화 화면" />

위 화면은 실제 사용자 대화가 없는 합성 검수 화면입니다.

## 할 수 있는 일

- 기기·프로젝트·실행 환경별 작업 목록과 최근 대화 확인
- 실제 native CLI 터미널 연결, 여러 터미널 전환과 재연결
- 지원되는 Codex 세션에 메시지·이미지·파일 전송
- 작업 파일·Git 변경·최근 대화를 다른 기기의 새 작업 폴더와 세션으로 인계
- 모바일 입력창 접기·펼치기, 한글 입력, 파일 선택, 브라우저가 허용하는 클립보드 붙여넣기
- 자기 Tailscale 네트워크에서 HTTPS로 접속

기기 이전은 실행 중인 프로세스의 메모리를 옮기는 기능이 아닙니다. 현재 응답 중단을 확인한 뒤 작업 파일과 대화 맥락을 전달합니다. 승인 대기 상태와 모든 대화 이력을 그대로 복제하지 않습니다. **Claude 직접 메시지는 지원하지 않으며** 터미널과 파일 업로드를 사용합니다. 상세 내용은 [지원 범위](docs/limitations.md)를 확인하세요.

## 처음 실행하기

필요한 도구는 Python 3.9 이상, Git, SSH, tmux와 설치·로그인된 Codex 또는 Claude Code입니다. Python 외부 패키지는 필요하지 않습니다. Node.js는 프런트엔드 테스트에만 사용합니다.

macOS에서 tmux가 없다면 Homebrew로 설치할 수 있습니다.

```sh
brew install tmux
```

소스를 받은 뒤 저장소 루트에서 실행합니다.

```sh
git clone https://github.com/RyanKwon87/sessionholic.git
cd sessionholic
python3 scripts/sessionholic.py init
python3 scripts/sessionholic.py doctor
python3 scripts/sessionholic.py serve
```

브라우저에서 `http://127.0.0.1:8790/`를 엽니다. 최초 서버 실행 시 `~/.config/sessionholic/token` 파일에 생성된 접속 토큰을 로그인 화면에 입력하세요. 토큰은 본인 컴퓨터에서만 확인하고 공유하지 마세요.

`init`은 기존 설정을 덮어쓰지 않으며 다른 도구의 인증·Git·SSH 설정을 변경하지 않습니다. `doctor`는 로컬 도구와 설정 형식만 검사합니다. SSH 접속, 모델 호출, 로그인 시도를 하지 않습니다. 기본 설정은 이 컴퓨터 한 대입니다.

처음에 목록이 비어 있다면 같은 OS 사용자로 Codex 또는 Claude Code에서 작업을 시작한 뒤 새로고침하세요. 터미널 연결과 직접 메시지 지원은 CLI의 종류·버전·세션 상태에 따라 달라집니다.

## 내 기기와 실행 환경 등록

| 파일 | 내용 |
| --- | --- |
| `~/.config/sessionholic/hosts.json` | 이 컴퓨터와 원격 SSH 기기 |
| `~/.config/sessionholic/config.json` | 프로필 라벨·프로젝트·선택적 실행 환경 |
| `~/.config/sessionholic/token` | 보드 접속 토큰 |
| `~/.local/state/sessionholic/` | 목록·대화 캐시, 터미널·요청·인계 상태 |

[기기 예시](examples/hosts.json)와 [환경 예시](examples/config.json)를 참고해 본인 설정을 편집하세요. 예시의 SSH 별칭·폴더·프로필은 사용자가 이미 설정한 실제 값으로 바꿔야 합니다. 프로필을 적는 것만으로 native CLI 로그인이나 계정이 만들어지지는 않습니다. 계정 표시 이름은 확인된 로그인 신원을 뜻하지 않습니다.

원격 컴퓨터에도 Python·Git·tmux와 해당 CLI가 필요하며, 사용자가 먼저 SSH 연결과 native 로그인을 구성해야 합니다. 각 컴퓨터는 자기 홈의 설정과 인증을 사용합니다. 인증 파일을 기기 간 자동 복사하지 않습니다. 사용자 설정은 저장소 밖에 보관합니다.

환경 그룹을 지정했다면 원본 작업과 대상 프로필의 그룹이 일치해야 새 인계·기기 이전이 가능합니다. 실제로 같은 기존 세션에 다시 연결하는 동작은 유지합니다. 설정 스키마와 구성은 [아키텍처](docs/architecture.md)에 설명합니다.

## 휴대폰에서 접속하기

서버는 loopback에만 바인딩합니다. 휴대폰 접속은 본인의 Tailscale 네트워크에서 HTTPS Serve를 사용합니다. 기기별 설정은 [설치·운영 안내](docs/setup.md)를 참고하세요. 일반 인터넷 공개나 여러 사람의 공동 서버 운영은 이 버전의 지원 범위가 아닙니다.

브라우저를 닫아도 보드 전용 tmux 안의 실행은 유지됩니다. Codex 연결 닫기와 보드가 시작한 독립 CLI 실행 종료는 다릅니다. 닫기 전에 화면의 안내를 확인하세요. 미전송 초안은 현재 탭 메모리에만 보관하므로 탭을 닫거나 새로고침하면 사라질 수 있습니다.

## 개발과 검증

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_frontend.cjs
python3 scripts/check_public.py
```

테스트는 임시 홈·작업 폴더와 합성 세션을 사용합니다. 실제 모델 호출·개인 인증·실제 사용자의 응답 중단이 없는 테스트와 실제 기기 검증을 구분합니다. [검증 기록](docs/verification.md), [기여 안내](CONTRIBUTING.md), [보안 경계](SECURITY.md), [지원 범위](docs/limitations.md)를 확인하세요.

## 이용 조건

세션홀릭 자체 코드는 **Sessionholic Source Available License 1.0**으로 제공합니다.

- 개인 사용, 회사 내부 업무와 고객 업무 수행에 사용·수정 가능
- 라이선스·저작권 고지를 유지하고 같은 조건으로 무료 재배포 가능
- 세션홀릭 자체 또는 수정판의 재판매·유료 호스팅/관리 서비스 제공 금지. 해당 사용에는 별도 서면 허가 필요
- 세션홀릭을 내부 도구로 사용해 만든 일반 업무 산출물은 판매 가능

이는 상업적 재배포를 제한하는 **소스 공개(source-available)** 프로젝트이며 OSI 오픈소스 라이선스로 표시하지 않습니다. 별도 작성한 이용 조건의 원문은 [LICENSE](LICENSE)이며, 위 설명은 원문을 대체하지 않습니다. 포함된 xterm 등의 외부 구성요소에는 [원래 라이선스](THIRD_PARTY_NOTICES.md)가 계속 적용됩니다.
