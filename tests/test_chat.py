import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import time
import unittest
from urllib.parse import quote
from unittest.mock import Mock, patch

import attachments
import chat
import managed_launch
import server
import terminal

SID = '019aaaaa-0000-7000-8000-000000000001'
HOST = {'name':'local','label':'Local','local':True}
REF = {'host':'local','agent':'codex','home':'.codex','id':SID}
CLAUDE_SID = '019bbbbb-0000-7000-8000-000000000002'
CLAUDE_REF = {'host':'local','agent':'claude','home':None,'id':'bg12345678'}
TERMINAL_ID = 'a' * 32
CODEX_TERMINAL_ID = 'c' * 32

# Screens in the shapes Claude Code and Codex draw (wording from their binaries).
CLAUDE_PERMISSION = """\
● 빌드 폴더를 지우겠습니다.

 Bash command

   rm -rf build
   Remove build dir

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in /work
   3. No, and tell Claude what to do differently (esc)
"""
CLAUDE_BOXED = """\
> 빌드 폴더 정리해줘

╭──────────────────────────────────────────────────────────╮
│ Bash command                                             │
│                                                          │
│   rm -rf build                                           │
│   Remove build dir                                       │
│                                                          │
│ Do you want to proceed?                                  │
│ ❯ 1. Yes                                                 │
│   2. Yes, and don't ask again for rm commands in /work   │
│   3. No, and tell Claude what to do differently (esc)    │
╰──────────────────────────────────────────────────────────╯
"""
CLAUDE_QUESTION = """\
● 질문이 있습니다.

 ☐ 방식

어떤 방식으로 할까요?

❯ 1. 옵션 A
     A 설명
  2. 옵션 B
     B 설명
     둘째 줄 설명
  3. Type something.

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
"""
CODEX_APPROVAL = """\
• Ran ls
  └ build

  Would you like to run the following command?

  $ rm -rf build

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for commands that start with `rm` (p)
  3. No, and tell Codex what to do differently (esc)

  Press enter to confirm or esc to cancel
"""
ANSWER_WITH_LIST = """\
● 다음 단계 후보입니다.

  1. 테스트 추가
  2. 문서 정리
  3. 배포 확인

  원하는 번호를 알려 주세요.

────────────────────────────────────────
>
────────────────────────────────────────
  ? for shortcuts
"""
DEMO_MENU = """\
 검증 질문 #1

 계속 진행할까요?

 ❯ 1. Yes
   2. No

 ↑/↓ 이동 · Enter 선택
"""


def setUpModule():
    # A board under test never reads this user's bot sources (nor, through them, a bot ledger).
    import pathlib
    import shutil
    import tempfile
    from unittest import mock
    folder = tempfile.mkdtemp(prefix="sh-config-")
    patcher = mock.patch.object(server, "CONFIG_DIR", pathlib.Path(folder))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    unittest.addModuleCleanup(shutil.rmtree, folder, True)


class MenuParserTest(unittest.TestCase):
    def options(self, menu):
        return [(o['number'], o['label'], o['detail']) for o in menu['options']]

    def test_claude_permission_menu(self):
        menu = chat.parse_menu(CLAUDE_PERMISSION)
        self.assertEqual(menu['question'], '  rm -rf build\n  Remove build dir\n\nDo you want to proceed?')
        self.assertEqual(self.options(menu), [
            (1, 'Yes', ''), (2, "Yes, and don't ask again for rm commands in /work", ''),
            (3, 'No, and tell Claude what to do differently (esc)', '')])
        self.assertEqual(menu['current'], 1)
        self.assertFalse(menu['multi'])
        self.assertEqual(set(menu), {'question', 'options', 'current', 'multi', 'fingerprint'})
        expected = hashlib.sha256(json.dumps(
            [menu['question'], [list(option) for option in self.options(menu)]], ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(menu['fingerprint'], expected)

    def test_box_borders_are_removed_and_the_frame_bounds_the_question(self):
        menu = chat.parse_menu(CLAUDE_BOXED)
        plain = chat.parse_menu(CLAUDE_PERMISSION)
        self.assertEqual(menu['question'], plain['question'])
        self.assertEqual(self.options(menu), self.options(plain))
        self.assertEqual(menu['fingerprint'], plain['fingerprint'])
        self.assertEqual(menu['current'], 1)
        # Only the frame's own rows count: the user's earlier input above it is not the question.
        framed = '> 빌드 폴더 정리해줘\n╭──────────────╮\n│ 계속할까요?  │\n│ ❯ 1. 예      │\n│   2. 아니요  │\n╰──────────────╯\n'
        menu = chat.parse_menu(framed)
        self.assertEqual(menu['question'], '계속할까요?')
        self.assertEqual(self.options(menu), [(1, '예', ''), (2, '아니요', '')])

    def test_claude_question_with_descriptions_and_guidance_below(self):
        menu = chat.parse_menu(CLAUDE_QUESTION)
        # Only the indentation common to every kept row is removed.
        self.assertEqual(menu['question'], ' ☐ 방식\n\n어떤 방식으로 할까요?')
        self.assertEqual(self.options(menu), [(1, '옵션 A', 'A 설명'), (2, '옵션 B', 'B 설명\n둘째 줄 설명'),
                                              (3, 'Type something.', '')])
        self.assertEqual(menu['current'], 1)
        self.assertFalse(menu['multi'])

    def test_codex_approval(self):
        menu = chat.parse_menu(CODEX_APPROVAL)
        self.assertEqual(menu['question'], 'Would you like to run the following command?\n\n$ rm -rf build')
        self.assertEqual([o['label'] for o in menu['options']], [
            'Yes, proceed (y)', "Yes, and don't ask again for commands that start with `rm` (p)",
            'No, and tell Codex what to do differently (esc)'])
        self.assertEqual(menu['current'], 1)

    def test_cursor_position_is_not_part_of_the_fingerprint(self):
        first = chat.parse_menu(CODEX_APPROVAL)
        moved = chat.parse_menu(CODEX_APPROVAL.replace('› 1. Yes', '  1. Yes').replace('  2. Yes', '› 2. Yes'))
        self.assertEqual(moved['current'], 2)
        self.assertEqual(moved['fingerprint'], first['fingerprint'])
        changed = chat.parse_menu(CODEX_APPROVAL.replace('rm -rf build', 'rm -rf dist'))
        self.assertNotEqual(changed['fingerprint'], first['fingerprint'])

    def test_without_exactly_one_cursor_current_is_unknown(self):
        bare = CLAUDE_PERMISSION.replace('❯ 1.', '  1.')
        self.assertIsNone(chat.parse_menu(bare)['current'])
        both = CLAUDE_PERMISSION.replace('   2. Yes,', ' ❯ 2. Yes,')
        self.assertIsNone(chat.parse_menu(both)['current'])

    def test_multi_select_labels(self):
        for mark in ('[ ]', '[x]', '☐', '☒', '◯', '◉'):
            with self.subTest(mark=mark):
                screen = CLAUDE_QUESTION.replace('1. 옵션 A', '1. ' + mark + ' 옵션 A')
                self.assertTrue(chat.parse_menu(screen)['multi'])
        # The "☐ 방식" header above the options is not an option label.
        self.assertFalse(chat.parse_menu(CLAUDE_QUESTION)['multi'])

    def test_answer_lists_and_input_prompts_are_not_menus(self):
        self.assertIsNone(chat.parse_menu(ANSWER_WITH_LIST))
        for prompt in ('> ', '❯ ', '› Ask Codex to do anything'):
            with self.subTest(prompt=prompt):
                screen = '● 후보\n\n  1. 첫째\n  2. 둘째\n\n' + prompt + '\n'
                self.assertIsNone(chat.parse_menu(screen))
        # A cursor-less list followed by more than six lines of answer text.
        far = '  1. 첫째\n  2. 둘째\n' + ''.join('설명 %d\n' % i for i in range(7))
        self.assertIsNone(chat.parse_menu(far))
        for screen in ('', '\n\n', '> ', '❯ 1. Yes\n', '❯ 1. Yes\n  3. No\n', '  2. Yes\n❯ 3. No\n',
                       '❯ 1. Yes\n────\n  2. No\n', '❯ 1. Yes\n설명 없는 줄\n  2. No\n'):
            with self.subTest(screen=screen):
                self.assertIsNone(chat.parse_menu(screen))

    def test_guidance_lines_below_the_menu_are_allowed_within_six_lines(self):
        hints = '\n'.join(['', '안내 1', '안내 2', '안내 3', '안내 4', '안내 5'])
        self.assertIsNotNone(chat.parse_menu(CLAUDE_PERMISSION.rstrip('\n') + '\n' + hints))
        self.assertIsNone(chat.parse_menu(CLAUDE_PERMISSION.rstrip('\n') + '\n' + hints + '\n안내 6'))
        menu = chat.parse_menu(DEMO_MENU)
        self.assertEqual(menu['question'], '검증 질문 #1\n\n계속 진행할까요?')
        self.assertEqual(self.options(menu), [(1, 'Yes', ''), (2, 'No', '')])

    def test_question_is_two_blocks_and_twelve_lines_at_most(self):
        long_block = '\n'.join('줄 %02d' % i for i in range(20))
        screen = '맨 위 블록\n\n' + long_block + '\n\n❯ 1. 예\n  2. 아니요\n'
        question = chat.parse_menu(screen)['question'].split('\n')
        self.assertEqual(question, ['줄 %02d' % i for i in range(8, 20)])
        screen = '셋째\n\n둘째\n\n첫째\n❯ 1. 예\n  2. 아니요\n'
        self.assertEqual(chat.parse_menu(screen)['question'], '둘째\n\n첫째')

    def test_a_screen_rests_only_at_an_empty_prompt_with_no_turn_running(self):
        # As tmux capture-pane -e draws them: Codex's placeholder dim, Claude's grey after its
        # reverse-video cursor cell.
        codex = ('• 수정했습니다.\n\n\x1b[0m› \x1b[2mImplement {feature}\x1b[0m\n\n'
                 '  \x1b[2m100% context left\x1b[0m\n')
        claude = ('● 끝났습니다.\n\n────────────\n❯ \x1b[7mT\x1b[27m\x1b[38;2;153;153;153mry "fix lint errors"\x1b[39m\n'
                  '────────────\n  Opus 5.5 · xhigh\n')
        self.assertTrue(chat.screen_at_rest(codex))
        self.assertTrue(chat.screen_at_rest(claude))
        self.assertTrue(chat.screen_at_rest(codex.replace('\x1b[2m', '\x1b[90m')), 'bright black is grey too')
        self.assertTrue(chat.screen_at_rest(codex.replace('\x1b[2m', '\x1b[38;5;244m')))
        self.assertTrue(chat.screen_at_rest('\x1b[0m› \x1b[0m\n'), 'an empty prompt without a placeholder')
        # Typed and left there: never closed (the draft would be lost).
        self.assertFalse(chat.screen_at_rest(codex.replace('\x1b[2mImplement {feature}', '빌드 고쳐줘')))
        self.assertFalse(chat.screen_at_rest(claude.replace('\x1b[7mT\x1b[27m\x1b[38;2;153;153;153mry "fix lint errors"\x1b[39m',
                                                             '빌드 고쳐줘\x1b[7m \x1b[27m')))
        self.assertFalse(chat.screen_at_rest(codex.replace('\x1b[2m', '\x1b[38;2;230;230;230m')), 'bright text is typed')
        # A turn running, a question or approval, no prompt, nothing on screen.
        self.assertFalse(chat.screen_at_rest('• Working (12s • esc to interrupt)\n\n' + codex))
        self.assertFalse(chat.screen_at_rest('✻ Thinking… (3s · esc to interrupt)\n' + claude))
        self.assertFalse(chat.screen_at_rest('✢ Running… (1m 12s · ↓ 3.4k tokens)\n' + claude), 'a background view')
        self.assertTrue(chat.screen_at_rest('● 토큰 수(tokens)를 셌습니다.\n' + claude), 'a word in the transcript')
        self.assertFalse(chat.screen_at_rest(CLAUDE_PERMISSION))
        self.assertFalse(chat.screen_at_rest('Windows PowerShell\nPS C:\\Users\\example> \n'))
        self.assertFalse(chat.screen_at_rest(''))


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'; self.project.mkdir()
        self.source = {**REF,'cwd':str(self.project),'phase':'idle','title':'Fixture'}
        self.calls = []
        self.board = server.Board([HOST],60,runner=lambda *a:{'codex':[self.source],'claude':[]})
        self.board.poll(HOST)
        class Terms:
            def list(self): return []
        self.flow = server.Workflow(self.board, Terms(), self.root)
        def rpc(host,op,args,timeout):
            self.calls.append((host['name'],op,args))
            if op=='attachment': return attachments.receive(args)
            if args['action']=='read':
                return {'cwd':str(self.project),'phase':'idle','capabilities':{'canSend':True},
                        'messages':[{'role':'assistant','text':'준비 완료'}],
                        'receipts':[{'requestId':args['requestId'],'delivery':'accepted','confirmed':True,'turnId':'t1'}] if args.get('requestId') else []}
            return {'delivery':'accepted','confirmed':True,'acknowledged':True,'turnId':'t1'}
        self.flow.transfers.rpc=rpc
        self.chat=chat.Chat(self.board,self.flow,self.root)
    def tearDown(self): self.temp.cleanup()
    def test_upload_bytes_exact_and_bound_to_session(self):
        raw=b'\x00\xfffile\r\n'
        result=self.chat.upload(REF,'한글 파일.bin',raw)['attachment']
        self.assertEqual(Path(result['path']).read_bytes(),raw)
        self.assertEqual(result['sha256'],hashlib.sha256(raw).hexdigest())
        response=self.chat.send({'source':REF,'requestId':'request-0001','text':'확인','attachments':[result['id']]})
        self.assertEqual(response['status'],'accepted')
        self.assertEqual(response['receipt']['turnId'],'t1')
        self.assertEqual(self.calls[-1][2]['attachmentIds'],[result['id']])
        self.board.state['local']['data']['codex'].append({**self.source,'id':SID[:-1]+'2'})
        with self.assertRaises(ValueError):
            self.chat.send({'source':{**REF,'id':SID[:-1]+'2'},'requestId':'request-0002','text':'x','attachments':[result['id']]})
    def test_client_paths_and_hosts_are_not_destinations(self):
        with self.assertRaises(ValueError): self.chat.upload({**REF,'host':'evil'},'file',b'X')
        result=self.chat.upload({**REF,'cwd':'/evil'},'safe.txt',b'X')['attachment']
        self.assertTrue(result['path'].startswith(str(self.project.resolve())))
    def test_a_slow_codex_send_reports_its_host_request_as_one_step(self):
        import contextlib, io
        ticks = iter(range(1000))
        self.chat.clock = lambda: next(ticks) * 1.5
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.chat.send({'source':REF,'requestId':'request-0001','text':'개인 메시지'})
        lines = [line for line in err.getvalue().splitlines() if ': 느린 전송 ' in line]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(' codex 4.50s prepare=1.50 send=1.50 rest=1.50'), lines[0])
    def test_cwd_drift_blocks_upload(self):
        self.source['cwd']='/changed'
        self.board.poll(HOST)
        with self.assertRaises(ValueError): self.chat.upload(REF,'file',b'X')
    def test_transport_failure_is_unknown_not_retry(self):
        self.flow.transfers.rpc=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('timeout'))
        result=self.chat.send({'source':REF,'requestId':'request-0001','text':'hello'})
        self.assertEqual(result['status'],'unknown')
    def test_helper_refusal_is_a_known_pre_send_rejection(self):
        import transfer
        refusal=transfer.WorkerError('실제 세션의 작업 폴더가 변경되었습니다. 목록을 다시 읽어 주세요.')
        self.flow.transfers.rpc=lambda *a,**k: (_ for _ in ()).throw(refusal)
        with self.assertRaises(chat.SendRejected) as caught:
            self.chat.send({'source':REF,'requestId':'request-0001','text':'hello'})
        self.assertEqual(caught.exception.status,409)
        self.assertIn('작업 폴더가 변경', str(caught.exception))
    def test_attachment_receipt_survives_transport_unknown_without_claiming_delivery(self):
        uploaded=self.chat.upload(REF,'fixture.txt',b'fixture')['attachment']
        self.flow.transfers.rpc=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('timeout'))
        result=self.chat.send({'source':REF,'requestId':'request-0001','text':'hello','attachments':[uploaded['id']]})
        receipt=result['receipt']
        self.assertEqual(receipt['status'],'unknown')
        self.assertFalse(receipt['confirmed'])
        self.assertFalse(receipt['readbackConfirmed'])
        self.assertFalse(receipt['acknowledged'])
        self.assertEqual(receipt['attachments'][0]['name'],'fixture.txt')
        self.assertNotIn('path',receipt['attachments'][0])
        self.assertNotIn('sha256',receipt['attachments'][0])
    def test_receipt_preserves_acknowledged_and_readback_separately(self):
        receipt=self.chat.receipt({'delivery':'accepted','confirmed':True,'acknowledged':True,'readbackConfirmed':False,
                                   'queueId':'legacy-worker-field'},'request-0001')
        self.assertTrue(receipt['acknowledged'])
        self.assertFalse(receipt['readbackConfirmed'])
        self.assertEqual(receipt['status'],'accepted')
        self.assertEqual(set(receipt),{'requestId','status','confirmed','readbackConfirmed','currentSnapshotConfirmed',
                                       'acknowledged','turnId','reason','attachments'})
        self.assertEqual(self.chat.receipt({'delivery':'rejected'},'request-0001')['status'],'failed')
        # A Claude send that attached its board terminal names it, for the screen's terminal list.
        self.assertEqual(self.chat.receipt({'delivery':'accepted','terminalId':TERMINAL_ID},'request-0001')['terminalId'],TERMINAL_ID)
    def test_batch_receipts_use_one_read_and_plain_cache_hits_show_no_receipts(self):
        def rpc(host,op,args,timeout):
            self.calls.append((host['name'],op,args))
            return {'messages':[], 'receipts':[{'requestId':identifier,'delivery':'accepted','confirmed':True,
                    'readbackConfirmed':False,'currentSnapshotConfirmed':False} for identifier in args.get('requestIds',[])]}
        self.flow.transfers.rpc=rpc
        ids=['request-0001','request-0002']
        result=self.chat.state({'source':REF,'requestIds':ids})
        self.assertEqual([r['requestId'] for r in result['receipts']],ids)
        self.assertTrue(all(not r['currentSnapshotConfirmed'] for r in result['receipts']))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][2]['action'],'read')
        # The same read answers a plain view from the cache, without anyone's receipts.
        self.assertEqual(self.chat.state({'source':REF})['receipts'],[])
        self.assertEqual(len(self.calls),1)
        for ids in (['invalid'], ['request-'+str(i) for i in range(21)], None):
            with self.assertRaises(ValueError):self.chat.state({'source':REF,'requestIds':ids})
        self.assertEqual(len(self.calls),1)
    def test_state_accepts_native_ids_without_broadening_web_send_ids(self):
        ids=['native.request:000','n'*128]
        response=self.chat.state({'source':REF,'requestId':ids[0],'requestIds':ids})
        self.assertEqual([r['requestId'] for r in response['receipts']],ids)
        self.assertEqual(self.calls[-1][2]['requestIds'],ids)
        for identifier in ids:
            with self.assertRaises(chat.SendRejected):
                self.chat.send({'source':REF,'requestId':identifier,'text':'fixture'})
        for invalid in ('n'*129,'../outside','/absolute-path'):
            with self.assertRaises(ValueError):
                self.chat.state({'source':REF,'requestIds':[invalid]})
        self.assertEqual(len(self.calls),1)
    def test_state_never_forwards_worker_queue_fields(self):
        # An older worker may still return queue snapshots; the board no longer shows a board-side queue.
        result={'phase':'working','capabilities':{'canSend':True,'canQueue':True,'canSteer':True},
                'messages':[{'role':'user','text':'본문'}],'queue':[{'id':'PRIVATE-QUEUE'}],
                'queuedMessages':[{'queueId':'queue-1','text':'PRIVATE queued body'}],
                'queueState':{'available':True,'complete':True,'total':1,'truncated':False,'reason':'PRIVATE'}}
        self.flow.transfers.rpc=lambda *a,**k:result
        state=self.chat.state({'source':REF,'requestId':'request-0001'})
        self.assertEqual(set(state),{'route','phase','capability','messages','olderOmitted','receipts','maxAttachmentBytes','requiresNativeTerminal',
                                     'prompt','promptOpenable','workingSince','workingSinceApprox','modelSettings'})
        self.assertIs(state['olderOmitted'],False)
        self.assertIsNone(state['prompt'])
        self.assertFalse(state['promptOpenable'])
        self.assertEqual(set(state['capability']),{'supported','attachments','reason'})
        self.assertTrue(state['capability']['supported'])
        self.assertNotIn('PRIVATE',json.dumps(state))
        self.assertNotIn('queueId',state['receipts'][0])
        self.assertFalse(hasattr(chat.Chat,'queue_history'))
    def test_state_passes_the_whole_window_and_whether_older_rows_were_left_out(self):
        rows=[{'role':'tool' if i%4 else 'user','text':'합성 %d'%i,'ts':i} for i in range(300)]
        result={'phase':'idle','capabilities':{'canSend':True},'messages':rows,'olderOmitted':True}
        self.flow.transfers.rpc=lambda *a,**k:result
        state=self.chat.state({'source':REF})
        self.assertEqual(len(state['messages']),300)
        self.assertIs(state['olderOmitted'],True)
        for value in (False,None,'yes',1):
            result['olderOmitted']=value; self.chat.cache.clear()
            self.assertIs(self.chat.state({'source':REF})['olderOmitted'],False)
    def test_state_says_since_when_the_agent_works_only_while_it_works(self):
        result={'phase':'working','capabilities':{'canSend':True},'messages':[],'workingSince':1791334800}
        self.flow.transfers.rpc=lambda *a,**k:result
        def read(**changes):
            result.update(changes); self.chat.cache.clear()
            state=self.chat.state({'source':REF})
            return state['workingSince'],state['workingSinceApprox']
        self.assertEqual(read(),(1791334800,False))
        self.assertEqual(read(workingSinceApprox=True),(1791334800,True))
        self.assertEqual(read(phase='needs_input'),(None,False))
        self.assertEqual(read(phase='working',workingSince=True),(None,False))
        self.assertEqual(read(workingSince='1791334800'),(None,False))
        result.pop('workingSince'); self.assertEqual(read(),(None,False))
    def test_message_attachment_names_are_scrubbed_and_raw_native_fields_stay_metadata(self):
        token='ghp_'+'c'*40
        result={'phase':'idle','capabilities':{'canSend':True},'messages':[{'role':'user','text':'owner@example.com 에게',
                'attachments':[{'source':'native','kind':'image','name':token+' 사진.png','id':None}]}]}
        self.flow.transfers.rpc=lambda *a,**k:result
        state=self.chat.state({'source':REF})
        self.assertEqual(state['messages'][0]['attachments'][0]['name'],'[redacted] 사진.png')
        self.assertNotIn(token,json.dumps(state))
        # An email address is not a secret: it stays readable.
        self.assertEqual(state['messages'][0]['text'],'owner@example.com 에게')
    def test_state_is_exact_cached_and_offline_stops(self):
        first=self.chat.state({'source':REF})
        second=self.chat.state({'source':REF})
        self.assertEqual(first['route'],REF)
        self.assertEqual(len(self.calls),1)
        self.board.state['local']['ok']=False
        # A host that went away: the conversation as last read, read-only, with no remote call.
        self.chat.cache.clear()
        stale=self.chat.state({'source':REF,'requestId':'request-0009'})
        self.assertEqual(len(self.calls),1)
        self.assertEqual((stale['stale'],stale['readOnly'],stale['capability']['supported']),(True,True,False))
        self.assertIn('마지막으로 읽은 대화',stale['capability']['reason'])
        self.assertIn('연결되지 않아',stale['capability']['reason'])
        self.assertEqual(stale['messages'],first['messages'])
        self.assertEqual(stale['receipts'][0]['status'],'unknown')
        # Sending still needs the host.
        with self.assertRaises(RuntimeError): self.chat.source(REF)
    def test_a_read_before_the_first_collection_asks_the_host(self):
        # Right after a restart nothing is collected yet: a read still asks the host.
        self.board.state['local']['ok']=False
        self.assertEqual(self.chat.state({'source':REF})['route'],REF)
        self.assertEqual(len(self.calls),1)
        # It cannot be reached and nothing was read before: the read fails as before.
        self.chat.cache.clear()
        self.chat.last_good.clear()
        self.flow.transfers.rpc=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('fixture: unreachable'))
        with self.assertRaises(RuntimeError): self.chat.state({'source':REF})
    def test_stale_read_is_only_for_an_unreachable_host_and_the_same_session(self):
        from transfer import WorkerError
        working={'cwd':str(self.project),'phase':'working','workingSince':1791334800,'capabilities':{'canSend':True},
                 'messages':[{'role':'assistant','text':'진행 중'}]}
        self.flow.transfers.rpc=lambda *a,**k:working
        first=self.chat.state({'source':REF})
        self.assertEqual(first['phase'],'working')
        fail=lambda exc:(lambda *a,**k:(_ for _ in ()).throw(exc))
        # Unreachable: the last read, with nothing of its work or actions carried over.
        self.chat.cache.clear()
        self.flow.transfers.rpc=fail(RuntimeError('fixture: unreachable'))
        stale=self.chat.state({'source':REF})
        self.assertEqual((stale['stale'],stale['phase'],stale['workingSince'],stale['recovery']),(True,'unavailable',None,None))
        self.assertNotIn('action',stale['capability'])
        self.assertEqual(stale['messages'],first['messages'])
        # Reachable but late (its helper's own deadline): the last read too, saying it was late.
        from transfer_native import DEADLINE_EXCEEDED
        self.chat.cache.clear()
        self.flow.transfers.rpc=fail(WorkerError(DEADLINE_EXCEEDED))
        late=self.chat.state({'source':REF})
        self.assertEqual((late['stale'],late['messages']),(True,first['messages']))
        self.assertIn('제때 답하지 않아',late['capability']['reason'])
        # A failure the host reported is shown as it is.
        self.chat.cache.clear()
        self.flow.transfers.rpc=fail(WorkerError('fixture: reported'))
        with self.assertRaises(WorkerError): self.chat.state({'source':REF})
        # The same id in another folder is another conversation: it has no last read.
        self.chat.cache.clear()
        self.flow.transfers.rpc=fail(RuntimeError('fixture: unreachable'))
        moved=self.root/'moved'; moved.mkdir()
        self.source['cwd']=str(moved); self.board.poll(HOST)
        with self.assertRaises(RuntimeError) as caught: self.chat.state({'source':REF})
        self.assertEqual(str(caught.exception),'fixture: unreachable')
    def test_receipt_lookup_answers_plain_reads_from_cache_without_receipts(self):
        receipt=self.chat.state({'source':REF,'requestId':'request-0001'})
        self.assertEqual(receipt['receipts'][0]['requestId'],'request-0001')
        plain=self.chat.state({'source':REF})
        self.assertEqual(plain['receipts'],[])
        self.assertEqual(plain['messages'],receipt['messages'])
        # The cached view itself keeps its receipt for the next request that asks for it.
        self.assertEqual(self.chat.state({'source':REF,'requestId':'request-0001'})['receipts'],receipt['receipts'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][2]['requestId'],'request-0001')
        self.chat.cache.clear()
        self.chat.state({'source':REF})
        self.assertEqual([args['action'] for _,_,args in self.calls],['read','read'])
        self.assertNotIn('requestId',self.calls[1][2])
    def test_combined_missing_or_other_request_receipt_remains_unknown(self):
        for result in ({'messages':[]}, {'messages':[],'receipts':[{'requestId':'different-request','delivery':'completed','confirmed':True}]}):
            with self.subTest(result=result):
                calls=[]
                self.chat.cache.clear()
                self.flow.transfers.rpc=lambda *a,**k: calls.append((a,k)) or result
                response=self.chat.state({'source':REF,'requestId':'request-0001'})
                self.assertEqual(response['receipts'][0]['status'],'unknown')
                self.assertFalse(response['receipts'][0]['confirmed'])
                self.assertEqual(len(calls),1)
    def receipt_rpc(self):
        """Codex reads that answer every asked id, numbering the reads."""
        def rpc(host,op,args,timeout):
            self.calls.append((host['name'],op,args))
            if args.get('action')!='read':
                return {'delivery':'accepted','confirmed':True,'acknowledged':True,'turnId':'t1'}
            ids=([args['requestId']] if args.get('requestId') else [])+args.get('requestIds',[])
            return {'cwd':str(self.project),'phase':'idle','capabilities':{'canSend':True},
                    'messages':[{'role':'assistant','text':'read %d'%len(self.calls)}],
                    'receipts':[{'requestId':i,'delivery':'accepted','confirmed':True} for i in dict.fromkeys(ids)]}
        self.flow.transfers.rpc=rpc

    def test_watcher_read_carries_the_receipts_clients_wait_for(self):
        self.receipt_rpc()
        key=chat.Chat.state_key(self.source)
        import events
        self.assertEqual(events.watch_key(chat.Chat.identity(self.source)),key)
        self.chat.state({'source':REF,'requestId':'request-000a','requestIds':['request-000a']})
        self.chat.state({'source':REF,'requestIds':['request-000b']})
        self.assertEqual(self.chat._waiting_ids(key,time.monotonic()),['request-000a','request-000b'])
        self.chat.cache.clear()
        watched=self.chat.state({'source':REF},watching=True)
        # One remote read asks for every waiting id; no single requestId is sent for the watcher.
        self.assertEqual(self.calls[-1][2]['requestIds'],['request-000a','request-000b'])
        self.assertNotIn('requestId',self.calls[-1][2])
        self.assertEqual([r['requestId'] for r in watched['receipts']],['request-000a','request-000b'])
        reads=len(self.calls)
        # A subset is answered from the cache: only the asked receipts, in the asked order.
        both=self.chat.state({'source':REF,'requestId':'request-000b','requestIds':['request-000b','request-000a']})
        self.assertEqual([r['requestId'] for r in both['receipts']],['request-000b','request-000a'])
        self.assertTrue(all(r['status']=='accepted' for r in both['receipts']))
        one=self.chat.state({'source':REF,'requestIds':['request-000a']})
        self.assertEqual([r['requestId'] for r in one['receipts']],['request-000a'])
        self.assertEqual(self.chat.state({'source':REF})['receipts'],[])
        self.assertEqual(one['messages'],watched['messages'])
        self.assertEqual([r['requestId'] for r in watched['receipts']],['request-000a','request-000b'])
        self.assertEqual(len(self.calls),reads)
        # The watcher's own reads never keep an id waiting.
        expiry=dict(self.chat.waiting[key])
        self.chat.state({'source':REF},watching=True)
        self.assertEqual(self.chat.waiting[key],expiry)
        self.assertEqual(len(self.calls),reads)
        # An id outside the cached set is a new remote read, which replaces the cache. It also
        # carries the ids still waited on (asked ids first), so the watcher and other clients share it.
        fresh=self.chat.state({'source':REF,'requestIds':['request-000c']})
        self.assertEqual(len(self.calls),reads+1)
        self.assertEqual(self.calls[-1][2]['requestIds'],['request-000c','request-000b','request-000a'])
        self.assertEqual([r['requestId'] for r in fresh['receipts']],['request-000c'])
        again=self.chat.state({'source':REF,'requestIds':['request-000a']})
        self.assertEqual([r['requestId'] for r in again['receipts']],['request-000a'])
        self.assertEqual(len(self.calls),reads+1)
        # A stale cache is never used, even for a covered subset.
        cached=self.chat.cache[key]
        self.chat.cache[key]=(cached[0]-chat.STATE_CACHE_SECONDS,)+cached[1:]
        self.chat.state({'source':REF,'requestIds':['request-000a']})
        self.assertEqual(len(self.calls),reads+2)

    def test_clients_share_a_read_for_three_seconds_while_the_watcher_reads_again_after_two(self):
        key=chat.Chat.state_key(self.source)
        def age(seconds):
            cached=self.chat.cache[key]
            self.chat.cache[key]=(cached[0]-seconds,)+cached[1:]
        self.chat.state({'source':REF},watching=True)
        reads=len(self.calls)
        age(2.5)
        # A client polling every 3 s takes the watcher's newest read.
        self.chat.state({'source':REF})
        self.assertEqual(len(self.calls),reads)
        # The watcher's own next read (2 s after its last ended) asks the host again.
        self.chat.state({'source':REF},watching=True)
        self.assertEqual(len(self.calls),reads+1)
        age(3.01)
        self.chat.state({'source':REF})
        self.assertEqual(len(self.calls),reads+2)

    def test_waiting_ids_expire_are_bounded_and_only_valid(self):
        key=chat.Chat.state_key(self.source)
        with patch.object(chat,'WAITING_SECONDS',-1):
            self.chat.state({'source':REF,'requestIds':['request-old1']})
        self.assertEqual(self.chat._waiting_ids(key,time.monotonic()),[])
        self.assertNotIn(key,self.chat.waiting)
        for invalid in (['../outside'],['request-'+str(i) for i in range(21)]):
            with self.assertRaises(ValueError):self.chat.state({'source':REF,'requestIds':invalid})
        self.assertNotIn(key,self.chat.waiting)
        ids=['request-%04d'%i for i in range(25)]
        self.chat.state({'source':REF,'requestIds':ids[:20]})
        self.chat.state({'source':REF,'requestIds':ids[20:]})
        # At most the 20 most recently asked ids are kept for one conversation.
        self.assertEqual(self.chat._waiting_ids(key,time.monotonic()),ids[5:])
        self.chat.state({'source':REF,'requestIds':[ids[5]]})
        self.assertEqual(self.chat._waiting_ids(key,time.monotonic())[-1],ids[5])
        self.chat.cache.clear()
        self.chat.state({'source':REF},watching=True)
        self.assertEqual(len(self.calls[-1][2]['requestIds']),20)

    def test_watcher_read_right_after_a_send_answers_the_receipt_read(self):
        self.receipt_rpc()
        self.chat.state({'source':REF})
        self.chat.send({'source':REF,'requestId':'request-0001','text':'hello'})
        sends=len(self.calls)
        # The send dropped the cached view; the watcher's next read carries the new receipt.
        self.chat.state({'source':REF},watching=True)
        self.assertEqual(self.calls[-1][2]['requestIds'],['request-0001'])
        receipt=self.chat.state({'source':REF,'requestId':'request-0001','requestIds':['request-0001']})
        self.assertEqual(receipt['receipts'][0]['requestId'],'request-0001')
        self.assertEqual(receipt['receipts'][0]['status'],'accepted')
        self.assertEqual(len(self.calls),sends+1)
        # Send ids the state route would refuse are never waited on.
        self.chat.send({'source':REF,'requestId':'_request-0002','text':'hello'})
        self.assertNotIn('_request-0002',self.chat.waiting[chat.Chat.state_key(self.source)])

    def test_cwd_change_bypasses_cached_state(self):
        self.chat.state({'source':REF})
        changed=self.root/'changed';changed.mkdir()
        self.source['cwd']=str(changed)
        self.board.poll(HOST)
        self.chat.state({'source':REF})
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[-1][2]['source']['cwd'],str(changed))
    def test_read_started_before_send_cannot_repopulate_cache(self):
        started,release=threading.Event(),threading.Event()
        original=self.flow.transfers.rpc
        results=[]
        def rpc(host,op,args,timeout):
            if args.get('action')=='read' and not started.is_set():
                started.set()
                if not release.wait(3): raise RuntimeError('fixture read did not release')
                return {'phase':'idle','messages':[{'role':'assistant','text':'before-send'}]}
            result=original(host,op,args,timeout)
            if args.get('action')=='read':
                result['messages']=[{'role':'assistant','text':'after-send'}]
            return result
        self.flow.transfers.rpc=rpc
        worker=threading.Thread(target=lambda:results.append(self.chat.state({'source':REF})))
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            self.chat.send({'source':REF,'requestId':'request-0001','text':'hello'})
        finally:
            release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['messages'][0]['text'],'before-send')
        fresh=self.chat.state({'source':REF})
        self.assertEqual(fresh['messages'][0]['text'],'after-send')
    def test_older_overlapping_read_cannot_replace_newer_cache(self):
        started,release=threading.Event(),threading.Event()
        results=[]
        def rpc(host,op,args,timeout):
            if not started.is_set():
                started.set()
                if not release.wait(3): raise RuntimeError('fixture read did not release')
                text='older'
            else:
                text='newer'
            return {'phase':'idle','messages':[{'role':'assistant','text':text}]}
        self.flow.transfers.rpc=rpc
        worker=threading.Thread(target=lambda:results.append(self.chat.state({'source':REF})))
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            # Asks for a receipt the first read does not carry, so it reads on its own.
            self.assertEqual(self.chat.state({'source':REF,'requestId':'request-0009'})['messages'][0]['text'],'newer')
        finally:
            release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]['messages'][0]['text'],'older')
        self.assertEqual(self.chat.state({'source':REF})['messages'][0]['text'],'newer')
    def test_native_terminal_identity_supports_new_destination(self):
        ref={**REF,'id':SID[:-1]+'3'}
        self.flow.terminals.list=lambda:[{'nativeSource':{**ref,'cwd':str(self.project)}}]
        self.assertEqual(self.chat.state({'source':ref})['route'],ref)
        with self.assertRaises(ValueError): self.chat.state({'source':{**ref,'home':'.codex-other'}})
    def test_file_count_text_size_and_missing_attachment(self):
        for fields in ({'attachments':['a'*32]*9},{'text':'x'*32769},{'attachments':['b'*32]}):
            with self.assertRaises(ValueError):
                self.chat.send({'source':REF,'requestId':'request-0001','text':'ok',**fields})

    def codex_terminal(self, metadata=False, **native):
        terms = FakeTerminals()
        source = {**REF, 'cwd': str(self.project), **native}
        row = {'id': CODEX_TERMINAL_ID, 'alive': True, 'closed': False}
        row.update({'metadata': {'nativeSource': source}} if metadata else {'nativeSource': source})
        terms.rows.append(row)
        terms.screen = CODEX_APPROVAL
        self.flow.terminals = terms
        self.chat.cache.clear()
        return terms

    def test_codex_board_terminal_matches_exact_id_and_home(self):
        for metadata in (False, True):
            with self.subTest(metadata=metadata):
                self.codex_terminal(metadata)
                state = self.chat.state({'source': REF})
                self.assertEqual(state['prompt']['agent'], 'codex')
                self.assertEqual(state['prompt']['terminalId'], CODEX_TERMINAL_ID)
                self.assertEqual(state['prompt']['current'], 1)
                self.assertFalse(state['promptOpenable'])
        for native in ({'home': '.codex-other'}, {'home': None}, {'id': SID[:-1] + '9'}, {'host': 'other'},
                       {'agent': 'claude'}):
            with self.subTest(native=native):
                terms = self.codex_terminal(**native)
                state = self.chat.state({'source': REF})
                self.assertIsNone(state['prompt'])
                self.assertFalse(state['promptOpenable'])
                self.assertNotIn('capture', terms.log)
        for change in ({'alive': False}, {'closed': True}):
            with self.subTest(change=change):
                terms = self.codex_terminal()
                terms.rows[0].update(change)
                self.assertIsNone(self.chat.state({'source': REF})['prompt'])
                self.assertNotIn('capture', terms.log)
        same = chat.Chat._same_native
        codex = {'host': 'local', 'agent': 'codex', 'id': SID}
        self.assertTrue(same({**codex, 'home': None}, codex))
        self.assertTrue(same({**codex, 'home': ''}, {**codex, 'home': None}))
        self.assertFalse(same({**codex, 'id': None}, {**codex, 'id': None}))
        self.assertFalse(same({**codex, 'sessionId': SID, 'id': 'other'}, codex))
        # A failing terminal list never breaks the Codex chat view.
        terms = self.codex_terminal()
        terms.list = lambda: (_ for _ in ()).throw(RuntimeError('fixture'))
        state = self.chat.state({'source': REF})
        self.assertIsNone(state['prompt'])
        self.assertTrue(state['capability']['supported'])

    def test_codex_menu_is_answered_through_its_board_terminal(self):
        terms = self.codex_terminal()
        fingerprint = chat.parse_menu(CODEX_APPROVAL)['fingerprint']
        self.assertEqual(self.chat.prompt({'source': REF, 'action': 'open'})['prompt']['fingerprint'], fingerprint)
        terms.screens = [CODEX_APPROVAL, '› Ask Codex to do anything\n']
        with patch.object(chat.time, 'sleep'):
            result = self.chat.prompt({'source': REF, 'action': 'answer', 'fingerprint': fingerprint,
                                       'number': 2, 'requestId': 'answer-0001'})
        self.assertEqual(result['status'], 'answered')
        self.assertIsNone(result['prompt'])
        self.assertEqual(terms.keys, [(CODEX_TERMINAL_ID, ['Down', 'Enter'])])
        # The host chat RPC was never asked to send anything.
        self.assertNotIn('send', [args.get('action') for _, _, args in self.calls])
        self.flow.terminals = FakeTerminals()
        with self.assertRaises(chat.PromptRejected) as caught:
            self.chat.prompt({'source': REF, 'action': 'open'})
        self.assertEqual(caught.exception.code, 'terminal_required')


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('condition was not reached')


class StateShareTest(unittest.TestCase):
    """One remote conversation read at a time: a reader asking for a subset of the receipts
    of a read in progress, started after the latest send, waits for it instead of reading."""
    setUp = ChatTest.setUp
    tearDown = ChatTest.tearDown
    receipt_rpc = ChatTest.receipt_rpc
    RID = 'request-0001'

    def gated_reads(self, failure=None):
        """Codex reads that wait at a gate; the first one may fail with `failure`."""
        gate, self.started = threading.Event(), []
        self.addCleanup(gate.set)
        self.receipt_rpc()
        answer = self.flow.transfers.rpc
        def rpc(host, op, args, timeout):
            if args.get('action') == 'read':
                self.started.append(args)
                if not gate.wait(5):
                    raise RuntimeError('fixture gate was not opened')
                if failure is not None and len(self.started) == 1:
                    raise failure
            return answer(host, op, args, timeout)
        self.flow.transfers.rpc = rpc
        self.key = chat.Chat.state_key(self.source)
        return gate

    def run_reads(self, *reads):
        """Start each read in its own thread once the previous one has started or joined a read."""
        results, threads = {}, []
        def read(name, data, watching):
            try:
                results[name] = self.chat.state(data, watching=watching)
            except Exception as exc:
                results[name] = exc
        for name, data, watching, ready in reads:
            thread = threading.Thread(target=read, args=(name, data, watching), daemon=True)
            thread.start()
            threads.append(thread)
            wait_for(ready)
        return results, threads

    def joined(self, count):
        return lambda: self.key in self.chat.reads and self.chat.reads[self.key].joined == count

    def finish(self, gate, threads):
        gate.set()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(self.chat.reads, {})

    def share_after_send(self, leader):
        gate = self.gated_reads()
        self.chat.send({'source': REF, 'requestId': self.RID, 'text': 'hello'})
        browser = ('browser', {'source': REF, 'requestId': self.RID, 'requestIds': [self.RID]}, False)
        watcher = ('watcher', {'source': REF}, True)
        first, second = (watcher, browser) if leader == 'watcher' else (browser, watcher)
        results, threads = self.run_reads(first + (lambda: len(self.started) == 1,),
                                          second + (self.joined(1),))
        self.finish(gate, threads)
        # One remote read for the receipt both asked for right after the send.
        self.assertEqual(len(self.started), 1)
        self.assertIn(self.RID, [self.started[0].get('requestId')] + self.started[0].get('requestIds', []))
        self.assertEqual([r['requestId'] for r in results['browser']['receipts']], [self.RID])
        self.assertEqual(results['browser']['receipts'][0]['status'], 'accepted')
        self.assertEqual([r['requestId'] for r in results['watcher']['receipts']], [self.RID])
        self.assertEqual(results['watcher']['messages'], results['browser']['messages'])
        # The shared read is the cached view for the next reader.
        self.assertEqual(self.chat.state({'source': REF, 'requestId': self.RID})['receipts'][0]['status'], 'accepted')
        self.assertEqual(len(self.started), 1)

    def test_watcher_read_right_after_a_send_is_shared_by_the_browser_read(self):
        self.share_after_send('watcher')

    def test_browser_read_right_after_a_send_is_shared_by_the_watcher_read(self):
        self.share_after_send('browser')

    def test_a_read_started_before_a_send_is_not_shared(self):
        gate = self.gated_reads()
        results, threads = self.run_reads(('before', {'source': REF}, False, lambda: len(self.started) == 1))
        self.chat.send({'source': REF, 'requestId': self.RID, 'text': 'hello'})
        more, others = self.run_reads(('after', {'source': REF, 'requestId': self.RID}, False,
                                       lambda: len(self.started) == 2))
        self.finish(gate, threads + others)
        self.assertNotIn('requestIds', self.started[0])
        self.assertEqual(self.started[1]['requestId'], self.RID)
        self.assertEqual([r['requestId'] for r in more['after']['receipts']], [self.RID])
        self.assertEqual(results['before']['receipts'], [])
        # Only the read started after the send is cached.
        self.assertIn(self.RID, self.chat.cache[self.key][4])

    def test_a_read_missing_an_asked_id_starts_one_carrying_every_waiting_id(self):
        gate = self.gated_reads()
        results, threads = self.run_reads(
            ('a', {'source': REF, 'requestIds': ['request-000a']}, False, lambda: len(self.started) == 1),
            ('b', {'source': REF, 'requestIds': ['request-000b']}, False, lambda: len(self.started) == 2),
            # Waits on both ids: the newer read carries them and is shared.
            ('watcher', {'source': REF}, True, self.joined(1)))
        self.finish(gate, threads)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.started[1]['requestIds'], ['request-000b', 'request-000a'])
        self.assertEqual([r['requestId'] for r in results['b']['receipts']], ['request-000b'])
        self.assertEqual([r['requestId'] for r in results['watcher']['receipts']], ['request-000a', 'request-000b'])
        self.assertTrue(all(r['status'] == 'accepted' for r in results['watcher']['receipts']))

    def test_a_failed_read_fails_the_readers_waiting_for_it_with_the_same_error(self):
        failure = RuntimeError('기기 연결을 다시 확인한 뒤 메시지를 보내 주세요.')
        gate = self.gated_reads(failure)
        results, threads = self.run_reads(('leader', {'source': REF}, False, lambda: len(self.started) == 1),
                                          ('follower', {'source': REF}, True, self.joined(1)))
        self.finish(gate, threads)
        self.assertIs(results['leader'], failure)
        self.assertIs(results['follower'], failure)
        self.assertEqual(len(self.started), 1)
        # Nothing was cached or left registered: the next read asks the host again.
        self.assertEqual(self.chat.state({'source': REF})['phase'], 'idle')
        self.assertEqual(len(self.started), 2)

    def test_a_waiting_reader_gives_up_after_its_own_timeout(self):
        gate = self.gated_reads()
        results, threads = self.run_reads(('leader', {'source': REF}, False, lambda: len(self.started) == 1))
        with patch.object(chat, 'STATE_SHARE_SECONDS', 0.05):
            with self.assertRaises(RuntimeError) as caught:
                self.chat.state({'source': REF}, watching=True)
        self.assertEqual(str(caught.exception), chat.STATE_UNAVAILABLE)
        self.finish(gate, threads)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(results['leader']['phase'], 'idle')


class ChatHttpTest(ChatTest):
    def test_command_route_requires_auth_csrf_origin_and_preserves_authoritative_source(self):
        calls = []
        self.chat.command = lambda data: calls.append(data) or {'status': 'configured'}
        def request(headers, body):
            client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
            client.request('POST', '/api/chat/command', json.dumps(body), {'Content-Type': 'application/json', **headers})
            response = client.getresponse(); response.read(); client.close(); return response.status
        payload = {'source': REF, 'command': 'effort', 'value': 'high', 'requestId': 'control-0001'}
        self.assertEqual(request({}, payload), 401)
        auth = {'Authorization': 'Bearer synthetic'}
        self.assertEqual(request(auth, payload), 403)
        auth['X-CSRF-Token'] = self.httpd.csrf
        self.assertEqual(request({**auth, 'Origin': 'https://evil.example'}, payload), 403)
        self.assertEqual(calls, [])
        self.assertEqual(request(auth, payload), 200)
        self.assertEqual(calls, [payload])
    def setUp(self):
        super().setUp()
        self.httpd=server.make_server('127.0.0.1',0,self.board,'synthetic',terminals=self.flow.terminals,
                                      workflow=self.flow,state_dir=self.root)
        self.httpd.chat=self.chat
        threading.Thread(target=self.httpd.serve_forever,daemon=True).start()
        self.port=self.httpd.server_address[1]
    def tearDown(self):
        self.httpd.shutdown(); self.httpd.server_close();super().tearDown()
    def upload_request(self,headers,content=b'File'):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        c.request('POST','/api/chat/upload',content,{'Content-Type':'application/octet-stream',
            'X-Chat-Source':quote(json.dumps(REF)), 'X-File-Name':quote('메모.txt'),**headers})
        r=c.getresponse(); body=r.read();c.close();return r.status,body
    def test_binary_endpoint_auth_csrf_origin_and_readback(self):
        self.assertEqual(self.upload_request({})[0],401)
        auth={'Authorization':'Bearer synthetic'}
        self.assertEqual(self.upload_request(auth)[0],403)
        auth['X-CSRF-Token']=self.httpd.csrf
        self.assertEqual(self.upload_request({**auth,'Origin':'https://evil.example'})[0],403)
        code,data=self.upload_request(auth,b'\x00\xffnative file')
        self.assertEqual(code,200)
        self.assertEqual(Path(json.loads(data)['attachment']['path']).read_bytes(),b'\x00\xffnative file')
    def test_binary_endpoint_caps_length_before_read(self):
        auth={'Authorization':'Bearer synthetic','X-CSRF-Token':self.httpd.csrf,
              'Content-Length':str(chat.MAX_FILE+1)}
        self.assertEqual(self.upload_request(auth,b'')[0],400)
    def post(self, path, body):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        c.request('POST',path,json.dumps(body),{'Content-Type':'application/json','Authorization':'Bearer synthetic',
                                                 'X-CSRF-Token':self.httpd.csrf})
        r=c.getresponse(); data=json.loads(r.read()); c.close(); return r.status,data
    def test_prompt_route_answers_and_reports_refusals_with_a_code(self):
        terms=self.codex_terminal()
        fingerprint=chat.parse_menu(CODEX_APPROVAL)['fingerprint']
        answer={'source':REF,'action':'answer','fingerprint':fingerprint,'number':1}
        status,data=self.post('/api/chat/state',{'source':REF})
        self.assertEqual(status,200)
        self.assertEqual(data['prompt']['fingerprint'],fingerprint)
        status,data=self.post('/api/chat/prompt',{**answer,'fingerprint':'f'*64,'requestId':'answer-0001'})
        self.assertEqual((status,data['code']),(409,'prompt_changed'))
        self.assertEqual(data['error'],chat.PROMPT_CHANGED)
        status,data=self.post('/api/chat/prompt',{**answer,'number':True,'requestId':'answer-0001'})
        self.assertEqual(status,400)
        self.assertNotIn('code',data)
        self.assertEqual(terms.keys,[])
        terms.screens=[CODEX_APPROVAL,'']
        with patch.object(chat.time,'sleep'):
            status,data=self.post('/api/chat/prompt',{**answer,'requestId':'answer-0002'})
        self.assertEqual(status,200)
        self.assertEqual((data['status'],data['prompt']),('answered',None))
        self.assertEqual(terms.keys,[(CODEX_TERMINAL_ID,['Enter'])])
        # The terminal scroll route returns the scroll mode instead of a bare ok.
        self.httpd.terminals=terms
        status,data=self.post('/api/terminal/'+CODEX_TERMINAL_ID+'/scroll',{'lines':-3})
        self.assertEqual((status,data),(200,{'ok':True,'mode':'app','position':None}))


class FakeTerminals:
    """Board terminal manager boundary: no tmux, no CLI, only recorded calls."""
    def __init__(self):
        self.rows, self.pastes, self.log, self.keys = [], [], [], []
        self.paste_error = self.keys_error = self.capture_error = None
        self.chunks = [{'after': 0, 'alive': True}]
        self.screen = '> '
        self.screens = []  # captured in order first, then `screen`
    def list(self):
        return [json.loads(json.dumps(row)) for row in self.rows]
    def paste(self, terminal_id, text, submit=True):
        self.log.append('paste')
        self.pastes.append((terminal_id, text, submit))
        if self.paste_error:
            raise self.paste_error
        return {'ok': True}
    def capture(self, terminal_id):
        self.log.append('capture')
        if self.capture_error:
            raise self.capture_error
        return self.screens.pop(0) if self.screens else self.screen
    def send_keys(self, terminal_id, keys):
        self.log.append('keys')
        if self.keys_error:
            raise self.keys_error
        self.keys.append((terminal_id, list(keys)))
        return {'ok': True}
    def scroll(self, terminal_id, lines):
        return {'ok': True, 'mode': 'app', 'position': None}
    def read(self, terminal_id, after=0, wait=20, epoch=None):
        self.log.append('read')
        time.sleep(min(wait, 0.02))
        return dict(self.chunks.pop(0) if len(self.chunks) > 1 else self.chunks[0])


class ChatClaudeTest(unittest.TestCase):
    """Claude input is typed by the board into its own attached terminal, never by the host RPC."""
    def setUp(self):
        # The check for a message left in the input box waits no real time here.
        submit = patch.object(chat, 'SUBMIT_CHECK_SECONDS', 0); submit.start(); self.addCleanup(submit.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'; self.project.mkdir()
        # Same shape as collector.claude_row: Claude rows carry no profile home.
        self.row = {'agent': 'claude', 'id': CLAUDE_REF['id'], 'sessionId': CLAUDE_SID, 'kind': 'background',
                    'cwd': str(self.project), 'phase': 'idle', 'title': 'Claude fixture'}
        self.board = server.Board([HOST], 60, runner=lambda *a: {'codex': [], 'claude': [self.row]})
        self.board.poll(HOST)
        self.terms = FakeTerminals()
        self.flow = server.Workflow(self.board, self.terms, self.root)
        self.phase, self.messages, self.calls, self.model_settings = 'idle', [], [], None
        def rpc(host, op, args, timeout):
            self.calls.append((op, args.get('action')))
            if op == 'attachment':
                return attachments.receive(args)
            if args['action'] == 'send':
                raise AssertionError('Claude input must never use the host send RPC')
            if args['action'] == 'capabilities':
                return {'phase': self.phase, 'kind': self.row['kind'], 'cwd': str(self.project)}
            return {'phase': self.phase, 'cwd': str(self.project), 'messages': list(self.messages),
                    'requiresNativeTerminal': True, **({'modelSettings': dict(self.model_settings)} if self.model_settings else {}),
                    'capabilities': {'canSend': False, 'reason': 'Claude 입력은 보드가 연결한 터미널로 전달합니다.'}}
        self.flow.transfers.rpc = rpc
        self.chat = chat.Chat(self.board, self.flow, self.root)

    def kind(self, value):
        self.row['kind'] = value
        self.board.poll(HOST)

    def terminal(self, **native):
        row = {'id': TERMINAL_ID, 'alive': True, 'closed': False,
               'nativeSource': {'host': 'local', 'agent': 'claude', 'id': CLAUDE_REF['id'], 'home': '.claude',
                                'cwd': str(self.project), **native}}
        self.terms.rows.append(row)
        return row

    def records(self):
        folder = self.root / 'chat' / 'claude-sends'
        return sorted(folder.glob('*.json')) if folder.exists() else []

    def control(self, command, value=None, request='control-0001'):
        return self.chat.command({'source': CLAUDE_REF, 'command': command, 'value': value, 'requestId': request})

    def test_only_a_conversation_read_from_a_live_host_lets_the_host_skip_claude_agents(self):
        seen, original = [], self.flow.transfers.rpc
        def rpc(host, op, args, timeout):
            if op == 'chat':
                seen.append((args['action'], args.get('liveRow')))
            return original(host, op, args, timeout)
        self.flow.transfers.rpc = rpc
        self.chat.state({'source': CLAUDE_REF})
        # The host's resident watch delivers its rows: the read says so.
        self.board.state['local']['live'] = True
        self.chat._forget_state(CLAUDE_REF)
        self.chat.state({'source': CLAUDE_REF})
        # Upload's check of the real folder never takes the listed row.
        self.chat.upload(CLAUDE_REF, 'note.txt', b'fixture')
        self.assertEqual(seen, [('read', None), ('read', True), ('read', None)])

    def test_model_command_uses_one_native_slash_without_prompt_send_or_automatic_enter(self):
        self.terminal()
        ready = '──────────\n❯ \n──────────\n  Sonnet · medium effort\n'
        self.terms.screens = [ready, 'Set model to Opus\n' + ready.replace('Sonnet', 'Opus')]
        result = self.control('model', 'opus')
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['current'], {'model': 'opus'})
        self.assertEqual(self.terms.pastes, [(TERMINAL_ID, '/model opus', True)])
        self.assertEqual(self.terms.keys, [])
        self.assertEqual(self.control('model', 'opus'), result)
        self.assertEqual(len(self.terms.pastes), 1)
        with self.assertRaisesRegex(ValueError, '같은'): self.control('model', 'sonnet')

    def test_commands_refuse_menus_existing_input_busy_and_invalid_values(self):
        self.terminal()
        for screen in [CLAUDE_PERMISSION, '──────────\n❯ already typed\n──────────\n', 'loading...']:
            self.terms.screen = screen
            with self.assertRaises(ValueError): self.control('compact')
        self.terms.screen = '──────────\n❯ \n──────────\n'
        self.phase = 'working'
        with self.assertRaisesRegex(ValueError, '끝난'): self.control('compact')
        self.phase = 'idle'
        for command, value in [('model', 'opus\n/clear'), ('effort', 'ultra'), ('compact', 'unexpected')]:
            with self.assertRaises(ValueError): self.control(command, value)
        self.assertEqual(self.terms.pastes, [])

    def test_lost_compact_result_is_not_repeated_and_no_success_is_inferred(self):
        self.terminal()
        self.terms.screen = '──────────\n❯ \n──────────\n'
        self.terms.paste_error = OSError('fixture response lost')
        result = self.control('compact')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.control('compact'), result)
        self.assertEqual(len(self.terms.pastes), 1)
        self.assertEqual(self.terms.keys, [])

    def test_compact_gets_second_enter_only_when_that_exact_command_remains_typed(self):
        self.terminal()
        self.terms.screens = ['──────────\n❯ \n──────────\n', '──────────\n❯ /compact\n──────────\n']
        with patch.object(chat, 'SUBMIT_CHECK_SECONDS', 0): result = self.control('compact')
        self.assertTrue(result['resubmitted'])
        self.assertEqual(self.terms.keys, [(TERMINAL_ID, ['Enter'])])
        self.assertEqual(self.control('compact'), result)
        self.assertEqual(len(self.terms.keys), 1)

    def test_model_footer_does_not_take_values_from_transcript_or_typed_input(self):
        before = 'Opus · max effort is an example\n──────────\n❯ Sonnet high effort\n──────────\n ready\n'
        self.assertEqual(chat.terminal_model_settings(before), {})
        footer = '──────────\n❯ \n──────────\n  Opus · high effort\n'
        self.assertEqual(chat.terminal_model_settings(footer), {'model': 'opus', 'effort': 'high'})
        self.terminal(); self.terms.screen = footer
        self.assertEqual(self.chat.state({'source': CLAUDE_REF})['modelSettings'], {'model': 'opus', 'effort': 'high', 'observed': 'runtime'})

    def test_model_footer_reads_a_status_line_and_keeps_the_full_model_id(self):
        # A status line draws "Opus 5.5 · xhigh · 5h 12% · 주간 30%" with no word "effort".
        line = '──────────\n❯ \n──────────\n  Opus 5.5 · xhigh · 5h 12% · 주간 30%\n'
        self.assertEqual(chat.terminal_model_settings(line), {'model': 'opus', 'effort': 'xhigh'})
        self.assertEqual(chat.terminal_model_settings(line.replace('Opus 5.5', 'Fable 5.1').replace('xhigh', 'max')),
                         {'model': 'fable', 'effort': 'max'})
        self.assertEqual(chat.terminal_model_settings(line.replace(' · xhigh', '')), {'model': 'opus'})
        self.terminal(); self.terms.screen = line
        self.model_settings = {'model': 'claude-opus-5-5', 'effort': 'high', 'observed': 'last_response'}
        # The screen's newer effort counts; its "opus" does not replace the response's full id.
        self.assertEqual(self.state()['modelSettings'], {'model': 'claude-opus-5-5', 'effort': 'xhigh', 'observed': 'runtime'})
        self.terms.screen = line.replace('Opus 5.5', 'Sonnet 5.5')
        self.assertEqual(self.state()['modelSettings'], {'model': 'sonnet', 'effort': 'xhigh', 'observed': 'runtime'})

    def send(self, rid='request-0001', text='안녕하세요', **extra):
        return self.chat.send({'source': CLAUDE_REF, 'requestId': rid, 'text': text, **extra})

    def state(self, **extra):
        self.chat.cache.clear()
        return self.chat.state({'source': CLAUDE_REF, **extra})

    def rejected(self, **kwargs):
        with self.assertRaises(chat.SendRejected) as caught:
            self.send(**kwargs)
        self.assertEqual(caught.exception.status, 409)
        return str(caught.exception)

    def test_input_ready_reads_claude_codes_input_line_not_a_transcript_message(self):
        ruled = '답변 끝\n✻ Cooked for 3s\n───── title ─\n❯ \n──────────\n  Opus · xhigh\n  ⏵⏵ bypass permissions on'
        boxed = '답변 끝\n╭──────────╮\n│ >        │\n╰──────────╯\n  ? for shortcuts'
        self.assertTrue(chat.input_ready(ruled))
        self.assertTrue(chat.input_ready(boxed))
        # A busy session lists its status, mode and tasks below the box (six lines and more).
        busy = ruled + '\n  ⏺ 1 background task\n  ◯ task one\n  ◯ task two\n  ◯ task three\n  ◯ task four'
        self.assertTrue(chat.input_ready(busy))
        # A message in the transcript has text below it, not a rule; nor a message or a quote
        # with a rule below and no box or rule above (no input box drawn).
        self.assertFalse(chat.input_ready('> 사용자 메시지\n답변 첫 줄\n답변 둘째 줄'))
        self.assertFalse(chat.input_ready('> 사용자 메시지\n──────────\n  Opus · xhigh'))
        self.assertFalse(chat.input_ready('❯ 사용자 메시지\n──────────\n  ⏵⏵ bypass'))
        self.assertFalse(chat.input_ready('답변\n> 인용문\n──────────\n다음'))
        # An open menu is not an input line.
        self.assertFalse(chat.input_ready('Do you want to proceed?\n❯ 1. Yes\n  2. No\n──────────'))
        self.assertFalse(chat.input_ready(''))

    def test_a_working_sessions_attach_is_ready_once_its_input_line_shows(self):
        # Its spinner never stops: waiting for quiet alone took the whole limit (about 14s sends).
        counter = iter(range(1, 10000))
        self.terms.read = lambda terminal_id, after=0, wait=20, epoch=None: (
            time.sleep(0.02), {'after': next(counter), 'alive': True})[1]
        self.terms.screen = '✻ Working…\n──────────\n❯ \n──────────\n  Opus'
        started = time.monotonic()
        self.assertTrue(self.chat._settled(TERMINAL_ID))
        self.assertLess(time.monotonic() - started, 3)
        # Without an input line it waits as before, and at the limit takes what it saw.
        self.terms.screen = '✻ Working…\n> 사용자 메시지\n답변'
        with patch.object(chat, 'CLAUDE_READY_SECONDS', 1.5):
            started = time.monotonic()
            self.assertTrue(self.chat._settled(TERMINAL_ID))
            self.assertGreaterEqual(time.monotonic() - started, 1.4)

    def test_starting_a_message_opens_a_background_sessions_attach_once(self):
        opened, gate = [], threading.Event()
        def attach(ref, source, request_id):
            opened.append(request_id)
            gate.wait(3)
            return {'id': TERMINAL_ID}
        with patch.object(self.chat, '_open_claude_terminal', side_effect=attach):
            self.assertEqual(self.chat.warm({'source': CLAUDE_REF}), {'warming': True})
            # Again while it opens: no second attach.
            self.assertEqual(self.chat.warm({'source': CLAUDE_REF}), {'warming': True})
            # A send pressed meanwhile waits on the same lock instead of opening another.
            wait_for(lambda: self.chat._claude_lock_for(self.chat.resolve(CLAUDE_REF)).locked())
            gate.set()
            wait_for(lambda: not self.chat.warming)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith('warm_'))
        # A board terminal already there, an interactive session, a full board: nothing opens.
        with patch.object(self.chat, '_open_claude_terminal', side_effect=AssertionError('no warm-up')):
            self.terminal()
            self.chat.warm({'source': CLAUDE_REF}); wait_for(lambda: not self.chat.warming)
            self.terms.rows.clear()
            self.terms.rows.extend({'id': '%032x' % index, 'alive': True, 'closed': False} for index in range(15))
            self.chat.warm({'source': CLAUDE_REF}); wait_for(lambda: not self.chat.warming)
            self.terms.rows.clear()
            # A finished session is not woken by typing.
            self.row['phase'] = 'done'; self.board.poll(HOST)
            self.assertEqual(self.chat.warm({'source': CLAUDE_REF}), {'warming': False})
            self.row['phase'] = 'idle'
            self.kind('interactive')
            self.assertEqual(self.chat.warm({'source': CLAUDE_REF}), {'warming': False})
        with self.assertRaises(ValueError):
            self.chat.warm({'source': CLAUDE_REF, 'text': 'x'})

    def test_a_send_waiting_on_a_warm_up_says_so(self):
        source = self.chat.resolve(CLAUDE_REF)
        held = SimpleNamespace(acquire=lambda *args, **kwargs: False, release=lambda: None)
        self.chat.warming.add(self.chat._warm_key(source))
        try:
            with patch.object(self.chat, '_claude_lock_for', return_value=held):
                self.assertIn('연결을 여는 중', self.rejected())
                self.chat.warming.clear()
                self.assertIn('다른 메시지를 전달하고', self.rejected())
        finally:
            self.chat.warming.clear()

    def test_a_warm_up_never_holds_up_a_send_in_progress(self):
        lock = self.chat._claude_lock_for(self.chat.resolve(CLAUDE_REF))
        lock.acquire()
        try:
            with patch.object(self.chat, '_open_claude_terminal', side_effect=AssertionError('the send opens it')):
                self.chat.warm({'source': CLAUDE_REF})
                wait_for(lambda: not self.chat.warming)
        finally:
            lock.release()

    def test_state_capability_follows_board_terminal_background_and_needs_input(self):
        self.kind('interactive')
        state = self.state()
        self.assertFalse(state['capability']['supported'])
        self.assertEqual(state['capability']['reason'], chat.CLAUDE_EXTERNAL)
        # An interactive session cannot be attached, so no "open the terminal" action is offered.
        self.assertNotIn('action', state['capability'])
        for key in ('queuedMessages', 'queueState'):
            self.assertNotIn(key, state)
        for native in ({'host': 'other'}, {'id': 'other12345'}, {'agent': 'codex'}):
            with self.subTest(native=native):
                self.terms.rows = []
                self.terminal(**native)
                self.assertFalse(self.state()['capability']['supported'])
        for change in ({'alive': False}, {'closed': True}):
            with self.subTest(change=change):
                self.terms.rows = []
                self.terminal().update(change)
                self.assertFalse(self.state()['capability']['supported'])
        # A board-created terminal may carry the exact native session id instead of the agents row id.
        for native in ({}, {'id': CLAUDE_SID}, {'id': 'other12345', 'sessionId': CLAUDE_SID}):
            with self.subTest(native=native):
                self.terms.rows = []
                self.terminal(**native)
                state = self.state()
                self.assertTrue(state['capability']['supported'])
                self.assertIsNone(state['capability']['reason'])
                self.assertNotIn('action', state['capability'])
                self.assertFalse(state['requiresNativeTerminal'])
        self.terms.rows = []
        self.kind('background')
        # A background session without a board terminal takes input too: sending attaches it.
        capability = self.state()['capability']
        self.assertTrue(capability['supported'])
        self.assertIsNone(capability['reason'])
        self.assertNotIn('action', capability)
        # A session waiting for the user still takes a reply; menus are checked at send time.
        self.phase = 'needs_input'
        self.terminal()
        state = self.state()
        self.assertTrue(state['capability']['supported'])
        self.assertIsNone(state['capability']['reason'])
        self.assertTrue(state['requiresNativeTerminal'])
        self.phase = 'idle'
        with patch.object(self.terms, 'list', side_effect=RuntimeError('fixture terminal state')):
            state = self.state()
        self.assertFalse(state['capability']['supported'])
        self.assertNotIn('fixture', state['capability']['reason'])
        self.assertEqual([action for _, action in self.calls], ['read'] * 12)

    def test_send_pastes_into_board_terminal_once_and_stores_no_body(self):
        self.kind('interactive')
        self.terminal()
        result = self.send(text='  첫 줄\n둘째 줄  ')
        self.assertEqual(result['status'], 'accepted')
        receipt = result['receipt']
        self.assertTrue(receipt['confirmed'])
        self.assertTrue(receipt['acknowledged'])
        self.assertFalse(receipt['readbackConfirmed'])
        self.assertEqual(self.terms.pastes, [(TERMINAL_ID, '첫 줄\n둘째 줄', True)])
        self.assertEqual(self.calls, [('chat', 'capabilities')])
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual((records[0].parent.stat().st_mode & 0o777), 0o700)
        saved = records[0].read_text()
        for private in ('첫 줄', '둘째', '\\uccab'):
            self.assertNotIn(private, saved)
        self.assertEqual(json.loads(saved)['receipt']['delivery'], 'accepted')
        # Same request: the saved receipt answers; nothing reaches the CLI again, even after a restart.
        self.assertEqual(self.send(text='  첫 줄\n둘째 줄  ')['status'], 'accepted')
        self.send(text='다른 내용')
        restarted = chat.Chat(self.board, self.flow, self.root)
        restarted.send({'source': CLAUDE_REF, 'requestId': 'request-0001', 'text': '첫 줄\n둘째 줄'})
        self.assertEqual(len(self.terms.pastes), 1)
        self.assertEqual(self.calls, [('chat', 'capabilities')])

    def test_a_slow_send_leaves_one_line_of_its_step_times_and_nothing_of_the_message(self):
        import contextlib, io, instance
        self.kind('interactive')
        self.terminal()
        mark = instance.current()['brand']['logPrefix'] + ': 느린 전송 '
        def sent(text, rid, clock):
            self.chat.clock = clock
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(self.send(rid=rid, text=text)['status'], 'accepted')
            return [line for line in err.getvalue().splitlines() if line.startswith(mark)]
        # Each step takes half a second on this clock.
        ticks = iter(range(1000))
        lines = sent('개인 메시지 본문', 'request-0001', lambda: next(ticks) * 0.5)
        self.assertEqual(len(lines), 1)
        words = lines[0][len(mark):].split(' ')
        self.assertEqual(words[:2], ['claude', '4.50s'])
        self.assertEqual([word.split('=')[0] for word in words[2:]],
                         ['prepare', 'lock', 'capabilities', 'terminal', 'screen', 'paste', 'submit', 'save', 'rest'])
        for private in ('개인', '본문', self.row['title'], TERMINAL_ID, CLAUDE_REF['id'], CLAUDE_SID, 'local'):
            self.assertNotIn(private, lines[0])
        # A send under two seconds says nothing.
        self.assertEqual(sent('다음 메시지', 'request-0002', lambda: 7.0), [])

    def test_a_message_left_in_the_input_box_after_its_enter_gets_enter_once_more(self):
        # Claude Code took the Enter after the paste as a newline: the message sits in the box.
        self.kind('interactive')
        self.terminal()
        top, rule = '─' * 20 + ' plan ─', '─' * 40
        typed = '\n'.join(['답변 끝', top, '❯ 이 내용 스킬 업데이트하고싶어.', '  제안해봐 어떻게 하면 좋을지.', '  ', rule, '  Opus'])
        self.terms.screens = ['> ', typed]
        result = self.send(text='이 내용 스킬 업데이트하고싶어. 제안해봐 어떻게 하면 좋을지.')
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.terms.keys, [(TERMINAL_ID, ['Enter'])])
        self.assertTrue(json.loads(self.records()[0].read_text())['receipt']['resubmitted'])
        # Sent (the box is empty again): nothing more is pressed.
        self.terms.screens = ['> ', '\n'.join(['답변 끝', top, '❯ ', rule, '  Opus'])]
        self.send(rid='request-0002', text='다음 메시지')
        self.assertEqual(len(self.terms.keys), 1)

    def test_a_terminal_still_loading_from_an_earlier_send_is_waited_for_before_a_paste(self):
        self.terminal()
        self.chat.unsettled.add(TERMINAL_ID)
        with patch.object(self.chat, '_settled', return_value=False) as waited:
            with self.assertRaises(chat.SendRejected) as caught:
                self.send(rid='request-0011', text='아직 로딩 중')
        waited.assert_called_once_with(TERMINAL_ID)
        self.assertEqual((caught.exception.code, self.terms.pastes, self.records()), ('terminal_unavailable', [], []))
        with patch.object(self.chat, '_settled', return_value=True):
            self.assertEqual(self.send(rid='request-0012', text='이제 준비됨')['status'], 'accepted')
        self.assertNotIn(TERMINAL_ID, self.chat.unsettled)
        self.assertEqual(self.terms.pastes, [(TERMINAL_ID, '이제 준비됨', True)])

    def test_background_without_terminal_attaches_it_once_then_pastes(self):
        # 보내기 on a background session with no board terminal attaches that exact session
        # first. A failed attach is refused before any reservation, so the draft stays.
        with patch.object(self.chat, '_open_claude_terminal',
                          side_effect=chat.SendRejected('Claude 터미널이 준비되지 않았습니다.', 409)) as failing:
            with self.assertRaises(chat.SendRejected) as caught:
                self.send(rid='request-0002', text='백그라운드 확인')
        self.assertEqual((caught.exception.status, caught.exception.code), (409, 'terminal_unavailable'))
        failing.assert_called_once()
        self.assertEqual((self.terms.pastes, self.records()), ([], []))
        with patch.object(self.chat, '_open_claude_terminal', side_effect=RuntimeError('열린 터미널 4개가 가득 찼습니다.')):
            with self.assertRaises(chat.SendRejected) as caught:
                self.send(rid='request-0003', text='백그라운드 확인')
        self.assertEqual((str(caught.exception), caught.exception.code), ('열린 터미널 4개가 가득 찼습니다.', 'terminal_unavailable'))
        self.assertEqual((self.terms.pastes, self.records()), ([], []))
        opened = []

        def attach(ref, source, request_id):
            opened.append((ref['id'], source['kind'], request_id))
            return self.terminal()
        with patch.object(self.chat, '_open_claude_terminal', side_effect=attach):
            result = self.send(rid='request-0004', text='  백그라운드 확인  ')
            self.assertEqual(result['status'], 'accepted')
            # The attached terminal is used from then on; nothing opens a second one.
            self.assertEqual(self.send(rid='request-0005', text='두 번째')['status'], 'accepted')
        self.assertEqual(opened, [(CLAUDE_REF['id'], 'background', 'request-0004')])
        self.assertEqual(self.terms.pastes, [(TERMINAL_ID, '백그라운드 확인', True), (TERMINAL_ID, '두 번째', True)])
        self.assertEqual(len(self.records()), 2)
        # An interactive session still cannot be attached and is refused as before.
        self.terms.rows = []
        self.kind('interactive')
        with patch.object(self.chat, '_open_claude_terminal', side_effect=AssertionError('interactive is never attached')):
            with self.assertRaises(chat.SendRejected) as caught:
                self.send(rid='request-0006', text='대화형')
        self.assertEqual(str(caught.exception), chat.CLAUDE_EXTERNAL)

    def test_question_card_attach_failures_type_nothing(self):
        # "질문 불러오기" still attaches a background session; its failures reach the card.
        cases = [({'allowed': False, 'reason': '연결 계획을 만들 수 없습니다.'}, None, '연결 계획을 만들 수 없습니다.'),
                 ({'allowed': True, 'id': 'plan-1'}, RuntimeError('터미널 실행 실패'), '터미널 실행 실패'),
                 ({'allowed': True, 'id': 'plan-1'}, None, '준비되지 않았습니다')]
        self.terms.chunks = [{'after': 0, 'alive': False}]
        for plan, error, reason in cases:
            with self.subTest(reason=reason):
                launch = {'side_effect': error} if error else {'return_value': {'terminal': {'id': TERMINAL_ID}}}
                with patch.object(self.flow, 'plan', return_value=plan), patch.object(self.flow, 'launch', **launch):
                    with self.assertRaises(chat.PromptRejected) as caught:
                        self.chat.prompt({'source': CLAUDE_REF, 'action': 'open', 'requestId': 'prompt-0003'})
                self.assertIn(reason, str(caught.exception))
                self.assertEqual(caught.exception.code, 'terminal_unavailable')
                self.assertEqual((self.terms.pastes, self.terms.keys), ([], []))
                self.assertEqual(self.records(), [])

    def test_interactive_without_board_terminal_is_external_and_not_reserved(self):
        self.kind('interactive')
        with patch.object(self.flow, 'plan') as plan:
            self.assertEqual(self.rejected(), chat.CLAUDE_EXTERNAL)
        plan.assert_not_called()
        self.assertEqual(self.terms.pastes, [])
        self.assertEqual(self.records(), [])
        # Nothing was reserved, so the same request can be delivered once the board owns a terminal.
        self.terminal()
        self.assertEqual(self.send()['status'], 'accepted')
        self.assertEqual(len(self.terms.pastes), 1)

    def test_a_held_terminal_attaching_again_takes_no_send_until_it_is_back(self):
        row = self.terminal()
        row['holdName'] = 'sh-0123456789abcdef'
        waiting = Path(self.flow.transfers.hold_waiting) / row['holdName']
        waiting.parent.mkdir(parents=True)
        waiting.touch()
        self.assertIn('다시 연결하는 중', self.rejected())
        self.assertEqual(self.terms.pastes, [])
        self.assertEqual(self.records(), [], 'nothing reserved: the same request goes once it is back')
        waiting.unlink()
        self.assertEqual(self.send()['status'], 'accepted')
        self.assertEqual(len(self.terms.pastes), 1)

    def test_a_windows_claude_background_session_is_attached_like_a_mac_one(self):
        # A running PC background session attaches by its job id (windows_runtime.terminal_plan):
        # 보내기, the question card and a slash command open that exact session first.
        self.board.hosts[0]['platform'] = 'windows'
        try:
            state = self.state()
            self.assertEqual((state['capability']['supported'], state['capability']['reason']), (True, None))
            self.assertNotIn('action', state['capability'])
            self.assertTrue(state['capability']['attachments'], 'a PC session takes files as well')
            self.assertTrue(state['promptOpenable'])
            opened = []

            def attach(ref, source, request_id):
                opened.append((ref['id'], source['kind'], request_id))
                return self.terminal()
            with patch.object(self.chat, '_open_claude_terminal', side_effect=attach):
                self.assertEqual(self.send(rid='request-0004', text='PC 확인')['status'], 'accepted')
                self.assertEqual(self.send(rid='request-0005', text='두 번째')['status'], 'accepted')
            self.assertEqual(opened, [(CLAUDE_REF['id'], 'background', 'request-0004')])
            self.assertEqual(self.terms.pastes, [(TERMINAL_ID, 'PC 확인', True), (TERMINAL_ID, '두 번째', True)])
            # Waiting for the user with that terminal open: it still takes a reply.
            self.phase = 'needs_input'
            self.assertTrue(self.state()['capability']['supported'])
            # The question card attaches through the same plan as the terminal button.
            self.terms.rows = []
            self.terms.chunks = [{'after': 10, 'alive': True}, {'after': 25, 'alive': True}, {'after': 25, 'alive': True}]
            self.terms.screen = CLAUDE_QUESTION
            with patch.object(self.flow, 'plan', return_value={'allowed': True, 'id': 'plan-1'}) as plan, \
                    patch.object(self.flow, 'launch', side_effect=lambda *a: {'terminal': self.terminal()}):
                result = self.chat.prompt({'source': CLAUDE_REF, 'action': 'open', 'requestId': 'prompt-0001'})
            self.assertEqual(plan.call_args.args[0]['target'], {'host': HOST['name'], 'agent': 'claude', 'account': 'claude:default'})
            self.assertEqual(result['prompt']['terminalId'], TERMINAL_ID)
            # An interactive PC conversation still opens only from its terminal button (a resume).
            self.terms.rows = []
            self.kind('interactive')
            with patch.object(self.chat, '_open_claude_terminal', side_effect=AssertionError('a send never resumes a PC conversation')):
                with self.assertRaises(chat.SendRejected) as caught:
                    self.send(rid='request-0006', text='대화형')
            self.assertEqual((str(caught.exception), caught.exception.code), (chat.CLAUDE_WINDOWS_OPEN, 'terminal_required'))
            self.assertEqual(len(self.terms.pastes), 2)
        finally:
            self.board.hosts[0].pop('platform')

    def test_a_windows_claude_conversation_takes_input_through_its_resumed_board_terminal(self):
        self.board.hosts[0]['platform'] = 'windows'
        self.kind('interactive')
        try:
            # No terminal yet: its terminal button resumes it on the PC (or says it still runs there).
            state = self.state()
            self.assertEqual(state['capability'], {'supported': False, 'attachments': True,
                                                   'reason': chat.CLAUDE_WINDOWS_OPEN, 'action': 'open_terminal'})
            self.assertFalse(state['promptOpenable'])
            with patch.object(self.flow, 'plan', side_effect=AssertionError('a send never opens a PC terminal')):
                with self.assertRaises(chat.SendRejected) as caught:
                    self.send()
            self.assertEqual((str(caught.exception), caught.exception.code), (chat.CLAUDE_WINDOWS_OPEN, 'terminal_required'))
            self.assertEqual((self.terms.pastes, self.records()), ([], []))
            # Resumed in a board terminal: the text is pasted there, like a Mac session.
            self.terminal(kind='interactive')
            state = self.state()
            self.assertTrue(state['capability']['supported'])
            self.assertTrue(state['capability']['attachments'], 'a PC session takes files as well')
            self.assertEqual(self.send()['status'], 'accepted')
            self.assertEqual(len(self.terms.pastes), 1)
            self.assertNotIn(('chat', 'send'), self.calls)
            # A menu on that screen is still answered only from what it shows.
            self.terms.screen = CLAUDE_PERMISSION
            self.assertEqual(self.rejected(rid='request-0002'), chat.CLAUDE_BLOCKED)
            self.assertIsNotNone(self.state()['prompt'])
        finally:
            self.board.hosts[0].pop('platform')

    def test_a_new_session_terminal_linked_by_start_time_never_takes_a_paste(self):
        # A 새 세션 terminal linked by start time may run another session: it is shown, not typed into.
        self.kind('interactive')
        self.terminal()['linkConfidence'] = 'estimated'
        with patch.object(self.flow, 'plan') as plan:
            self.assertEqual(self.rejected(), chat.CLAUDE_EXTERNAL)
        plan.assert_not_called()
        self.assertEqual(self.terms.pastes, [])
        self.assertEqual(self.records(), [])
        self.assertFalse(self.state()['capability']['supported'])

    def test_selection_menu_on_screen_or_unknown_state_never_types_into_terminal(self):
        self.terminal()
        self.phase = 'needs_input'
        # The same parse_menu that feeds the web answers decides the block, with or without a cursor.
        for menu in ('Do you want to proceed?\n❯ 1. Yes\n  2. No', CLAUDE_PERMISSION, CLAUDE_BOXED, CLAUDE_QUESTION,
                     CLAUDE_PERMISSION.replace('❯ 1.', '  1.'), '│ ❯ 1. Yes, allow once │\n│   2. No │'):
            with self.subTest(menu=menu):
                self.terms.screen = menu
                with patch.object(self.flow, 'plan') as plan:
                    self.assertEqual(self.rejected(), chat.CLAUDE_BLOCKED)
                plan.assert_not_called()
                self.assertEqual(self.terms.pastes, [])
                self.assertEqual(self.records(), [])
        with patch.object(self.terms, 'capture', side_effect=RuntimeError('fixture capture')):
            self.assertNotIn('fixture', self.rejected())
        self.assertEqual(self.terms.pastes, [])
        self.phase = 'idle'
        self.flow.transfers.rpc = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('fixture host offline'))
        self.assertNotIn('fixture', self.rejected())
        self.assertEqual(self.terms.pastes, [])
        self.assertEqual(self.records(), [])

    def test_terminal_control_characters_are_rejected_before_any_paste(self):
        self.terminal()
        for text in ('앞\x1b[201~뒤', '종료\x03', '지우기\x7f'):
            with self.subTest(text=repr(text)):
                with self.assertRaises(chat.SendRejected) as caught:
                    self.send(rid='request-ctrl-' + str(len(text)), text=text)
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.terms.pastes, [])
        self.assertEqual(self.records(), [])
        self.assertEqual(self.send(rid='request-tabs', text='첫 줄\n\t둘째 줄')['status'], 'accepted')

    def test_waiting_session_without_menu_takes_a_reply(self):
        self.terminal()
        self.phase = 'needs_input'
        self.terms.screen = '● 작업을 마쳤습니다. 다음에 무엇을 할까요?\n\n> '
        self.assertEqual(self.send(text='다음 단계 진행해')['status'], 'accepted')
        self.assertEqual(self.terms.pastes, [(TERMINAL_ID, '다음 단계 진행해', True)])
        self.assertEqual(self.terms.log[-3:], ['capture', 'paste', 'capture'])
        # A numbered list in the answer above the input line is not a menu (the old regex
        # blocked any "> 1." on screen; only a menu ending the screen blocks now).
        self.terms.screen = ANSWER_WITH_LIST.replace('  1. 테스트', '> 1. 테스트')
        self.assertEqual(self.send(rid='request-0002', text='2번')['status'], 'accepted')
        self.assertEqual(len(self.terms.pastes), 2)

    def test_input_not_sent_releases_reservation_and_rejects(self):
        self.terminal()
        self.terms.paste_error = terminal.InputNotSent('터미널에 붙여넣지 못했습니다.')
        self.assertEqual(self.rejected(), '터미널에 붙여넣지 못했습니다.')
        self.assertEqual(self.records(), [])
        self.terms.paste_error = None
        self.assertEqual(self.send()['status'], 'accepted')
        self.assertEqual(len(self.terms.pastes), 2)
        self.assertEqual(len(self.records()), 1)

    def test_paste_failure_after_reservation_is_unknown_and_never_resent(self):
        self.terminal()
        self.terms.paste_error = RuntimeError('fixture private enter failure')
        result = self.send()
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['receipt']['confirmed'])
        self.assertFalse(result['receipt']['acknowledged'])
        self.assertNotIn('fixture private', json.dumps(result))
        self.terms.paste_error = None
        self.assertEqual(self.send()['status'], 'unknown')
        self.assertEqual(len(self.terms.pastes), 1)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.state(requestId='request-0001')['receipts'][0]['status'], 'unknown')

    def test_watcher_read_serves_the_local_ledger_receipt_from_cache(self):
        self.terminal()
        self.chat.cache.clear()
        self.send()
        self.messages = [{'role': 'user', 'text': '안녕하세요', 'ts': int(time.time())}]
        watched = self.chat.state({'source': CLAUDE_REF}, watching=True)
        self.assertEqual([r['requestId'] for r in watched['receipts']], ['request-0001'])
        reads = self.calls.count(('chat', 'read'))
        receipt = self.chat.state({'source': CLAUDE_REF, 'requestId': 'request-0001'})['receipts'][0]
        self.assertEqual(self.calls.count(('chat', 'read')), reads)
        # The Claude receipt is still the board's own ledger judgment against the read messages.
        self.assertEqual(receipt['status'], 'accepted')
        self.assertTrue(receipt['readbackConfirmed'])
        self.assertEqual(receipt, watched['receipts'][0])
        missing = self.chat.state({'source': CLAUDE_REF, 'requestId': 'request-9999'})['receipts'][0]
        self.assertEqual(self.calls.count(('chat', 'read')), reads + 1)
        self.assertEqual(missing['status'], 'unknown')

    def test_receipt_readback_matches_collapsed_opening_text_of_new_user_message(self):
        self.terminal()
        long_tail = '가' * 300
        self.send(text='안녕  하세요\n확인 ' + long_tail)
        same, now = '안녕 하세요 확인 ' + long_tail, int(time.time())
        cases = [([{'role': 'user', 'text': same, 'ts': now}], True),
                 ([{'role': 'user', 'text': same[:200] + ' 원본 기록에서 잘린 뒷부분', 'ts': now}], True),
                 ([{'role': 'assistant', 'text': same, 'ts': now}], False),
                 ([{'role': 'user', 'text': same, 'ts': now - 60}], False),
                 ([{'role': 'user', 'text': same, 'ts': None}], False),
                 ([{'role': 'user', 'text': '다른 메시지', 'ts': now}], False)]
        for index, (messages, confirmed) in enumerate(cases):
            with self.subTest(case=index):
                self.messages = messages
                receipt = self.state(requestId='request-0001')['receipts'][0]
                self.assertEqual(receipt['status'], 'accepted')
                self.assertTrue(receipt['confirmed'])
                self.assertIs(receipt['readbackConfirmed'], confirmed)
                self.assertIs(receipt['currentSnapshotConfirmed'], confirmed)
        missing = self.state(requestId='request-9999')['receipts'][0]
        self.assertEqual(missing['status'], 'unknown')
        self.assertFalse(missing['confirmed'])
        self.assertEqual(len(self.terms.pastes), 1)

    def test_receipt_readback_matches_the_words_claude_code_recorded_for_an_image(self):
        # The board pastes "본문\n첨부 파일: <경로>"; Claude Code records the image path as
        # "[Image #1]" (before, after or instead of the words) and drops the line.
        self.terminal()
        uploaded = self.chat.upload(CLAUDE_REF, 'shot.png', b'\x89PNG\r\n\x1a\nfixture')['attachment']
        self.send(text='코덱스로 이어서 하려는데 여기서 막힘', attachments=[uploaded['id']])
        self.send(rid='request-0002', text='', attachments=[uploaded['id']])
        image = {'id': None, 'name': '이미지', 'type': 'image/*', 'size': None, 'kind': 'image', 'source': 'native'}
        now = int(time.time())
        cases = [('request-0001', '[Image #1]코덱스로 이어서 하려는데 여기서 막힘', [image], True),
                 ('request-0001', '[Image #1] 코덱스로   이어서 하려는데\n여기서 막힘', [image], True),
                 ('request-0001', '코덱스로 이어서 하려는데 여기서 막힘 [Image #1]', [image], True),
                 ('request-0001', '코덱스로 이어서 하려는데 여기서 막힘\n첨부 파일: ' + uploaded['path'], [], True),
                 ('request-0001', '[Image #1]코덱스로 이어서 하려는데 여기서 막힘 그리고 더', [image], False),
                 ('request-0001', '[Image #1]다른 메시지', [image], False),
                 ('request-0002', '[Image #1]', [image], True),
                 ('request-0002', '[이미지 첨부]', [image], True),
                 ('request-0002', '[Pasted text #1 +12 lines]', [], False)]
        for index, (rid, text, files, confirmed) in enumerate(cases):
            with self.subTest(case=index):
                self.messages = [{'role': 'user', 'text': text, 'ts': now, 'attachments': files}]
                receipt = self.state(requestId=rid)['receipts'][0]
                self.assertEqual(receipt['status'], 'accepted')
                self.assertIs(receipt['readbackConfirmed'], confirmed)
        # Only hashes of the words are kept, never the words or the path.
        saved = ''.join(path.read_text() for path in self.records())
        self.assertNotIn('코덱스', saved)
        self.assertNotIn(uploaded['path'], saved)

    def test_unknown_paste_result_is_accepted_once_the_transcript_shows_the_message(self):
        self.terminal()
        self.terms.paste_error = RuntimeError('fixture enter failure')
        self.assertEqual(self.send(text='확인해 주세요')['status'], 'unknown')
        now = int(time.time())
        self.messages = [{'role': 'user', 'text': '다른 메시지', 'ts': now}]
        self.assertEqual(self.state(requestId='request-0001')['receipts'][0]['status'], 'unknown')
        self.messages = [{'role': 'user', 'text': '확인해 주세요', 'ts': now - 60}]
        self.assertEqual(self.state(requestId='request-0001')['receipts'][0]['status'], 'unknown',
                         'a message from before the send is not this one')
        self.messages = [{'role': 'user', 'text': '확인해 주세요', 'ts': now}]
        receipt = self.state(requestId='request-0001')['receipts'][0]
        self.assertEqual(receipt['status'], 'accepted')
        self.assertTrue(receipt['confirmed'])
        self.assertTrue(receipt['readbackConfirmed'])
        self.assertEqual(len(self.terms.pastes), 1, 'and nothing was sent again')
        # A short message is matched whole: a longer one that starts the same is another message.
        self.assertEqual(self.send(rid='request-0002', text='확인')['status'], 'unknown')
        self.messages = [{'role': 'user', 'text': '확인해 주세요', 'ts': now}]
        self.assertEqual(self.state(requestId='request-0002')['receipts'][0]['status'], 'unknown')
        self.messages = [{'role': 'user', 'text': '확인', 'ts': now}]
        self.assertEqual(self.state(requestId='request-0002')['receipts'][0]['status'], 'accepted')

    def test_attachment_paths_are_pasted_as_separate_lines(self):
        self.terminal()
        uploaded = self.chat.upload(CLAUDE_REF, 'memo.txt', b'fixture')['attachment']
        result = self.send(text='파일 확인', attachments=[uploaded['id']])
        self.assertEqual(self.terms.pastes[0][1], '파일 확인\n첨부 파일: ' + uploaded['path'])
        self.assertEqual(result['receipt']['attachments'][0]['name'], 'memo.txt')
        self.assertNotIn('path', result['receipt']['attachments'][0])
        self.assertNotIn(uploaded['path'], self.records()[0].read_text())
        self.send(rid='request-0002', text='', attachments=[uploaded['id']])
        self.assertEqual(self.terms.pastes[1][1], '첨부 파일: ' + uploaded['path'])

    def test_a_windows_claude_session_takes_a_file_the_pc_stored_as_its_windows_path_line(self):
        # The PC stores the upload itself (windows_attachments) and names its own path; the paste
        # carries that path, backslashes and all, as a Mac session's line.
        self.board.hosts[0]['platform'] = 'windows'
        uploads, original = [], self.flow.transfers.rpc

        def rpc(host, op, args, timeout):
            if op != 'attachment':
                return original(host, op, args, timeout)
            uploads.append({key: value for key, value in args.items() if key != 'data'})
            stored = attachments.receive(args)
            return {**stored, 'path': 'C:\\Users\\example\\project\\' + stored['relativePath'].replace('/', '\\')}
        self.flow.transfers.rpc = rpc
        try:
            self.terminal()
            self.assertTrue(self.state()['capability']['attachments'])
            uploaded = self.chat.upload(CLAUDE_REF, 'shot.png', b'\x89PNG\r\n\x1a\nfixture')['attachment']
            self.assertEqual(uploads[0]['source'], {'host': 'local', 'agent': 'claude', 'home': None, 'id': CLAUDE_REF['id']})
            self.assertEqual(uploads[0]['cwd'], str(self.project))
            result = self.send(text='PC 화면 확인', attachments=[uploaded['id']])
            self.assertEqual(result['status'], 'accepted')
            self.assertEqual(self.terms.pastes, [(TERMINAL_ID, 'PC 화면 확인\n첨부 파일: ' + uploaded['path'], True)])
            self.assertTrue(uploaded['path'].startswith('C:\\Users\\example\\project\\.'))
            self.assertEqual(result['receipt']['attachments'][0]['name'], 'shot.png')
        finally:
            self.board.hosts[0].pop('platform')

    # ---------- menus answered from the web ----------
    def answer(self, number, fingerprint=None, rid='answer-0001', source=CLAUDE_REF):
        if fingerprint is None:
            fingerprint = chat.parse_menu(CLAUDE_PERMISSION)['fingerprint']
        return self.chat.prompt({'source': source, 'action': 'answer', 'fingerprint': fingerprint,
                                 'number': number, 'requestId': rid})

    def refused(self, *args, **kwargs):
        with self.assertRaises(chat.PromptRejected) as caught:
            self.answer(*args, **kwargs)
        self.assertEqual(caught.exception.status, 409)
        return caught.exception

    def test_state_reports_the_board_terminal_menu_and_whether_one_can_be_opened(self):
        state = self.state()
        self.assertIsNone(state['prompt'])
        self.assertTrue(state['promptOpenable'])
        self.terminal()
        self.terms.screen = CLAUDE_PERMISSION
        state = self.state()
        self.assertFalse(state['promptOpenable'])
        prompt = state['prompt']
        self.assertEqual(prompt['terminalId'], TERMINAL_ID)
        self.assertEqual(prompt['agent'], 'claude')
        self.assertEqual(prompt['current'], 1)
        self.assertFalse(prompt['multi'])
        self.assertEqual(prompt['fingerprint'], chat.parse_menu(CLAUDE_PERMISSION)['fingerprint'])
        self.assertEqual([o['number'] for o in prompt['options']], [1, 2, 3])
        self.assertEqual(set(prompt), {'question', 'options', 'current', 'multi', 'fingerprint', 'terminalId', 'agent'})
        # Displayed text is redacted like messages, while the fingerprint stays the screen's.
        secret = CLAUDE_PERMISSION.replace('rm -rf build', 'curl -H "Authorization: Bearer abcdefghijklmnop"')
        self.terms.screen = secret
        prompt = self.state()['prompt']
        self.assertIn('[redacted]', prompt['question'])
        self.assertNotIn('abcdefghijklmnop', json.dumps(prompt))
        self.assertEqual(prompt['fingerprint'], chat.parse_menu(secret)['fingerprint'])
        for screen, error in (('● 완료했습니다.\n\n> ', None), (CLAUDE_PERMISSION, RuntimeError('fixture capture'))):
            with self.subTest(screen=screen[:8], error=error):
                self.terms.screen, self.terms.capture_error = screen, error
                state = self.state()
                self.assertIsNone(state['prompt'])
                self.assertTrue(state['capability']['supported'])
        self.terms.capture_error = None
        self.terms.rows = []
        self.kind('interactive')
        state = self.state()
        self.assertIsNone(state['prompt'])
        self.assertFalse(state['promptOpenable'])
        self.kind('background')
        with patch.object(self.terms, 'list', side_effect=RuntimeError('fixture terminal state')):
            state = self.state()
        self.assertIsNone(state['prompt'])
        self.assertFalse(state['promptOpenable'])
        self.assertEqual(self.terms.keys, [])

    def test_open_attaches_a_background_session_once_then_reads_its_menu(self):
        self.terms.chunks = [{'after': 10, 'alive': True}, {'after': 25, 'alive': True}, {'after': 25, 'alive': True}]
        self.terms.screen = CLAUDE_QUESTION
        def launch(plan_id, request_id):
            self.terms.log.append('launch')
            self.terminal()
            return {'terminal': {'id': TERMINAL_ID}}
        with patch.object(self.flow, 'plan', return_value={'allowed': True, 'id': 'plan-1'}) as plan, \
                patch.object(self.flow, 'launch', side_effect=launch) as launched:
            self.assertTrue(self.state()['promptOpenable'])
            result = self.chat.prompt({'source': CLAUDE_REF, 'action': 'open', 'requestId': 'prompt-0001'})
            launched.assert_called_once_with('plan-1', 'chat_' + hashlib.sha256(b'prompt-0001').hexdigest()[:40])
            self.assertEqual(result['prompt']['terminalId'], TERMINAL_ID)
            self.assertEqual(result['prompt']['options'][0]['detail'], 'A 설명')
            self.assertEqual(self.terms.log[-1], 'capture')
            # The attached terminal is reused; a request id is optional for opening.
            again = self.chat.prompt({'source': CLAUDE_REF, 'action': 'open'})
            self.assertEqual(again['prompt']['fingerprint'], result['prompt']['fingerprint'])
            plan.assert_called_once()
            launched.assert_called_once()
            self.assertFalse(self.state()['promptOpenable'])
        self.terms.screen = '> '
        self.assertEqual(self.chat.prompt({'source': CLAUDE_REF, 'action': 'open'}), {'prompt': None})
        self.assertEqual(self.terms.keys, [])
        self.assertEqual(self.terms.pastes, [])

    def test_open_without_an_attachable_terminal_is_refused(self):
        self.kind('interactive')
        with patch.object(self.flow, 'plan') as plan:
            with self.assertRaises(chat.PromptRejected) as caught:
                self.chat.prompt({'source': CLAUDE_REF, 'action': 'open'})
        plan.assert_not_called()
        self.assertEqual((caught.exception.status, caught.exception.code), (409, 'terminal_required'))
        self.kind('background')
        with patch.object(self.flow, 'plan', return_value={'allowed': False, 'reason': '연결 계획 거절'}):
            with self.assertRaises(chat.PromptRejected) as caught:
                self.chat.prompt({'source': CLAUDE_REF, 'action': 'open', 'requestId': 'prompt-0002'})
        self.assertEqual(str(caught.exception), '연결 계획 거절')
        self.assertEqual(caught.exception.code, 'terminal_unavailable')
        for data in ({'action': 'close'}, {'action': 'open', 'requestId': 'bad id'},
                     {'action': 'answer', 'fingerprint': 'f' * 64, 'number': 1}):
            with self.subTest(data=data), self.assertRaises(ValueError) as caught:
                self.chat.prompt({'source': CLAUDE_REF, **data})
            self.assertNotIsInstance(caught.exception, chat.PromptRejected)

    def test_answer_moves_the_cursor_presses_enter_and_confirms_on_the_next_screen(self):
        self.terminal()
        cases = [  # screen before, number, keys, screen after, status
            (CLAUDE_PERMISSION, 3, ['Down', 'Down', 'Enter'], '● 삭제했습니다.\n\n> ', 'answered'),
            (CLAUDE_PERMISSION, 1, ['Enter'], '', 'answered'),
            (CLAUDE_PERMISSION.replace('❯ 1.', '  1.').replace('   2. Yes,', ' ❯ 2. Yes,'), 1, ['Up', 'Enter'],
             '> ', 'answered'),
            (CLAUDE_PERMISSION, 2, ['Down', 'Enter'], CLAUDE_QUESTION, 'answered'),
            (CLAUDE_PERMISSION, 2, ['Down', 'Enter'], CLAUDE_PERMISSION.replace('❯ 1.', '  1.').replace('   2. Yes,', ' ❯ 2. Yes,'),
             'unconfirmed'),
        ]
        for index, (before, number, keys, after, status) in enumerate(cases):
            with self.subTest(case=index):
                self.terms.screens = [before, after]
                with patch.object(chat.time, 'sleep') as sleep:
                    result = self.answer(number, rid='answer-%04d' % index)
                sleep.assert_called_once_with(0.6)
                self.assertEqual(self.terms.keys[-1], (TERMINAL_ID, keys))
                self.assertEqual(result['status'], status)
                self.assertEqual(result['requestId'], 'answer-%04d' % index)
                self.assertEqual(result['number'], number)
                expected = chat.parse_menu(after)
                if expected is None:
                    self.assertIsNone(result['prompt'])
                    self.assertIsNone(result['reason'])
                else:
                    self.assertEqual(result['prompt']['fingerprint'], expected['fingerprint'])
                    self.assertEqual(result['prompt']['terminalId'], TERMINAL_ID)
        self.assertEqual(chat.PROMPT_SETTLE, 0.6)
        self.assertIsNotNone(result['reason'])
        self.assertEqual(self.terms.pastes, [])

    def test_same_request_id_returns_the_saved_answer_without_pressing_again(self):
        self.terminal()
        self.terms.screens = [CLAUDE_PERMISSION, '> ']
        with patch.object(chat.time, 'sleep'):
            first = self.answer(2)
            log = list(self.terms.log)
            self.assertEqual(self.answer(2), first)
        self.assertEqual(len(self.terms.keys), 1)
        self.assertEqual(self.terms.log, log)
        with self.assertRaises(ValueError) as caught:
            self.answer(3)
        self.assertNotIsInstance(caught.exception, chat.PromptRejected)
        self.assertEqual(len(self.terms.keys), 1)
        # Bounded in memory: the oldest saved answer is dropped first.
        with patch.object(chat, 'PROMPT_RESULTS', 2), patch.object(chat.time, 'sleep'):
            for rid in ('answer-0002', 'answer-0003'):
                self.terms.screens = [CLAUDE_PERMISSION, '> ']
                self.answer(1, rid=rid)
        self.assertEqual(len(self.chat.prompt_results), 2)

    def test_changed_or_unanswerable_menus_never_press_keys(self):
        self.terminal()
        fingerprint = chat.parse_menu(CLAUDE_PERMISSION)['fingerprint']
        cases = [
            (CLAUDE_PERMISSION.replace('rm -rf build', 'rm -rf src'), 1, '질문이 바뀌었거나'),
            ('● 완료했습니다.\n\n> ', 1, '질문이 바뀌었거나'),
            (CLAUDE_PERMISSION, 4, '없는 선택지'),
            (CLAUDE_PERMISSION, 0, '없는 선택지'),
            (CLAUDE_PERMISSION.replace('❯ 1.', '  1.'), 2, '현재 선택 위치'),
        ]
        for screen, number, reason in cases:
            with self.subTest(reason=reason, number=number):
                self.terms.screen = screen
                exc = self.refused(number, fingerprint)
                self.assertIn(reason, str(exc))
                self.assertEqual(exc.code, 'prompt_changed')
        multi = CLAUDE_QUESTION.replace('1. 옵션 A', '1. [ ] 옵션 A')
        self.terms.screen = multi
        self.assertIn('여러 개', str(self.refused(1, chat.parse_menu(multi)['fingerprint'])))
        self.terms.screen = CLAUDE_PERMISSION
        self.terms.capture_error = RuntimeError('fixture capture')
        exc = self.refused(1)
        self.assertEqual(exc.code, 'terminal_unavailable')
        self.assertNotIn('fixture', str(exc))
        self.terms.capture_error = None
        for value in ('F' * 64, 'f' * 63, None, 64):
            with self.subTest(fingerprint=value), self.assertRaises(ValueError):
                self.chat.prompt({'source': CLAUDE_REF, 'action': 'answer', 'fingerprint': value,
                                  'number': 1, 'requestId': 'answer-0001'})
        for value in (True, 1.0, '1', None):
            with self.subTest(number=value), self.assertRaises(ValueError):
                self.answer(value)
        self.assertEqual(self.terms.keys, [])
        self.assertNotIn('keys', self.terms.log)
        # Nothing was stored for refused requests: the same id answers once the menu matches.
        with patch.object(chat.time, 'sleep'):
            self.terms.screens = [CLAUDE_PERMISSION, '> ']
            self.assertEqual(self.answer(1)['status'], 'answered')
        self.terms.rows = []
        self.assertEqual(self.refused(1, rid='answer-0099').code, 'terminal_required')
        self.assertEqual(len(self.terms.keys), 1)

    def test_keys_refused_by_tmux_may_retry_but_an_unknown_delivery_never_repeats(self):
        self.terminal()
        self.terms.screen = CLAUDE_PERMISSION
        self.terms.keys_error = terminal.InputNotSent('터미널에 키를 보내지 못했습니다.')
        exc = self.refused(2)
        self.assertEqual((str(exc), exc.code), ('터미널에 키를 보내지 못했습니다.', 'terminal_unavailable'))
        self.assertEqual(self.chat.prompt_results, {})
        self.terms.keys_error = RuntimeError('fixture tmux timeout')
        with patch.object(chat.time, 'sleep') as sleep:
            result = self.answer(2)
        sleep.assert_not_called()
        self.assertEqual(result['status'], 'unconfirmed')
        self.assertNotIn('fixture', json.dumps(result))
        self.terms.keys_error = None
        self.assertEqual(self.answer(2), result)
        self.assertEqual(self.terms.keys, [])
        # A confirmation capture that fails after the keys were sent is unconfirmed too.
        self.terms.screens = [CLAUDE_PERMISSION]
        with patch.object(self.terms, 'capture', side_effect=[CLAUDE_PERMISSION, RuntimeError('fixture')]), \
                patch.object(chat.time, 'sleep'):
            self.assertEqual(self.answer(2, rid='answer-0002')['status'], 'unconfirmed')
        self.assertEqual(len(self.terms.keys), 1)

    def test_answer_waits_for_a_terminal_send_and_clears_the_cached_state(self):
        self.terminal()
        self.terms.screen = CLAUDE_PERMISSION
        self.assertIsNotNone(self.chat.state({'source': CLAUDE_REF})['prompt'])
        reads = len(self.calls)
        self.chat.state({'source': CLAUDE_REF})
        self.assertEqual(len(self.calls), reads)
        self.terms.screens = [CLAUDE_PERMISSION, '> ']
        with patch.object(chat.time, 'sleep'):
            self.answer(1)
        self.terms.screen = '> '
        self.assertIsNone(self.chat.state({'source': CLAUDE_REF})['prompt'])
        self.assertEqual(len(self.calls), reads + 1)
        source = self.chat.source(CLAUDE_REF)[0]
        lock = self.chat._claude_lock_for(source)
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
        # One session's input lock only: another session's is its own.
        self.assertIs(self.chat._claude_lock_for(source), lock)
        self.assertIsNot(self.chat._claude_lock_for({**source, 'id': 'another-session', 'sessionId': None}), lock)
        # The same conversation named by a launched terminal's source (id = sessionId) shares it.
        conversation = {'host': 'h', 'agent': 'claude', 'id': 'agent-1234', 'sessionId': 'conversation-0001'}
        launched = {'host': 'h', 'agent': 'claude', 'home': '.claude', 'id': 'conversation-0001', 'sessionId': 'conversation-0001'}
        self.assertIs(self.chat._claude_lock_for(conversation), self.chat._claude_lock_for(launched))
        # A paste or another answer in progress on this session holds its terminal input lock.
        busy = Mock(acquire=Mock(return_value=False))
        with patch.object(self.chat, '_claude_lock_for', return_value=busy):
            self.terms.screen = CLAUDE_PERMISSION
            exc = self.refused(1, rid='answer-0002')
            self.assertEqual(exc.code, 'busy')
            with self.assertRaises(chat.PromptRejected):
                self.chat.prompt({'source': CLAUDE_REF, 'action': 'open'})
            busy.acquire.assert_called_with(timeout=30)
        self.assertEqual(len(self.terms.keys), 1)


@unittest.skipUnless(shutil.which('tmux'), 'tmux fixture requires installed tmux')
class MenuDemoTmuxTest(unittest.TestCase):
    """The --menu-demo program in a unique tmux server, read and answered through Chat."""
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        project = root / 'project'
        project.mkdir()
        socket = 'sb-test-' + secrets.token_hex(8)
        self.manager = terminal.TerminalManager(state_dir=root / 'terminals', socket_name=socket, max_terminals=1)
        # This unique test socket is the only server this fixture may stop.
        # tmux keeps the socket file of an ended server (cleanups run last first).
        self.addCleanup(lambda: (terminal.socket_dir() / socket).unlink(missing_ok=True))
        self.addCleanup(subprocess.run, [shutil.which('tmux'), '-L', socket, 'kill-server'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.addCleanup(self.manager.close)
        codex = {**REF, 'cwd': str(project), 'phase': 'needs_input', 'title': 'Menu fixture'}
        claude = {'agent': 'claude', 'id': CLAUDE_REF['id'], 'sessionId': CLAUDE_SID, 'kind': 'background',
                  'cwd': str(project), 'phase': 'needs_input', 'title': 'Claude menu fixture'}
        board = server.Board([HOST], 60, runner=lambda *a: {'codex': [codex], 'claude': [claude]})
        board.poll(HOST)
        self.flow = server.Workflow(board, self.manager, root)
        self.flow.transfers.rpc = lambda *a, **k: {'phase': 'needs_input', 'cwd': str(project), 'messages': [],
                                                   'capabilities': {'canSend': True}}
        self.chat = chat.Chat(board, self.flow, root)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'chat_preview_fixture_for_test', str(Path(__file__).with_name('chat_preview_fixture.py')))
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        self.demo = {'argv': [sys.executable, '-u', '-c', fixture.MENU_DEMO], 'cwd': str(project),
                     'env': {'PATH': os.environ.get('PATH', ''), 'LANG': 'en_US.UTF-8'}}

    def wait_prompt(self, predicate, source=REF):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.chat.cache.clear()
            prompt = self.chat.state({'source': source})['prompt']
            if predicate(prompt):
                return prompt
            time.sleep(0.1)
        self.fail('the demo menu did not reach the expected screen')

    def test_background_claude_menu_opens_through_the_real_attach_path(self):
        # The fixture's "질문 불러오기": plan, launch, nativeSource metadata, settle, capture.
        self.flow.profiles = lambda: [{'id': 'claude:default', 'agent': 'claude', 'home': '.claude',
                                       'label': 'Fixture Claude', 'available': True}]
        state = self.chat.state({'source': CLAUDE_REF})
        self.assertTrue(state['promptOpenable'])
        self.assertIsNone(state['prompt'])
        with patch('launch.build_launch', return_value=self.demo), \
                patch('launch.source_environment', return_value='default'):
            result = self.chat.prompt({'source': CLAUDE_REF, 'action': 'open', 'requestId': 'prompt-0001'})
        prompt = result['prompt']
        self.assertEqual((prompt['agent'], prompt['current']), ('claude', 1))
        self.assertEqual(prompt['question'], '검증 질문 #1\n\n계속 진행할까요?')
        native = self.manager.list()[0]['nativeSource']
        self.assertEqual((native['id'], native['kind']), (CLAUDE_REF['id'], 'background'))
        self.assertEqual(self.manager.list()[0]['id'], prompt['terminalId'])
        state = self.chat.state({'source': CLAUDE_REF})
        self.assertFalse(state['promptOpenable'])
        self.assertEqual(state['prompt']['fingerprint'], prompt['fingerprint'])
        result = self.chat.prompt({'source': CLAUDE_REF, 'action': 'answer', 'fingerprint': prompt['fingerprint'],
                                   'number': 1, 'requestId': 'answer-0001'})
        self.assertEqual(result['status'], 'answered')
        self.assertIn('선택 결과: 1. Yes (질문 #1)', self.manager.capture(prompt['terminalId']))

    def test_demo_menu_is_read_answered_with_arrow_keys_and_asks_again(self):
        created = self.manager.create('menu-demo', self.demo['argv'], self.demo['cwd'], self.demo['env'],
                                      {'nativeSource': {**REF, 'cwd': self.demo['cwd']}})
        self.tid = created['id']
        prompt = self.wait_prompt(lambda value: value is not None)
        self.assertEqual(prompt['question'], '검증 질문 #1\n\n계속 진행할까요?')
        self.assertEqual([(o['number'], o['label']) for o in prompt['options']], [(1, 'Yes'), (2, 'No')])
        self.assertEqual((prompt['current'], prompt['multi'], prompt['agent']), (1, False, 'codex'))
        self.assertEqual(prompt['terminalId'], self.tid)
        result = self.chat.prompt({'source': REF, 'action': 'answer', 'fingerprint': prompt['fingerprint'],
                                   'number': 2, 'requestId': 'answer-0001'})
        self.assertEqual((result['status'], result['prompt']), ('answered', None))
        self.assertIn('선택 결과: 2. No (질문 #1)', self.manager.capture(self.tid))
        following = self.wait_prompt(lambda value: value is not None and value['fingerprint'] != prompt['fingerprint'])
        self.assertIn('#2', following['question'])
        self.assertEqual(following['current'], 1)


class ManagedLaunchTest(unittest.TestCase):
    def test_new_codex_prompt_is_native_once_and_tui_only_attaches(self):
        class Client:
            def __init__(self): self.calls=[]
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def call(self,method,args):
                self.calls.append((method,args))
                if method=='thread/start': return {'thread':{'id':SID,'cwd':'/tmp/fixture'}}
                if method=='thread/read': return {'thread':{'id':SID,'turns':[{'id':'t1'}]}}
                return {'turn':{'id':'t1'}}
        client=Client()
        spec={'mode':'transfer','argv':['codex','--cd','/tmp/fixture','prompt'], 'cwd':'/tmp/fixture','env':{}}
        with tempfile.TemporaryDirectory() as folder, patch('transfer_native._socket_path',return_value=Path('/home/.codex/control.sock')),patch('native_chat._client',return_value=client):
            receipt=Path(folder)/'receipt.json'
            out=managed_launch.bind(spec,{'agent':'codex','home':'.codex'},'remote',receipt_path=receipt)
            reopened=managed_launch.bind(spec,{'agent':'codex','home':'.codex'},'remote',receipt_path=receipt)
            # The first prompt goes straight to turn/start; no board-side queue is involved.
            self.assertEqual([method for method,_ in client.calls],['thread/start','turn/start','thread/read'])
            self.assertEqual(client.calls[0],('thread/start',{'cwd':'/tmp/fixture','historyMode':'legacy'}))
            self.assertEqual(client.calls[1][1]['input'],[{'type':'text','text':'prompt'}])
            self.assertEqual(json.loads(receipt.read_text())['delivery'],'accepted')
            self.assertEqual(out['nativeSource']['id'],SID)
            self.assertEqual(out['argv'],reopened['argv'])
            self.assertNotIn('prompt',out['argv'])
            self.assertEqual(out['connectionMode'],'attach')
            self.assertEqual(receipt.stat().st_mode & 0o777,0o600)
    def test_unknown_initial_prompt_survives_terminal_reopen_without_resend(self):
        class Client:
            def __init__(self):self.calls=[]
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def call(self,method,args):
                self.calls.append(method)
                if method=='thread/start': return {'thread':{'id':SID,'cwd':'/tmp/fixture'}}
                raise OSError('response lost')
        client=Client()
        spec={'mode':'handoff','argv':['codex','prompt'],'cwd':'/tmp/fixture','env':{}}
        with tempfile.TemporaryDirectory() as folder, patch('transfer_native._socket_path',return_value=Path('/home/.codex/control.sock')),patch('native_chat._client',return_value=client):
            receipt=Path(folder)/'receipt.json'
            out=managed_launch.bind(spec,{'agent':'codex','home':'.codex'},'remote',receipt_path=receipt)
            managed_launch.mark_attached(out,'terminal1')
            self.assertNotIn('terminalId',json.loads(receipt.read_text()))
            again=managed_launch.bind(spec,{'agent':'codex','home':'.codex'},'remote',receipt_path=receipt)
            self.assertEqual(client.calls,['thread/start','turn/start'])
            self.assertEqual(out['nativeSource'],again['nativeSource'])
    def test_codex_bind_without_app_server_starts_nothing_when_it_cannot_start_one(self):
        # A board-local handoff also goes through bind: no daemon and no way to start one
        # refuses before any thread or first prompt exists.
        spec={'mode':'handoff','argv':['codex','prompt'],'cwd':'/tmp/fixture','env':{}}
        with tempfile.TemporaryDirectory() as folder, patch('managed_launch.app_server_running',return_value=False),patch('native_chat._client') as client, patch('subprocess.run') as run:
            with self.assertRaisesRegex(ValueError,'앱 서버를 시작하지 못했습니다'):
                managed_launch.bind(spec,{'agent':'codex','home':'.codex'},'remote',receipt_path=Path(folder)/'receipt.json')
            client.assert_not_called(); run.assert_not_called()
            self.assertFalse((Path(folder)/'receipt.json').exists())
    def test_claude_fixed_uuid_and_attach_unchanged(self):
        spec={'mode':'handoff','argv':['claude','prompt'],'cwd':'/tmp','env':{}}
        out=managed_launch.bind(spec,{'agent':'claude','home':'.claude'},'remote')
        self.assertEqual(out['argv'][1:3],['--session-id',out['nativeSource']['id']])
        spec['mode']='attach'; self.assertIs(managed_launch.bind(spec,{},'remote'),spec)
