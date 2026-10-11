import io
import json
import os
import shutil
import socket
import struct
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector  # noqa: E402


class SocketBufferTest(unittest.TestCase):
    class Socket:
        def __init__(self, data, chunk=65536):
            self.data, self.chunk, self.offset = data, chunk, 0
            self.sent, self.closed = [], False

        def settimeout(self, timeout): pass
        def connect(self, path): pass
        def close(self): self.closed = True
        def sendall(self, data): self.sent.append(data)
        def recv(self, limit):
            end = min(self.offset + min(limit, self.chunk), len(self.data))
            result, self.offset = self.data[self.offset:end], end
            return result

    @staticmethod
    def frame(payload, opcode=1, final=True, masked=False):
        size = len(payload)
        header = bytes([(0x80 if final else 0) | opcode,
                        (0x80 if masked else 0) | (size if size < 126 else 126 if size < 65536 else 127)])
        if size >= 126:
            header += struct.pack('>H' if size < 65536 else '>Q', size)
        if masked:
            mask = b'\x01\x02\x03\x04'
            header += mask
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return header + payload

    def client(self, wire, chunk=65536):
        client = object.__new__(collector.AppServer)
        client.sock = self.Socket(wire, chunk)
        client.buf = bytearray()
        return client

    def test_handshake_preserves_prefetched_rpc_frames(self):
        wire = (b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n'
                + self.frame(b'{"id":1,"result":{}}')
                + self.frame(b'{"method":"notification","params":{}}')
                + self.frame(b'{"id":2,"result":{"ok":true}}'))
        sock = self.Socket(wire)
        with patch.object(collector.socket, 'socket', return_value=sock):
            with collector.AppServer('/synthetic/socket') as client:
                self.assertEqual(client.call('thread/read', {}), {'ok': True})
        self.assertTrue(sock.closed)

    def test_fragmented_masked_text_and_ping_across_small_reads(self):
        payload = ('한글 메시지 ' * 30).encode()
        client = self.client(self.frame(payload[:100], final=False, masked=True)
                             + self.frame(b'ping', opcode=9)
                             + self.frame(payload[100:], opcode=0, masked=True), chunk=7)
        self.assertEqual(client._message(), payload)
        self.assertEqual(client.sock.sent[0][0], 0x8A)
        self.assertEqual(self.client(client.sock.sent[0])._frame()[2], b'ping')

    def test_large_frame_and_following_message_preserve_exact_bytes(self):
        payload = bytes(range(256)) * 8192
        client = self.client(self.frame(payload, opcode=2) + self.frame(b'next'), chunk=8192)
        self.assertEqual(client._message(), payload)
        self.assertEqual(client._message(), b'next')
        with self.assertRaises(collector.RpcError):
            client._message()


class LabelTest(unittest.TestCase):
    def test_home_labels_use_explicit_settings_and_ignore_legacy_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config/sessionholic/config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"version": 1, "profiles": [
                {"agent": "codex", "home": ".codex-beta", "label": "Example"}]}))
            legacy = home / ".codex-router/0123456789abcdef/ag-sessions"
            legacy.mkdir(parents=True)
            (legacy / "old.json").write_text('{"scope":{"account":"DO-NOT-IMPORT"}}')
            with patch.object(collector, "HOME", home):
                self.assertEqual(collector.home_label(".codex-beta", home / ".codex-beta"), (None, "Example"))
                self.assertEqual(collector.home_label(".codex-router/0123456789abcdef", legacy.parent),
                                 (None, ".codex-router/0123456789abcdef"))
                self.assertEqual(collector.home_label(".codex", home / ".codex"), (None, "기본"))

    def test_project_for_prefers_longest_root(self):
        projects = [{"id": "a", "label": "예제", "root": "/Users/u/workspace"},
                    {"id": "b", "label": "하위 프로젝트", "root": "/Users/u/workspace/backoffice"}]
        self.assertEqual(collector.project_for("/Users/u/workspace/backoffice/src", projects), "하위 프로젝트")
        self.assertEqual(collector.project_for("/Users/u/workspace", projects), "예제")
        self.assertEqual(collector.project_for("/Users/u/workspaceX", projects), "workspaceX")
        self.assertEqual(collector.project_for("", projects), "")

    def test_load_projects_uses_explicit_json_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"version": 1, "projects": [{"id": "example", "label": "Example", "root": "/example"}]}))
            self.assertEqual(collector.load_projects(path), [{"id": "example", "label": "Example", "root": "/example"}])

    def test_home_key_rejects_other_paths(self):
        for good in (".codex", ".codex-beta", ".codex-router/0123456789abcdef"):
            self.assertTrue(collector.HOME_KEY.match(good), good)
        for bad in ("../.codex", ".codex/../../etc", ".codex-router/xyz", "/tmp/x", ".codex-router/0123456789abcdef/.."):
            self.assertFalse(collector.HOME_KEY.match(bad), bad)


class SessionOriginTest(unittest.TestCase):
    def codex(self, **fields):
        return collector.codex_row({'id': 'child-session', **fields}, '.codex', None, 'Fixture', set(), [])

    def test_client_and_execution_modes_never_determine_initiator(self):
        for source in ('cli', 'vscode', 'exec', 'appServer', 'mcp', 'background', 'future-client', None):
            with self.subTest(source=source):
                row = self.codex(source=source, originator='codex_cli_rs', agentNickname='Current worker')
                self.assertEqual((row['initiator'], row['initiatorEvidence']), ('unknown', 'unknown'))
                self.assertIsNone(row['initiatorName'])
                self.assertEqual(row['agentName'], 'Current worker')
        for kind in ('interactive', 'background'):
            row = collector.claude_row({'id': 'abcd', 'kind': kind}, {}, [])
            self.assertEqual(row['initiator'], 'unknown')

    def test_explicit_initiator_and_parent_preserve_executor_separately(self):
        row = self.codex(source='exec', initiator={'type': 'human', 'name': 'Owner'},
                         parentSessionId='parent-session', agentNickname='Executor')
        self.assertEqual((row['initiator'], row['initiatorName'], row['initiatorEvidence']),
                         ('human', 'Owner', 'explicit'))
        self.assertEqual((row['parentSessionId'], row['agentName']), ('parent-session', 'Executor'))
        row = collector.claude_row({'id': 'abcd', 'kind': 'interactive', 'initiator': 'agent',
                                    'initiatorName': 'Scheduler', 'parentSessionId': 'parent-session',
                                    'agentName': 'Reviewer'}, {}, [])
        self.assertEqual((row['initiator'], row['initiatorName'], row['parentSessionId'], row['agentName']),
                         ('agent', 'Scheduler', 'parent-session', 'Reviewer'))

    def test_structured_codex_subagent_rpc_and_persisted_forms(self):
        # Native schemas: app-server v2 SessionSource uses subAgent; persisted protocol
        # uses subagent. SubAgentSource.thread_spawn fields keep their snake_case.
        for key in ('subAgent', 'subagent'):
            source = {key: {'thread_spawn': {'parent_thread_id': 'parent-session', 'depth': 1,
                                             'agent_nickname': 'Reviewer', 'agent_role': 'explorer'}}}
            for value in (source, json.dumps(source)):
                with self.subTest(source=value):
                    row = self.codex(source=value)
                    self.assertEqual(row['source'], source)
                    self.assertEqual((row['initiator'], row['initiatorEvidence'], row['parentSessionId']),
                                     ('agent', 'subagent', 'parent-session'))
                    self.assertEqual((row['agentName'], row['agentRole']), ('Reviewer', 'explorer'))
                    self.assertIsNone(row['initiatorName'], 'A child nickname is not the initiating parent name')

    def test_parent_alone_and_unrecognized_source_keep_unknown(self):
        for source in ({'futureSource': {'mode': 'agent'}}, {'subAgent': None},
                       {'subAgent': {'thread_spawn': 'bad'}}, 'subagent', '{invalid', None):
            row = self.codex(source=source, parentThreadId='parent-session')
            self.assertEqual(row['initiator'], 'unknown')
            self.assertEqual(row['parentSessionId'], 'parent-session')
            self.assertEqual(row['source'], source)

    def test_explicit_initiator_precedes_native_source_and_malformed_metadata_is_bounded(self):
        row = self.codex(source={'subAgent': 'review'}, initiator='human', initiatorName='Owner')
        self.assertEqual((row['initiator'], row['initiatorEvidence']), ('human', 'explicit'))
        row = self.codex(source={'unbounded': 'x' * 4097}, initiator=['agent'],
                         initiatorName='x\nsecret', parentSessionId={'id': 'parent'}, agentNickname='x' * 257)
        self.assertEqual(row['initiator'], 'unknown')
        self.assertTrue(all(row[field] is None for field in ('source', 'initiatorName', 'parentSessionId', 'agentName')))

    def test_native_listing_requests_agent_sources_and_reports_legacy_filter_fallback(self):
        calls = []
        class Legacy:
            def call(self, method, params):
                calls.append(params)
                if 'sourceKinds' in params:
                    raise collector.RpcReplyError('unsupported source filter', -32602)
                return {'data': [{'id': 'legacy', 'source': 'cli'}]}
        listed, quick = collector.list_threads(Legacy())
        self.assertFalse(quick)
        self.assertIn('subAgent', calls[0]['sourceKinds'])
        self.assertIn('exec', calls[0]['sourceKinds'])
        self.assertEqual(len(calls), 3)
        rows, errors = collector.codex_home_rows('.codex', None, 'Fixture', (listed, set(), None), [])
        self.assertEqual(rows[0]['initiator'], 'unknown')
        self.assertIn('대화형 기록만', errors[0])


class PhaseTest(unittest.TestCase):
    def test_claude(self):
        self.assertEqual(collector.claude_phase("blocked", "idle"), "needs_input")
        self.assertEqual(collector.claude_phase("failed", None), "needs_input")
        self.assertEqual(collector.claude_phase("done", "waiting"), "needs_input")
        self.assertEqual(collector.claude_phase("working", "busy"), "working")
        self.assertEqual(collector.claude_phase("done", "busy"), "working")
        self.assertEqual(collector.claude_phase("done", "idle"), "done")
        self.assertEqual(collector.claude_phase("something-new", None), "idle")

    def test_codex(self):
        self.assertEqual(collector.codex_phase({"type": "active", "activeFlags": ["waitingOnApproval"]}), "needs_input")
        self.assertEqual(collector.codex_phase({"type": "systemError"}), "needs_input")
        self.assertEqual(collector.codex_phase({"type": "active", "activeFlags": []}), "working")
        self.assertEqual(collector.codex_phase({"type": "idle"}), "idle")
        self.assertEqual(collector.codex_phase({"type": "notLoaded"}), "closed")
        self.assertEqual(collector.codex_phase(None), "closed")


class MessageTest(unittest.TestCase):
    def test_mixed_and_image_only_codex_history_keeps_evidence_without_content(self):
        image = {"type": "localImage", "path": "/example/private/image.png"}
        rows = collector.codex_messages([{"items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "확인"}, image]},
            {"type": "userMessage", "content": [{"type": "image", "url": "data:image/png;base64,PRIVATE"}]}]}])
        self.assertEqual([row["text"] for row in rows], ["확인", "[이미지 첨부]"])
        self.assertTrue(all(row["attachments"][0]["source"] == "native" for row in rows))
        self.assertNotIn("private", json.dumps(rows))
        self.assertNotIn("PRIVATE", json.dumps(rows))

        document = collector.codex_messages([{"items": [{"type": "userMessage", "content": [
            {"type": "document", "source": {"data": "PRIVATE"}}]}]}])
        self.assertEqual(document[0]["text"], "[파일 첨부]")
        self.assertEqual(document[0]["attachments"][0]["kind"], "file")
        self.assertNotIn("PRIVATE", json.dumps(document))

    def test_claude_image_evidence_is_attached_once_and_meta_stays_hidden(self):
        image = {"type": "image", "source": {"type": "base64", "data": "PRIVATE"}}
        rows = collector.claude_messages([
            {"type": "user", "message": {"content": [{"type": "text", "text": "첫 문장"}, image,
                                                         {"type": "text", "text": "둘째 문장"}]}},
            {"type": "user", "message": {"content": [image]}},
            {"type": "user", "isMeta": True, "message": {"content": [image]}}])
        self.assertEqual([r["text"] for r in rows], ["첫 문장", "둘째 문장", "[이미지 첨부]"])
        self.assertEqual([len(r.get("attachments", [])) for r in rows], [1, 0, 1])
        self.assertNotIn("PRIVATE", json.dumps(rows))

    def test_claude_input_typed_while_working_is_a_user_row_shown_once(self):
        # Claude Code records input sent while it works (a board paste included) as a
        # queued_command attachment, not as a user line.
        def queued(prompt, kind="human", mode="prompt", ts="2026-10-07T01:00:10Z"):
            return {"type": "attachment", "timestamp": ts, "attachment": {
                "type": "queued_command", "prompt": prompt, "origin": {"kind": kind}, "commandMode": mode}}
        image = {"type": "image", "source": {"type": "base64", "data": "PRIVATE"}}
        entries = [
            {"type": "user", "timestamp": "2026-10-07T01:00:00Z", "message": {"content": "정리해 줘"}},
            {"type": "assistant", "timestamp": "2026-10-07T01:00:05Z",
             "message": {"content": [{"type": "text", "text": "정리하겠습니다."}]}},
            queued("<system-reminder>숨김</system-reminder>이것도 확인해 줘"),
            queued("<task-notification><task-id>x</task-id></task-notification>", "task-notification", "task-notification"),
            queued([{"type": "text", "text": "[Image #1]그림 봐 줘"}, image], ts="2026-10-07T01:00:12Z"),
            {"type": "attachment", "attachment": {"type": "hook_success", "content": "숨김"}},
        ]
        rows = collector.claude_messages(entries)
        self.assertEqual([(r["role"], r["text"]) for r in rows],
                         [("user", "정리해 줘"), ("assistant", "정리하겠습니다."),
                          ("user", "이것도 확인해 줘"), ("user", "[Image #1]그림 봐 줘")])
        self.assertEqual(rows[2]["ts"], 1791334810)
        self.assertEqual(len(rows[3]["attachments"]), 1)
        self.assertNotIn("PRIVATE", json.dumps(rows))
        # An interrupt hands the queued input back; Claude Code records it again as a prompt.
        again = entries[:3] + [
            {"type": "user", "message": {"content": "[Request interrupted by user]"}},
            {"type": "user", "timestamp": "2026-10-07T01:00:20Z", "message": {"content": "이것도  확인해 줘"}}]
        rows = collector.claude_messages(again)
        self.assertEqual([(r["role"], r["text"]) for r in rows],
                         [("user", "정리해 줘"), ("assistant", "정리하겠습니다."), ("system", "사용자가 중단함"),
                          ("user", "이것도  확인해 줘")])
        self.assertEqual(rows[-1]["ts"], 1791334820)
        # The same words asked again after an answer are a new message.
        later = entries[:3] + [{"type": "assistant", "message": {"content": [{"type": "text", "text": "확인했습니다."}]}},
                               {"type": "user", "message": {"content": "이것도 확인해 줘"}}]
        self.assertEqual([r["text"] for r in collector.claude_messages(later)].count("이것도 확인해 줘"), 2)

    def test_claude_messages_hide_harness_wrappers(self):
        entries = [
            {"type": "user", "timestamp": "2026-10-07T01:00:00Z",
             "message": {"content": "<system-reminder>숨김</system-reminder>\n정산 확인해줘"}},
            {"type": "user", "message": {"content": "<task-notification><task-id>x</task-id></task-notification>"}},
            {"type": "user", "message": {"content": "<command-name>/model</command-name>"}},
            {"type": "user", "message": {"content": "<local-command-stdout>ok</local-command-stdout>"}},
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "숨김"},
                {"type": "text", "text": "확인하겠습니다."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls\n-la", "description": "목록 보기"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "결과"}]}},
            {"type": "attachment", "message": {"content": "x"}},
        ]
        got = [(m["role"], m["text"]) for m in collector.claude_messages(entries)]
        self.assertEqual(got, [("user", "정산 확인해줘"), ("system", "백그라운드 작업 알림"), ("system", "명령 /model"),
                               ("assistant", "확인하겠습니다."), ("tool", "Bash · 목록 보기")])
        self.assertEqual(collector.claude_messages(entries[:1])[0]["ts"], 1791334800)

    def test_codex_messages(self):
        turns = [{"startedAt": 1791334800, "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "배포해"}, {"type": "image"}]},
            {"type": "reasoning", "summary": ["숨김"]},
            {"type": "commandExecution", "command": ["git", "status"]},
            {"type": "fileChange", "changes": [{"path": "a.py"}, {"path": "b.py"}]},
            {"type": "mcpToolCall", "server": "slack", "tool": "send"},
            {"type": "agentMessage", "text": "완료했습니다."},
        ]}]
        got = [(m["role"], m["text"]) for m in collector.codex_messages(turns)]
        self.assertEqual(got, [("user", "배포해"), ("tool", "$ git status"), ("tool", "파일 수정 · a.py, b.py"),
                               ("tool", "slack send"), ("assistant", "완료했습니다.")])
        # Every tool row names its kind for the board's "도구 실행 N개 · 명령 1 · …" line.
        self.assertEqual([m.get("toolKind") for m in collector.codex_messages(turns) if m["role"] == "tool"],
                         ["command", "edit", "mcp"])
        searched = collector.codex_messages([{"items": [{"type": "webSearch", "query": "x"},
                                                        {"type": "dynamicToolCall", "tool": "t"}]}])
        self.assertEqual([m["toolKind"] for m in searched], ["search", "other"])

    def test_codex_times_come_from_the_turn_start_and_completion_only(self):
        # Items carry no time: the question shows the turn's start, the final answer its
        # completion, and progress notes, tools and inputs sent mid-turn show none.
        turn = {"id": "t1", "status": "completed", "startedAt": 1000, "completedAt": 2500, "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "질문"}]},
            {"type": "agentMessage", "text": "살펴볼게요", "phase": "commentary"},
            {"type": "commandExecution", "command": "ls"},
            {"type": "userMessage", "content": [{"type": "text", "text": "중간 추가 지시"}]},
            {"type": "agentMessage", "text": "최종 답", "phase": "final_answer"},
            {"type": "agentMessage", "text": "덧붙임", "phase": "commentary"},
        ]}
        self.assertEqual([(m["text"], m["ts"]) for m in collector.codex_messages([turn])],
                         [("질문", 1000), ("살펴볼게요", None), ("$ ls", None), ("중간 추가 지시", None),
                          ("최종 답", 2500), ("덧붙임", None)])
        # Without phases, a finished turn's last answer is its final one.
        plain = {**turn, "items": [turn["items"][0], {"type": "agentMessage", "text": "하나"},
                                   {"type": "agentMessage", "text": "둘"}]}
        self.assertEqual([m["ts"] for m in collector.codex_messages([plain])], [1000, None, 2500])
        # A running turn has no completion yet; older history without times shows none.
        running = {**plain, "status": "inProgress", "completedAt": None}
        self.assertEqual([m["ts"] for m in collector.codex_messages([running])], [1000, None, None])
        self.assertEqual([m["ts"] for m in collector.codex_messages([{"items": plain["items"]}])], [None, None, None])
        # The same rows per item for the native read path, which pairs them with item ids.
        pairs = collector.codex_turn_rows(turn)
        self.assertEqual(len(pairs), 6)
        self.assertIs(pairs[4][0], turn["items"][4])
        self.assertEqual(pairs[4][1][0]["ts"], 2500)

    def test_claude_harness_lines_are_notes_not_user_bubbles(self):
        entries = [
            {"type": "user", "isCompactSummary": True, "timestamp": 1791334800,
             "message": {"content": "This session is being continued from a previous conversation…"}},
            {"type": "user", "timestamp": 1791334801, "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
            {"type": "user", "timestamp": 1791334802,
             "message": {"content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}},
            {"type": "user", "timestamp": 1791334803, "message": {"content": "<bash-input>git status</bash-input>"}},
            {"type": "user", "timestamp": 1791334804, "message": {"content": "<bash-stdout>clean</bash-stdout><bash-stderr></bash-stderr>"}},
            {"type": "assistant", "timestamp": 1791334805, "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
                {"type": "tool_use", "name": "mcp__slack__send", "input": {}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "b.py"}},
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}},
                {"type": "tool_use", "name": "TodoWrite", "input": {}}]}},
        ]
        got = [(m["role"], m["text"], m.get("toolKind")) for m in collector.claude_messages(entries)]
        self.assertEqual(got, [("system", "이전 대화 요약으로 이어짐", None), ("system", "사용자가 중단함", None),
                               ("system", "사용자가 중단함", None), ("tool", "$ git status", "command"),
                               ("tool", "Edit · a.py", "edit"), ("tool", "mcp__slack__send", "mcp"),
                               ("tool", "Read · b.py", "read"), ("tool", "Grep · x", "search"),
                               ("tool", "TodoWrite", "other")])
        self.assertNotIn("continued", json.dumps(collector.claude_messages(entries)))

    def test_claude_prompt_time_skips_tool_results_and_harness_lines(self):
        entries = [
            {"type": "user", "timestamp": 100, "message": {"content": "첫 질문"}},
            {"type": "user", "timestamp": 200, "message": {"content": "두 번째 질문"}},
            {"type": "assistant", "timestamp": 210, "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "user", "timestamp": 220, "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "timestamp": 230, "isMeta": True, "message": {"content": "meta"}},
            {"type": "user", "timestamp": 240, "message": {"content": "[Request interrupted by user]"}},
            {"type": "user", "timestamp": 250, "message": {"content": "<task-notification>x</task-notification>"}},
            {"type": "user", "timestamp": 260, "isCompactSummary": True, "message": {"content": "요약"}},
        ]
        self.assertEqual(collector.claude_prompt_at(entries), 200)
        image = {"type": "user", "timestamp": 300, "message": {"content": [{"type": "image", "source": {}}]}}
        self.assertEqual(collector.claude_prompt_at(entries + [image]), 300)
        self.assertIsNone(collector.claude_prompt_at(entries[2:]))

    def test_claude_row_without_an_id_keeps_its_process_id_apart(self):
        rows = [{"id": "", "sessionId": "", "pid": 4242, "kind": "interactive", "state": "working", "cwd": "/w"},
                {"id": "agent123", "sessionId": "s" * 8, "pid": 7, "kind": "background", "state": "idle", "cwd": "/w"}]
        out = collector.claude_session_rows(rows, [], [], transcripts={})
        self.assertEqual((out[0]["id"], out[0]["pid"]), ("", 4242))
        self.assertNotIn("pid", out[1])

    def test_tail_entries_drops_cut_first_line(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".jsonl", delete=False) as f:
            for i in range(50):
                f.write((json.dumps({"type": "user", "n": i, "pad": "x" * 40}) + "\n").encode())
        entries = list(collector.tail_entries(f.name, 300))
        self.assertTrue(entries)
        self.assertEqual(entries[-1]["n"], 49)
        self.assertTrue(all("n" in e for e in entries))

    def test_to_epoch(self):
        self.assertEqual(collector.to_epoch(1791334800), 1791334800)
        self.assertEqual(collector.to_epoch(1791334800123), 1791334800)
        self.assertEqual(collector.to_epoch("2026-10-07T01:00:00Z"), 1791334800)
        self.assertIsNone(collector.to_epoch("not a date"))
        self.assertIsNone(collector.to_epoch(True))

    def test_read_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            collector.read("claude", "../../etc/passwd")
        with self.assertRaises(ValueError):
            collector.read("codex", "019a0000-0000-7000-8000-000000000000", "../x")
        with self.assertRaises(ValueError):
            collector.read("gemini", "019a0000-0000-7000-8000-000000000000")

    def test_clipped_rows_report_truncation_and_original_length(self):
        long_answer, long_request = "답" * 5000, "요" * 4001
        rows = collector.codex_messages([{"items": [
            {"type": "userMessage", "content": [{"type": "text", "text": long_request}]},
            {"type": "agentMessage", "text": long_answer},
            {"type": "agentMessage", "text": "짧은 답"},
            {"type": "agentMessage", "text": "x" * collector.TEXT_LIMIT}]}])
        self.assertEqual([(r.get("truncated"), r.get("fullLength")) for r in rows],
                         [(True, 4001), (True, 5000), (None, None), (None, None)])
        self.assertEqual(rows[1]["text"], "답" * collector.TEXT_LIMIT + " …")
        self.assertNotIn("truncated", rows[2])
        self.assertNotIn("fullLength", rows[3])

        hidden = "<system-reminder>" + "숨김" * 100 + "</system-reminder>"
        rows = collector.claude_messages([
            {"type": "user", "message": {"content": hidden + "u" * 4100}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "a" * 4500},
                                                          {"type": "text", "text": "끝"}]}}])
        self.assertEqual([(r["role"], r.get("truncated"), r.get("fullLength")) for r in rows],
                         [("user", True, 4100), ("assistant", True, 4500), ("assistant", None, None)])
        self.assertEqual(len(rows[1]["text"]), collector.TEXT_LIMIT + 2)

    def test_malformed_history_rows_are_skipped(self):
        rows = collector.codex_messages([None, "turn", {"items": [None, "item", {"type": "fileChange", "changes": 5},
                                                                  {"type": "userMessage", "content": "text"},
                                                                  {"type": "agentMessage", "text": "남은 답"}]}])
        self.assertEqual([(r["role"], r["text"]) for r in rows], [("tool", "파일 수정 · "), ("assistant", "남은 답")])
        rows = collector.claude_messages([{"type": "assistant", "message": "not-a-dict"},
                                          {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}])
        self.assertEqual([r["text"] for r in rows], ["ok"])

    def test_read_reports_transcript_race_instead_of_failing(self):
        with patch.object(collector, "claude_transcript", return_value=Path("/nonexistent/fixture.jsonl")):
            result = collector.read("claude", "019a0000-0000-7000-8000-000000000000")
        self.assertEqual(result["messages"], [])
        self.assertIn("읽지 못했습니다", result["error"])


class SnapshotIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.projects = self.home / ".claude/projects"
        patcher = patch.object(collector, "HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def transcript(self, session_id, entries, project="example"):
        path = self.projects / project / (session_id + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
        return path

    @staticmethod
    def answer(text):
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}

    def sessions(self, rows):
        errors = []
        with patch.object(collector, "which", return_value="/fixture/claude"), \
             patch.object(collector, "run", return_value=json.dumps(rows)):
            return collector.claude_sessions(errors, []), errors

    def test_one_source_failure_keeps_the_other_sources(self):
        with patch.object(collector, "load_projects", return_value=[]), \
             patch.object(collector, "claude_sessions", return_value=[{"id": "fixture"}]), \
             patch.object(collector, "codex_threads", side_effect=AttributeError("fixture")), \
             patch.object(collector, "tmux_sessions", side_effect=PermissionError("fixture")):
            result = collector.snapshot()
        self.assertEqual(result["claude"], [{"id": "fixture"}])
        self.assertEqual((result["codex"], result["tmux"]), ([], []))
        self.assertEqual(len(result["errors"]), 2)
        self.assertNotIn("fixture", json.dumps(result["errors"]))

    def test_broken_claude_rows_and_transcript_races_are_isolated(self):
        good, unreadable, gone = ("a0000000-0000-4000-8000-00000000000" + str(i) for i in range(3))
        self.transcript(good, [self.answer("정상 미리보기")])
        locked = self.transcript(unreadable, [self.answer("읽지 못함")])
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        index = collector.claude_transcript_index()
        index[gone] = self.projects / "example" / (gone + ".jsonl")
        rows = ["not-a-dict", ["list"],
                {"id": "row-good", "sessionId": good, "name": "정상", "startedAt": 1791334800},
                {"id": "row-locked", "sessionId": unreadable, "startedAt": 1791334800},
                {"id": "row-gone", "sessionId": gone, "startedAt": 1791334800}]
        with patch.object(collector, "claude_transcript_index", return_value=index):
            out, errors = self.sessions(rows)
        self.assertEqual([row["id"] for row in out], ["row-good", "row-locked", "row-gone"])
        self.assertEqual(out[0]["snippet"], "정상 미리보기")
        self.assertEqual([row["snippet"] for row in out[1:]], ["", ""])
        self.assertEqual(out[2]["updatedAt"], 1791334800)
        self.assertEqual(errors, ["Claude 세션 2건을 읽지 못했습니다."])

    def test_snapshot_scans_transcripts_once_and_widens_tail_only_when_needed(self):
        near, far, other = ("b0000000-0000-4000-8000-00000000000" + str(i) for i in range(3))
        self.transcript(near, [{"type": "user", "message": {"content": "질문"}}, self.answer("가까운 답")])
        filler = [{"type": "user", "message": {"content": "x" * 1000}} for _ in range(40)]
        self.transcript(far, [self.answer("먼 답")] + filler)
        self.transcript(other, [self.answer("오래된 답")], project="older")
        newer = self.transcript(other, [self.answer("최신 답")], project="newer")
        os.utime(newer, (2000000000, 2000000000))
        rows = [{"id": "n", "sessionId": near}, {"id": "f", "sessionId": far}, {"id": "o", "sessionId": other}]
        with patch.object(collector.glob, "glob", wraps=collector.glob.glob) as scan, \
             patch.object(collector, "tail_entries", wraps=collector.tail_entries) as tail:
            out, errors = self.sessions(rows)
        self.assertEqual(scan.call_count, 1)
        self.assertEqual([row["snippet"] for row in out], ["가까운 답", "먼 답", "최신 답"])
        self.assertEqual(errors, [])
        sizes = [(Path(call.args[0]).stem, call.args[1]) for call in tail.call_args_list]
        self.assertEqual(sizes, [(near, 32 * 1024), (far, 32 * 1024), (far, 128 * 1024), (other, 32 * 1024)])

    def test_codex_rows_are_isolated(self):
        class Server:
            def __init__(self, path):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def call(self, method, params):
                if method == "thread/list":
                    return {"data": ["bad", {"id": "t1", "name": "작업", "status": {"type": "idle"}},
                                     {"id": ["unhashable"]}]}
                return {"data": ["t1"]}
        errors = []
        with patch.object(collector, "codex_homes", return_value=[(".codex", self.home / ".codex")]), \
             patch.object(collector, "AppServer", Server):
            rows = collector.codex_threads(errors, [])
        self.assertEqual([(row["id"], row["loaded"]) for row in rows], [("t1", True)])
        self.assertEqual(errors, ["Codex(기본) 작업 2건을 읽지 못했습니다."])

    def test_codex_homes_are_listed_concurrently_in_home_order(self):
        import threading
        import time
        keys = [".codex", ".codex-a", ".codex-b", ".codex-c", ".codex-d"]
        homes = [(key, self.home / key) for key in keys]
        # The four first homes meet at once, which a serial loop could never do.
        barrier = threading.Barrier(4, timeout=5)
        lock, active, peak, sockets = threading.Lock(), [0], [0], []

        class Server:
            def __init__(self, path):
                self.home = Path(path).parent.parent.name
                with lock:
                    sockets.append(self.home)
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                with lock:
                    active[0] -= 1
                return False
            def call(self, method, params):
                if method == "thread/loaded/list":
                    return {"data": []}
                if self.home in keys[:4]:
                    barrier.wait()
                # Earlier homes answer later, so completion order differs from home order.
                time.sleep(0.05 * (len(keys) - keys.index(self.home)))
                if self.home == ".codex-b":
                    raise collector.RpcError("fixture")
                return {"data": [{"id": "t" + self.home, "status": {"type": "idle"}}]}

        errors = []
        with patch.object(collector, "codex_homes", return_value=homes), \
             patch.object(collector, "AppServer", Server):
            rows = collector.codex_threads(errors, [])
        self.assertEqual([row["home"] for row in rows], [".codex", ".codex-a", ".codex-c", ".codex-d"])
        self.assertEqual(errors, ["Codex daemon(.codex-b)에 연결하지 못했습니다."])
        # One independent connection per home, never more than four at a time.
        self.assertEqual(sorted(sockets), sorted(keys))
        self.assertEqual(peak[0], 4)


class StateDbListingTest(unittest.TestCase):
    """`thread/list` asks for the state DB, and retries once without it only on a reply error."""

    class Server:
        def __init__(self, calls, reject=False, broken=False):
            self.calls, self.reject, self.broken = calls, reject, broken
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def call(self, method, params):
            self.calls.append((method, dict(params)))
            if self.broken:
                raise collector.RpcError("연결 끊김")
            if method == "thread/list" and params.get("useStateDbOnly") and self.reject:
                raise collector.RpcReplyError("unknown field `useStateDbOnly`", -32602)
            return {"data": [{"id": "t1", "status": {"type": "idle"}}]} if method == "thread/list" else {"data": []}

    def home_threads(self, full=False, **options):
        calls = []
        with patch.object(collector, "AppServer", lambda path: self.Server(calls, **options)):
            reply = collector.codex_home_threads(Path("/synthetic/.codex"), full)
        return reply, [(method, params.get("useStateDbOnly")) for method, params in calls]

    def test_state_db_listing_and_one_retry_without_the_argument(self):
        reply, calls = self.home_threads()
        self.assertEqual(reply[2], None)
        self.assertEqual(calls, [("thread/list", True), ("thread/loaded/list", None)])
        reply, calls = self.home_threads(reject=True)
        self.assertEqual([row["id"] for row in reply[0]["data"]], ["t1"])
        self.assertEqual(calls, [("thread/list", True), ("thread/list", None), ("thread/loaded/list", None)])

    def test_connection_failures_and_full_listing_are_not_retried(self):
        reply, calls = self.home_threads(broken=True)
        self.assertEqual(reply, (None, None, "connect"))
        self.assertEqual(calls, [("thread/list", True)])
        reply, calls = self.home_threads(full=True)
        self.assertEqual(calls, [("thread/list", None), ("thread/loaded/list", None)])

    def test_reply_errors_keep_their_code_and_remain_rpc_errors(self):
        wire = (b'HTTP/1.1 101 Switching Protocols\r\n\r\n' + SocketBufferTest.frame(b'{"id":1,"result":{}}')
                + SocketBufferTest.frame(b'{"id":2,"error":{"code":-32603,"message":"state db"}}'))
        sock = SocketBufferTest.Socket(wire)
        with patch.object(collector.socket, 'socket', return_value=sock):
            with collector.AppServer('/synthetic/socket') as client:
                with self.assertRaises(collector.RpcReplyError) as caught:
                    client.call('thread/list', {})
        self.assertIsInstance(caught.exception, collector.RpcError)
        self.assertEqual(caught.exception.code, -32603)

    def test_snapshot_full_flag_is_the_only_snapshot_option(self):
        with patch.object(collector, "snapshot", return_value={"ok": 1}) as snap, \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            collector.main(["snapshot", "--full"])
            collector.main(["snapshot"])
            collector.main(["snapshot", "--other"])
        self.assertEqual([call.kwargs for call in snap.call_args_list], [{"full": True}, {"full": False}])
        self.assertIn("사용법", out.getvalue().splitlines()[-1])


class LinkedHomeTest(unittest.TestCase):
    def test_symlinked_codex_homes_are_listed_once(self):
        home = Path(tempfile.mkdtemp(prefix="sh", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, home)
        for key in (".codex", ".codex-work", ".codex-router/0123456789abcdef"):
            socket_path = home / key / collector.SOCKET_REL
            socket_path.parent.mkdir(parents=True)
            socket_path.write_text("")
        # An alias folder and a router entry pointing at an existing home are the same daemon.
        (home / ".codex-alias").symlink_to(home / ".codex-work")
        (home / ".codex-router/fedcba9876543210").symlink_to(home / ".codex-work")
        with patch.object(collector, "HOME", home), patch.object(collector, "ROUTER", home / ".codex-router"):
            keys = [key for key, _ in collector.codex_homes()]
        self.assertEqual(keys, [".codex", ".codex-work", ".codex-router/0123456789abcdef"])


class FakeDaemon:
    """A Codex app-server stand-in on a real Unix socket: WebSocket upgrade, JSON-RPC
    replies from `threads`, and notifications pushed to every initialized client."""

    def __init__(self, path, threads=None, loaded=(), reject_state_db=False):
        self.path = str(path)
        self.threads = list(threads or [])
        self.loaded = list(loaded)
        self.reject_state_db = reject_state_db
        self.lock = threading.Lock()
        self.calls, self.clients, self.workers = [], [], []
        self.capabilities = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(8)
        # A close from another thread does not wake a blocked accept on every platform.
        self.listener.settimeout(0.05)
        self.closed = False
        self.acceptor = threading.Thread(target=self.accept, daemon=True)
        self.acceptor.start()

    @staticmethod
    def recv_exact(conn, size):
        data = b""
        while len(data) < size:
            chunk = conn.recv(size - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    def accept(self):
        while not self.closed:
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            worker = threading.Thread(target=self.serve, args=(conn,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve(self, conn):
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += self.recv_exact(conn, 1)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            while True:
                head = self.recv_exact(conn, 2)
                opcode, size = head[0] & 0x0F, head[1] & 0x7F
                if size == 126:
                    size = struct.unpack(">H", self.recv_exact(conn, 2))[0]
                elif size == 127:
                    size = struct.unpack(">Q", self.recv_exact(conn, 8))[0]
                mask = self.recv_exact(conn, 4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self.recv_exact(conn, size)))
                if opcode == 0x8:
                    return
                message = json.loads(payload)
                if "id" in message:
                    self.answer(conn, message)
                elif message.get("method") == "initialized":
                    with self.lock:
                        self.clients.append(conn)
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            with self.lock:
                if conn in self.clients:
                    self.clients.remove(conn)
            conn.close()

    def answer(self, conn, message):
        method, params = message.get("method"), message.get("params") or {}
        with self.lock:
            self.calls.append((method, params.get("useStateDbOnly")))
            if method == "initialize":
                self.capabilities.append(params.get("capabilities"))
                reply = {"result": {"userAgent": "fake"}}
            elif method == "thread/list" and params.get("useStateDbOnly") and self.reject_state_db:
                reply = {"error": {"code": -32602, "message": "unknown field"}}
            elif method == "thread/list":
                reply = {"result": {"data": json.loads(json.dumps(self.threads))}}
            elif method == "thread/loaded/list":
                reply = {"result": {"data": list(self.loaded)}}
            else:
                reply = {"error": {"code": -32601, "message": "not here"}}
        self.send(conn, dict(reply, id=message["id"]))

    @staticmethod
    def send(conn, message):
        data = json.dumps(message).encode()
        header = bytes([0x81, len(data)]) if len(data) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(data))
        conn.sendall(header + data)

    def notify(self, method, params):
        with self.lock:
            clients = list(self.clients)
        for conn in clients:
            try:
                self.send(conn, {"method": method, "params": params})
            except OSError:
                pass

    def count(self, method, state_db="any"):
        """Calls of `method`; `state_db` True or None (argument absent) narrows thread/list."""
        with self.lock:
            return sum(1 for m, flag in self.calls if m == method and (state_db == "any" or flag == state_db))

    def close(self):
        self.closed = True
        try:
            self.listener.close()
        except OSError:
            pass
        with self.lock:
            clients = list(self.clients)
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except OSError:
            pass
        self.acceptor.join(2)
        for worker in self.workers:
            worker.join(2)


class Lines:
    """The watcher's stdout: whole JSON lines, available as they are written."""
    def __init__(self):
        self.buffer, self.condition, self.items = b"", threading.Condition(), []

    def write(self, data):
        with self.condition:
            self.buffer += data
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                self.items.append(json.loads(line))
            self.condition.notify_all()

    def flush(self):
        pass

    def wait(self, predicate, timeout=5.0, start=0):
        """The first line from index `start` matching `predicate`, and its index."""
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for index in range(start, len(self.items)):
                    if predicate(self.items[index]):
                        return self.items[index], index
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("no matching line in %r" % [item.get("type") for item in self.items])
                self.condition.wait(remaining)

    def snapshots(self):
        with self.condition:
            return [item for item in self.items if item.get("type") == "snapshot"]


def thread_row(thread_id, status="idle", name="작업", updated=1791334800):
    return {"id": thread_id, "name": name, "status": {"type": status, "activeFlags": []},
            "createdAt": 1791334000, "updatedAt": updated, "cwd": "/example"}


class WatchTest(unittest.TestCase):
    FAST = {"WATCH_DEBOUNCE": 0.05, "WATCH_CODEX_POLL": 30.0, "WATCH_FULL": 60.0, "WATCH_CLAUDE_STAT": 0.05,
            "WATCH_CLAUDE_GAP": 0.1, "WATCH_CLAUDE_REFRESH": 60.0, "WATCH_TMUX": 60.0, "WATCH_HOMES": 60.0,
            "WATCH_RECONNECT": (0.05, 0.1), "WATCH_READY": 3.0, "WATCH_GAP": 0.05, "WATCH_MINOR_GAP": 0.05,
            "WATCH_HEARTBEAT": 60.0, "WATCH_PING_TIMEOUT": 30.0, "WATCH_LOCK_WAIT": 0.3}

    def setUp(self):
        self.baseline = set(threading.enumerate())
        self.home = Path(tempfile.mkdtemp(prefix="sh", dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.home, True)
        patches = {"HOME": self.home, "ROUTER": self.home / ".codex-router", "WATCH_LOCK_ROOT": self.home}
        patches.update(self.FAST)
        for name, value in patches.items():
            patcher = patch.object(collector, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agents = None
        self.agent_calls = 0
        def agent_rows(errors):
            self.agent_calls += 1
            return json.loads(json.dumps(self.agents)) if self.agents is not None else None
        for name, value in (("claude_agent_rows", agent_rows), ("tmux_sessions", lambda: []),
                            ("load_projects", lambda path=None: [])):
            patcher = patch.object(collector, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.daemons = []
        self.watches = []

    def tearDown(self):
        for watch, writer, runner in self.watches:
            try:
                writer.close()
            except OSError:
                pass
            runner.join(5)
            self.assertFalse(runner.is_alive(), "watch did not end")
        for daemon in self.daemons:
            daemon.close()
        # No watcher, home, input or fake daemon thread outlives its test.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and set(threading.enumerate()) - self.baseline:
            time.sleep(0.02)
        self.assertEqual(set(threading.enumerate()) - self.baseline, set())
        for watch, _, _ in self.watches:
            watch.stdin.close()

    def daemon(self, key=".codex", target="d1", **options):
        """A daemon whose socket is reached through the home's symlink, like Codex's own."""
        socket_path = self.home / key / collector.SOCKET_REL
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        daemon = FakeDaemon(self.home / (target + ".sock"), **options)
        self.daemons.append(daemon)
        if socket_path.is_symlink():
            socket_path.unlink()
        socket_path.symlink_to(daemon.path)
        return daemon

    def start(self, board_id=None):
        reader, writer = os.pipe()
        stdin, writer = os.fdopen(reader, "rb"), os.fdopen(writer, "wb", buffering=0)
        out = Lines()
        watch = collector.Watch(stdin, out, board_id)
        runner = threading.Thread(target=watch.run)
        self.watches.append((watch, writer, runner))
        runner.start()
        return watch, writer, out

    @staticmethod
    def codex(snapshot):
        return [(row["id"], row["phase"], row["title"]) for row in snapshot["data"]["codex"]]

    def test_notification_triggers_one_quick_list_and_no_change_prints_nothing(self):
        daemon = self.daemon(threads=[thread_row("t1")])
        watch, writer, out = self.start()
        first, index = out.wait(lambda m: m.get("type") == "snapshot")
        self.assertEqual(out.items[0]["type"], "hello")
        self.assertEqual((first["seq"], first["reason"]), (1, "initial"))
        self.assertEqual(self.codex(first), [("t1", "idle", "작업")])
        self.assertEqual(set(first["data"]), {"host", "collectedAt", "claude", "codex", "tmux", "errors"})
        caps = daemon.capabilities[0]
        self.assertTrue(caps["experimentalApi"])
        self.assertIn("account/rateLimits/updated", caps["optOutNotificationMethods"])
        lists = daemon.count("thread/list", True)
        # Nothing changes and the backup poll is far away: no second snapshot.
        time.sleep(0.4)
        self.assertEqual(len(out.snapshots()), 1)
        self.assertEqual(daemon.count("thread/list"), lists)
        with daemon.lock:
            daemon.threads = [thread_row("t1", "active")]
        daemon.notify("thread/status/changed", {"threadId": "t1", "status": {"type": "active", "activeFlags": []}})
        daemon.notify("thread/status/changed", {"threadId": "t1", "status": {"type": "active", "activeFlags": []}})
        changed, _ = out.wait(lambda m: m.get("type") == "snapshot" and m["seq"] == 2, timeout=3)
        self.assertEqual(self.codex(changed), [("t1", "working", "작업")])
        self.assertEqual(changed["reason"], "change")
        # Two notifications within the debounce window made one quick (state DB) list.
        self.assertEqual(daemon.count("thread/list", True), lists + 1)
        self.assertEqual(daemon.count("thread/list", None), 0)
        self.assertEqual(watch.homes[".codex"].notices, 2)
        writer.close()
        bye, _ = out.wait(lambda m: m.get("type") == "bye")
        self.assertEqual(bye["reason"], "stdin-closed")

    def test_backup_polls_and_minor_changes_wait_longer(self):
        daemon = self.daemon(threads=[thread_row("t1")])
        with patch.object(collector, "WATCH_CODEX_POLL", 0.1), patch.object(collector, "WATCH_MINOR_GAP", 0.8):
            watch, writer, out = self.start()
            out.wait(lambda m: m.get("type") == "snapshot")
            # A rename without any notification is found by the backup poll.
            with daemon.lock:
                daemon.threads = [thread_row("t1", name="새 이름")]
            renamed, _ = out.wait(lambda m: m.get("type") == "snapshot" and m["seq"] == 2, timeout=3)
            renamed_at = time.monotonic()
            self.assertEqual(self.codex(renamed), [("t1", "idle", "새 이름")])
            # Only a time moved: batched behind the longer gap.
            with daemon.lock:
                daemon.threads = [thread_row("t1", name="새 이름", updated=1791334900)]
            moved, _ = out.wait(lambda m: m.get("type") == "snapshot" and m["seq"] == 3, timeout=3)
            self.assertGreaterEqual(time.monotonic() - renamed_at, 0.7)
            self.assertEqual(moved["data"]["codex"][0]["updatedAt"], 1791334900)
            writer.close()
            out.wait(lambda m: m.get("type") == "bye")

    def test_state_db_rejection_is_asked_once_per_connection(self):
        daemon = self.daemon(threads=[thread_row("t1")], reject_state_db=True)
        with patch.object(collector, "WATCH_CODEX_POLL", 0.05):
            watch, writer, out = self.start()
            first, _ = out.wait(lambda m: m.get("type") == "snapshot")
            self.assertEqual(self.codex(first), [("t1", "idle", "작업")])
            deadline = time.monotonic() + 3
            while daemon.count("thread/list", None) < 4 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(daemon.count("thread/list", None), 4)
            self.assertEqual(daemon.count("thread/list", True), 1)
            writer.close()
            out.wait(lambda m: m.get("type") == "bye")

    def test_a_refused_state_db_list_is_asked_again_after_a_while(self):
        # M6: one refusal no longer means a scan on every list for the whole connection.
        daemon = self.daemon(threads=[thread_row("t1")], reject_state_db=True)
        with patch.object(collector, "WATCH_CODEX_POLL", 0.05), patch.object(collector, "WATCH_QUICK_RETRY", 0.3):
            watch, writer, out = self.start()
            out.wait(lambda m: m.get("type") == "snapshot")
            deadline = time.monotonic() + 3
            while daemon.count("thread/list", True) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(daemon.count("thread/list", True), 2, "asked again after the retry interval")
            # Between the two, every list scanned as before.
            self.assertGreater(daemon.count("thread/list", None), daemon.count("thread/list", True))
            # The daemon answers from its state DB again: quick lists from then on, no more scans.
            with daemon.lock:
                daemon.reject_state_db = False
            quick = daemon.count("thread/list", True)
            deadline = time.monotonic() + 3
            while daemon.count("thread/list", True) < quick + 4 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(daemon.count("thread/list", True), quick + 4)
            scans = daemon.count("thread/list", None)
            time.sleep(0.4)
            self.assertEqual(daemon.count("thread/list", None), scans)
            writer.close()
            out.wait(lambda m: m.get("type") == "bye")

    def test_an_unexpected_failure_ends_the_watch_cleanly_with_bye(self):
        self.agents = []
        with patch.object(collector.Watch, "compose", side_effect=RuntimeError("fixture failure")), \
                patch("sys.stderr", new_callable=io.StringIO) as log:
            watch, writer, out = self.start()
            bye, _ = out.wait(lambda m: m.get("type") == "bye")
            runner = self.watches[-1][2]
            runner.join(3)
        self.assertFalse(runner.is_alive())
        self.assertEqual(bye["reason"], "error")
        self.assertIn("watch watch: RuntimeError: fixture failure", log.getvalue())
        self.assertNotIn("Traceback", log.getvalue())

    def test_a_failure_in_the_local_sources_is_logged_and_read_again_next_round(self):
        self.agents = []
        failures = [RuntimeError("fixture registry"), RuntimeError("fixture registry")]
        real = collector.claude_registry_fingerprint
        def fingerprint():
            if failures:
                raise failures.pop()
            return real()
        with patch.object(collector, "claude_registry_fingerprint", fingerprint), \
                patch("sys.stderr", new_callable=io.StringIO) as log:
            watch, writer, out = self.start()
            first, _ = out.wait(lambda m: m.get("type") == "snapshot")
            self.assertEqual(first["data"]["claude"], [])
            self.assertEqual(first["data"]["errors"], [])
            self.assertTrue(watch.local.is_alive())
            writer.close()
            out.wait(lambda m: m.get("type") == "bye")
        self.assertEqual(log.getvalue().count("watch local: RuntimeError: fixture registry"), 2)

    def test_the_first_snapshot_names_a_source_not_read_yet(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def slow_agents(errors):
            release.wait(5)
            return []
        with patch.object(collector, "claude_agent_rows", slow_agents), patch.object(collector, "WATCH_READY", 0.3):
            watch, writer, out = self.start()
            first, index = out.wait(lambda m: m.get("type") == "snapshot")
            self.assertEqual(first["data"]["claude"], [])
            # tmux is read after Claude on the same thread, so it is still unread too.
            self.assertEqual(first["data"]["errors"], ["Claude 세션 목록을 아직 읽지 못했습니다.",
                                                       "tmux 세션 목록을 아직 읽지 못했습니다."])
            release.set()
            read, _ = out.wait(lambda m: m.get("type") == "snapshot", start=index + 1)
            self.assertEqual(read["data"]["errors"], [])
            writer.close()
            out.wait(lambda m: m.get("type") == "bye")

    def test_reconnects_through_the_symlink_after_a_daemon_restart(self):
        old = self.daemon(threads=[thread_row("t1")])
        watch, writer, out = self.start()
        out.wait(lambda m: m.get("type") == "snapshot")
        # The daemon restarts behind a new socket target with a new thread.
        new = self.daemon(target="d2", threads=[thread_row("t1"), thread_row("t2", "active")])
        old.close()
        changed, index = out.wait(lambda m: m.get("type") == "snapshot" and m["seq"] == 2, timeout=5)
        self.assertEqual(self.codex(changed), [("t1", "idle", "작업"), ("t2", "working", "작업")])
        self.assertEqual(changed["data"]["errors"], [])
        self.assertEqual(new.count("initialize"), 1)
        # A daemon that stays away is reported, as a one-shot collection would.
        new.close()
        gone, _ = out.wait(lambda m: m.get("type") == "snapshot" and m["seq"] == 3, timeout=5)
        self.assertEqual(gone["data"]["codex"], [])
        self.assertEqual(gone["data"]["errors"], ["Codex daemon(기본)에 연결하지 못했습니다."])
        writer.close()
        out.wait(lambda m: m.get("type") == "bye")

    def test_ping_timeout_ends_the_watch_and_pings_keep_it(self):
        self.daemon(threads=[thread_row("t1")])
        with patch.object(collector, "WATCH_PING_TIMEOUT", 0.6):
            watch, writer, out = self.start()
            out.wait(lambda m: m.get("type") == "snapshot")
            for _ in range(6):
                writer.write(b"ping\n")
                time.sleep(0.2)
            self.assertFalse(any(m.get("type") == "bye" for m in out.items))
            bye, _ = out.wait(lambda m: m.get("type") == "bye", timeout=3)
        self.assertEqual(bye["reason"], "ping-timeout")
        runner = self.watches[-1][2]
        runner.join(3)
        self.assertFalse(runner.is_alive())

    def test_refresh_reads_everything_now_with_a_full_codex_listing(self):
        daemon = self.daemon(threads=[thread_row("t1")])
        self.agents = []
        watch, writer, out = self.start()
        out.wait(lambda m: m.get("type") == "snapshot")
        calls = self.agent_calls
        writer.write(b"refresh\n")
        deadline = time.monotonic() + 3
        while (daemon.count("thread/list", None) < 1 or self.agent_calls == calls) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(daemon.count("thread/list", None), 1)
        self.assertGreater(self.agent_calls, calls)
        writer.close()
        out.wait(lambda m: m.get("type") == "bye")

    def test_claude_transcript_append_updates_the_preview_without_the_cli(self):
        session = "c0000000-0000-4000-8000-000000000001"
        transcript = self.home / ".claude/projects/example" / (session + ".jsonl")
        transcript.parent.mkdir(parents=True)
        answer = lambda text: json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"
        transcript.write_text(answer("첫 답"))
        registry = self.home / ".claude/sessions/123.json"
        registry.parent.mkdir(parents=True)
        registry.write_text("{}")
        self.agents = [{"id": "row-1", "sessionId": session, "name": "정산", "state": "working", "status": "busy"}]
        watch, writer, out = self.start()
        first, _ = out.wait(lambda m: m.get("type") == "snapshot")
        self.assertEqual(first["data"]["claude"][0]["snippet"], "첫 답")
        calls = self.agent_calls
        with open(transcript, "a") as stream:
            stream.write(answer("두 번째 답"))
        changed, _ = out.wait(lambda m: m.get("type") == "snapshot" and m["seq"] == 2, timeout=3)
        self.assertEqual(changed["data"]["claude"][0]["snippet"], "두 번째 답")
        self.assertEqual(self.agent_calls, calls)
        # A registry change (a status flip) asks `claude agents` again.
        self.agents[0]["status"] = "idle"
        self.agents[0]["state"] = "done"
        registry.write_text('{"status": "idle"}')
        done, _ = out.wait(lambda m: m.get("type") == "snapshot" and m["seq"] == 3, timeout=3)
        self.assertEqual(done["data"]["claude"][0]["phase"], "done")
        self.assertGreater(self.agent_calls, calls)
        writer.close()
        out.wait(lambda m: m.get("type") == "bye")

    def test_one_watcher_per_board_holds_the_lock(self):
        import fcntl
        folder = self.home / ("sessionholic-watch-%d" % os.getuid())
        folder.mkdir(mode=0o700)
        holder = os.open(str(folder / "0123456789abcdef.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        watch, writer, out = self.start("0123456789abcdef")
        busy, _ = out.wait(lambda m: m.get("type") in ("busy", "snapshot"))
        self.assertEqual(busy["type"], "busy")
        os.close(holder)
        # Once the previous watcher is gone, the next one starts normally.
        watch, writer, out = self.start("0123456789abcdef")
        out.wait(lambda m: m.get("type") == "snapshot")
        self.assertEqual(oct(folder.stat().st_mode & 0o777), oct(0o700))
        writer.close()
        out.wait(lambda m: m.get("type") == "bye")

    def test_watch_rejects_bad_board_ids(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(collector.watch_main(["--board", "../x"]), 2)
            self.assertEqual(collector.watch_main(["extra"]), 2)
        self.assertEqual([json.loads(line)["reason"] for line in out.getvalue().splitlines()], ["usage", "usage"])


if __name__ == "__main__":
    unittest.main()
