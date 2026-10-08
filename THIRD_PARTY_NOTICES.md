# 외부 구성요소 고지

세션홀릭의 자체 코드와 아래 외부 라이브러리는 구분됩니다. 외부 구성요소는 원래 저작권 고지와 라이선스를 유지하며, 세션홀릭 자체 코드의 배포 조건이 이 구성요소의 원래 허용 범위를 바꾸지 않습니다.

| 구성요소 | 포함 버전 | 원본 라이선스 | 고지 파일 |
| --- | --- | --- | --- |
| `@xterm/xterm` | 6.0.0 | MIT | [xterm-LICENSE](web/vendor/xterm-LICENSE) |
| `@xterm/addon-fit` | 0.11.0 | MIT | [addon-fit-LICENSE](web/vendor/addon-fit-LICENSE) |

배포물 출처와 npm 무결성 정보는 [versions.json](web/vendor/versions.json)에 기록되어 있습니다. vendored JavaScript·CSS를 바꿀 때는 원본 고지와 출처도 함께 확인해야 합니다.

Python, tmux, Git, OpenSSH, Codex, Claude Code, Tailscale은 사용자의 기기에 별도로 설치하는 도구입니다. 이 저장소에 해당 실행 파일이나 native 로그인 자격증명을 포함하지 않습니다.
