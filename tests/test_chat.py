import base64
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote
from unittest.mock import patch

import attachments
import chat
import managed_launch
import server

SID = '019aaaaa-0000-7000-8000-000000000001'
HOST = {'name':'local','label':'Local','local':True}
REF = {'host':'local','agent':'codex','home':'.codex','id':SID}


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
                        'receipts':[{'requestId':args['requestId'],'delivery':'queued','confirmed':True,'queueId':'q1'}] if args.get('requestId') else []}
            return {'delivery':'queued','confirmed':True,'queueId':'q1'}
        self.flow.transfers.rpc=rpc
        self.chat=chat.Chat(self.board,self.flow,self.root)
    def tearDown(self): self.temp.cleanup()
    def test_upload_bytes_exact_and_bound_to_session(self):
        raw=b'\x00\xfffile\r\n'
        result=self.chat.upload(REF,'한글 파일.bin',raw)['attachment']
        self.assertEqual(Path(result['path']).read_bytes(),raw)
        self.assertEqual(result['sha256'],hashlib.sha256(raw).hexdigest())
        response=self.chat.send({'source':REF,'requestId':'request-0001','text':'확인','attachments':[result['id']]})
        self.assertEqual(response['status'],'queued')
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
        receipt=self.chat.receipt({'delivery':'queued','confirmed':True,'acknowledged':True,'readbackConfirmed':False},'request-0001')
        self.assertTrue(receipt['acknowledged'])
        self.assertFalse(receipt['readbackConfirmed'])
    def test_batch_receipts_use_one_read_and_never_enter_plain_state_cache(self):
        def rpc(host,op,args,timeout):
            self.calls.append((host['name'],op,args))
            return {'messages':[], 'receipts':[{'requestId':identifier,'delivery':'queued','confirmed':True,
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
    def test_queue_typed_fallback_and_inconsistent_completeness_are_unknown(self):
        row={'queueId':'queue-1','requestId':None,'text':'예약','contentAvailable':True,
             'textTruncated':False,'attachments':[],'status':'queued','confirmed':True,'readbackConfirmed':True}
        valid={'available':True,'complete':True,'total':1,'truncated':False}
        for state,rows in ((None,[]),([],[]),({'available':False,'complete':False,'truncated':False},[]),
                           ({**valid,'available':False},[row]),({**valid,'total':2},[row]),
                           ({**valid,'truncated':True},[row]),(valid,'invalid'),(valid,[None])):
            with self.subTest(state=state):
                queued,info=self.chat.queue_history({'queueState':state,'queuedMessages':rows})
                self.assertEqual(queued,[])
                self.assertFalse(info['available'])
                self.assertFalse(info['complete'])
                self.assertIsNone(info['total'])
    def test_queue_metadata_is_scrubbed_and_never_forwards_raw_native_inputs(self):
        row={'queueId':'queue-1','requestId':'request-0001','text':'예약 본문','contentAvailable':True,
             'textTruncated':False,'status':'queued','confirmed':True,'readbackConfirmed':True,
             'input':[{'url':'data:image/png;base64,PRIVATE'}],
             'attachments':[{'source':'native','kind':'image','name':'/example/private/picture.png','url':'PRIVATE','path':'PRIVATE'}]}
        result={'queuedMessages':[row],'queueState':{'available':True,'complete':True,'total':1,'truncated':False}}
        self.flow.transfers.rpc=lambda *a,**k:result
        state=self.chat.state({'source':REF})
        self.assertEqual(state['queuedMessages'][0]['text'],'예약 본문')
        self.assertEqual(state['queuedMessages'][0]['attachments'][0]['name'],'이미지')
        self.assertNotIn('PRIVATE',json.dumps(state))
        self.assertNotIn('/example/private',json.dumps(state))
        self.assertNotIn('input',state['queuedMessages'][0])
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


class ManagedLaunchTest(unittest.TestCase):
    def test_new_codex_prompt_is_native_once_and_tui_only_attaches(self):
        class Client:
            def __init__(self): self.calls=[]
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def call(self,method,args):
                self.calls.append((method,args))
                if method=='thread/start': return {'thread':{'id':SID,'cwd':'/tmp/fixture'}}
                if method=='thread/queue/add': return {'queuedSubmission':{'id':'q1','clientUserMessageId':args['clientUserMessageId']}}
                if method=='thread/read': return {'thread':{'id':SID,'turns':[{'id':'t1'}]}}
                return {'turn':{'id':'t1'}}
        client=Client()
        spec={'mode':'transfer','argv':['codex','--cd','/tmp/fixture','prompt'], 'cwd':'/tmp/fixture','env':{}}
        with tempfile.TemporaryDirectory() as folder, patch('transfer_native._socket_path',return_value=Path('/home/.codex/control.sock')),patch('native_chat._client',return_value=client):
            receipt=Path(folder)/'receipt.json'
            out=managed_launch.bind(spec,{'agent':'codex','home':'.codex'},'remote',receipt_path=receipt)
            reopened=managed_launch.bind(spec,{'agent':'codex','home':'.codex'},'remote',receipt_path=receipt)
            self.assertEqual(len(client.calls),4)
            self.assertEqual(client.calls[0],('thread/start',{'cwd':'/tmp/fixture','historyMode':'legacy'}))
            self.assertEqual(client.calls[1][1]['input'],[{'type':'text','text':'prompt'}])
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
            self.assertEqual(client.calls,['thread/start','thread/queue/add'])
            self.assertEqual(out['nativeSource'],again['nativeSource'])
    def test_claude_fixed_uuid_and_attach_unchanged(self):
        spec={'mode':'handoff','argv':['claude','prompt'],'cwd':'/tmp','env':{}}
        out=managed_launch.bind(spec,{'agent':'claude','home':'.claude'},'remote')
        self.assertEqual(out['argv'][1:3],['--session-id',out['nativeSource']['id']])
        spec['mode']='attach'; self.assertIs(managed_launch.bind(spec,{},'remote'),spec)
