"""Synthetic goal/session UI preview. No native CLI, SSH or model calls.

Use a separate SESSIONHOLIC_INSTANCE and --state-dir, e.g. port 8796.
The reused chat fixture provides an inert runner and a synthetic chat transport.
"""
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_preview_fixture as fixture


def main():
    make_server = fixture.server.make_server

    def seeded(*args, **kwargs):
        httpd = make_server(*args, **kwargs)

        def with_session_metadata(data):
            # Board.poll makes its own scrubbed copy. Decorate every synthetic
            # snapshot so a refresh cannot replace the initial metadata seed.
            data = {**data, **{agent: [dict(row) for row in data.get(agent, [])]
                              for agent in ("codex", "claude")}}
            rows = data["codex"] + data["claude"]
            for index, row in enumerate(rows):
                initiator = ["human", "agent", "unknown"][index % 3]
                row.update(initiator=initiator, initiatorName=None,
                           initiatorEvidence="explicit" if initiator != "unknown" else "unknown",
                           parentSessionId=None, agentName=None)
                if initiator == "agent":
                    row.update(initiatorName="계획 에이전트", agentName="계획 워커")
                    # This fixture explicitly creates a child of the first
                    # same-tool, same-home row; production never infers this.
                    parent = next((item for item in rows[:index]
                                   if item.get("agent") == row.get("agent")
                                   and item.get("home") == row.get("home") and item.get("id")), None)
                    if parent:
                        row["parentSessionId"] = parent["id"]
            return data

        runner = httpd.board.runner

        def metadata_runner(host, command, timeout):
            result = runner(host, command, timeout)
            return with_session_metadata(result) if command[0] == "snapshot" else result

        httpd.board.runner = metadata_runner
        for host in httpd.board.state.values():
            data = with_session_metadata(host.get("data") or {})
            host["data"] = data
            rows = data.get("codex", []) + data.get("claude", [])
            refs = [{"host": host["name"], "agent": r["agent"], "home": r.get("home"), "id": r["id"]} for r in rows]
            if not httpd.goals.snapshot()["goals"]:
                samples = [
                    ("제품 도움말 정리", "todo", "다음 배포에 맞춰 도움말을 준비해요.", "주요 질문을 모으기"),
                    ("모바일 대화 경험 개선", "in_progress", "집중 모드와 입력창을 검증하고 있어요.", "태블릿에서 읽던 위치 확인"),
                    ("자료 수신 후 제안서 작성", "waiting", "파트너의 자료를 기다리고 있어요.", "자료가 도착하면 구성안 작성"),
                    ("콘텐츠 초안 검토", "review", "첫 초안과 편집 근거가 준비됐어요.", "표현과 이미지 확인"),
                    ("검색 오류 수정", "done", "빈 결과 안내와 회귀 테스트를 확인했어요.", "후속 요청이 오면 이어가기"),
                ]
                for index, (title, status, current, next_action) in enumerate(samples):
                    httpd.goals.save({"title": title, "status": status, "current": current,
                                     "next": next_action, "completionCriteria": "합성 데이터로 기능과 화면을 확인한다.",
                                     "evidence": "합성 회귀 검증 결과" if status == "done" else "",
                                     "waitingReason": "파트너 자료 수신 대기" if status == "waiting" else "",
                                     "project": "샘플 프로젝트", "pinned": index == 1,
                                     "sessions": refs[:2] if index == 1 else refs[2:] if index == 3 else []})
        return httpd

    with patch.object(fixture.server, "make_server", seeded):
        fixture.main()


if __name__ == "__main__":
    main()
