import base64
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import quote
from unittest.mock import patch

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
    def test_batch_receipts_use_one_read_and_never_enter_plain_state_cache(self):
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
        self.assertEqual(self.chat.state({'source':REF})['receipts'],[])
        self.assertEqual(len(self.calls),2)
        for ids in (['invalid'], ['request-'+str(i) for i in range(21)], None):
            with self.assertRaises(ValueError):self.chat.state({'source':REF,'requestIds':ids})
        self.assertEqual(len(self.calls),2)
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
        self.assertEqual(set(state),{'route','phase','capability','messages','receipts','maxAttachmentBytes','requiresNativeTerminal'})
        self.assertEqual(set(state['capability']),{'supported','attachments','reason'})
        self.assertTrue(state['capability']['supported'])
        self.assertNotIn('PRIVATE',json.dumps(state))
        self.assertNotIn('queueId',state['receipts'][0])
        self.assertFalse(hasattr(chat.Chat,'queue_history'))
    def test_message_attachment_names_are_scrubbed_and_raw_native_fields_stay_metadata(self):
        result={'phase':'idle','capabilities':{'canSend':True},'messages':[{'role':'user','text':'본문',
                'attachments':[{'source':'native','kind':'image','name':'owner@example.com 사진.png','id':None}]}]}
        self.flow.transfers.rpc=lambda *a,**k:result
        state=self.chat.state({'source':REF})
        self.assertEqual(state['messages'][0]['attachments'][0]['name'],'[redacted] 사진.png')
        self.assertNotIn('owner@example.com',json.dumps(state))
    def test_state_is_exact_cached_and_offline_stops(self):
        first=self.chat.state({'source':REF})
        second=self.chat.state({'source':REF})
        self.assertEqual(first['route'],REF)
        self.assertEqual(len(self.calls),1)
        self.board.state['local']['ok']=False
        with self.assertRaises(RuntimeError): self.chat.state({'source':REF})
    def test_receipt_lookup_does_not_pollute_plain_state_cache(self):
        receipt=self.chat.state({'source':REF,'requestId':'request-0001'})
        self.assertEqual(receipt['receipts'][0]['requestId'],'request-0001')
        plain=self.chat.state({'source':REF})
        self.assertEqual(plain['receipts'],[])
        self.assertEqual(len(self.calls),2)
        self.assertEqual([args['action'] for _,_,args in self.calls],['read','read'])
        self.assertEqual(self.calls[0][2]['requestId'],'request-0001')
        self.assertNotIn('requestId',self.calls[1][2])
    def test_combined_missing_or_other_request_receipt_remains_unknown(self):
        for result in ({'messages':[]}, {'messages':[],'receipts':[{'requestId':'different-request','delivery':'completed','confirmed':True}]}):
            with self.subTest(result=result):
                calls=[]
                self.flow.transfers.rpc=lambda *a,**k: calls.append((a,k)) or result
                response=self.chat.state({'source':REF,'requestId':'request-0001'})
                self.assertEqual(response['receipts'][0]['status'],'unknown')
                self.assertFalse(response['receipts'][0]['confirmed'])
                self.assertEqual(len(calls),1)
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
            self.assertEqual(self.chat.state({'source':REF})['messages'][0]['text'],'newer')
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


class ChatHttpTest(ChatTest):
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


class FakeTerminals:
    """Board terminal manager boundary: no tmux, no CLI, only recorded calls."""
    def __init__(self):
        self.rows, self.pastes, self.log = [], [], []
        self.paste_error = None
        self.chunks = [{'after': 0, 'alive': True}]
        self.screen = '> '
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
        return self.screen
    def read(self, terminal_id, after=0, wait=20, epoch=None):
        self.log.append('read')
        time.sleep(min(wait, 0.02))
        return dict(self.chunks.pop(0) if len(self.chunks) > 1 else self.chunks[0])


class ChatClaudeTest(unittest.TestCase):
    """Claude input is typed by the board into its own attached terminal, never by the host RPC."""
    def setUp(self):
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
        self.phase, self.messages, self.calls = 'idle', [], []
        def rpc(host, op, args, timeout):
            self.calls.append((op, args.get('action')))
            if op == 'attachment':
                return attachments.receive(args)
            if args['action'] == 'send':
                raise AssertionError('Claude input must never use the host send RPC')
            if args['action'] == 'capabilities':
                return {'phase': self.phase, 'kind': self.row['kind'], 'cwd': str(self.project)}
            return {'phase': self.phase, 'cwd': str(self.project), 'messages': list(self.messages),
                    'requiresNativeTerminal': True,
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

    def test_state_capability_follows_board_terminal_background_and_needs_input(self):
        self.kind('interactive')
        state = self.state()
        self.assertFalse(state['capability']['supported'])
        self.assertEqual(state['capability']['reason'], chat.CLAUDE_EXTERNAL)
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
                self.assertFalse(state['requiresNativeTerminal'])
        self.terms.rows = []
        self.kind('background')
        self.assertTrue(self.state()['capability']['supported'])
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

    def test_background_without_terminal_attaches_exact_session_then_pastes(self):
        self.terms.chunks = [{'after': 10, 'alive': True}, {'after': 25, 'alive': True}, {'after': 25, 'alive': True}]
        def launch(plan_id, request_id):
            self.terms.log.append('launch')
            self.terminal()
            return {'terminal': {'id': TERMINAL_ID}}
        with patch.object(self.flow, 'plan', return_value={'allowed': True, 'id': 'plan-1'}) as plan, \
                patch.object(self.flow, 'launch', side_effect=launch) as launched:
            result = self.send(rid='request-0002', text='백그라운드 확인')
            plan.assert_called_once_with({'source': CLAUDE_REF, 'target': {
                'host': 'local', 'agent': 'claude', 'account': 'claude:default'}})
            launched.assert_called_once_with('plan-1', 'chat_' + hashlib.sha256(b'request-0002').hexdigest()[:40])
            self.assertEqual(result['status'], 'accepted')
            self.assertEqual(self.terms.pastes, [(TERMINAL_ID, '백그라운드 확인', True)])
            # The attach output must settle before the paste; paste is the last terminal action.
            self.assertEqual(self.terms.log[0], 'launch')
            self.assertGreaterEqual(self.terms.log.count('read'), 3)
            self.assertEqual(self.terms.log[-1], 'paste')
            # The attached terminal is reused for the next message.
            self.send(rid='request-0003', text='두 번째')
            plan.assert_called_once()
            launched.assert_called_once()
        self.assertEqual([text for _, text, _ in self.terms.pastes], ['백그라운드 확인', '두 번째'])

    def test_background_attach_failures_reject_without_reservation(self):
        cases = [({'allowed': False, 'reason': '연결 계획을 만들 수 없습니다.'}, None, '연결 계획을 만들 수 없습니다.'),
                 ({'allowed': True, 'id': 'plan-1'}, RuntimeError('터미널 실행 실패'), '터미널 실행 실패'),
                 ({'allowed': True, 'id': 'plan-1'}, None, '준비되지 않았습니다')]
        self.terms.chunks = [{'after': 0, 'alive': False}]
        for plan, error, reason in cases:
            with self.subTest(reason=reason):
                launch = {'side_effect': error} if error else {'return_value': {'terminal': {'id': TERMINAL_ID}}}
                with patch.object(self.flow, 'plan', return_value=plan), patch.object(self.flow, 'launch', **launch):
                    self.assertIn(reason, self.rejected())
                self.assertEqual(self.terms.pastes, [])
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

    def test_selection_menu_on_screen_or_unknown_state_never_types_into_terminal(self):
        self.terminal()
        self.phase = 'needs_input'
        for menu in ('Do you want to proceed?\n❯ 1. Yes\n  2. No', '│ ❯ 1. Yes, allow once'):
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
        self.assertEqual(self.terms.log[-2:], ['capture', 'paste'])

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
    def test_claude_fixed_uuid_and_attach_unchanged(self):
        spec={'mode':'handoff','argv':['claude','prompt'],'cwd':'/tmp','env':{}}
        out=managed_launch.bind(spec,{'agent':'claude','home':'.claude'},'remote')
        self.assertEqual(out['argv'][1:3],['--session-id',out['nativeSource']['id']])
        spec['mode']='attach'; self.assertIs(managed_launch.bind(spec,{},'remote'),spec)
