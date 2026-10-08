import json
import struct
from pathlib import Path
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
