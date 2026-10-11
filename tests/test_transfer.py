import base64
import contextlib
import io
import json
import hashlib
import concurrent.futures
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import collector
import launch
import server
import transfer
from transfer import Transfers, WorkerError
import transfer_worker

PROFILE = {'id':'codex:.codex-isolated','agent':'codex','home':'.codex-isolated','label':'격리 환경','environment':'isolated','available':True}
HOSTS = [{'name':'remote','label':'원격 기기','local':True},
         {'name':'local','label':'로컬 기기','local':False,'ssh':'local','python':'/usr/bin/python3'}]


def raised_id(request):
    return hashlib.sha256(request.encode()).hexdigest()[:32]


class CoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.flow = Transfers(HOSTS, self.temp.name)
        self.source = {'host':'local','agent':'codex','home':'.codex-isolated','id':'a'*32,'cwd':'/work','phase':'working'}
        self.target = {'host':'remote','agent':'codex','account':PROFILE['id']}
        self.calls = []
        self.phase = 'idle'
        self.fingerprint = 'snapshot1'
        def rpc(host, op, args=None, timeout=30):
            self.calls.append((host['name'],op))
            self.flow.runtime_paths[host['name']] = '/safe/runtime.pyz'
            if op=='profiles': return {'profiles':[PROFILE]}
            if op=='interrupt': return {'confirmed':True,'safeToTransfer':True,'phase':'idle','revision':'state1'}
            if op=='state': return {'safeToTransfer':self.phase=='idle','revision':'state1'}
            if op=='inspect': return {'fingerprint':self.fingerprint}
            if op=='export': return {'sha256':'b'*64,'summary':{'fingerprint':'snapshot1','head':'c'*40,'sourceEnvironment':'isolated'}}
            if op=='cleanup': return {'ok':True}
            if op=='prepare':
                self.assertEqual(args['metadata']['sourceEnvironment'],'isolated')
                return {'cwd':'/new/work','warnings':[]}
            raise AssertionError(op)
        self.flow.rpc = rpc
        self.flow._copy = lambda *args: self.calls.append(('transport','copy'))
    def tearDown(self): self.temp.cleanup()
    def execute(self, request='request-1234'):
        return self.flow.execute(self.source,self.target,HOSTS[1],HOSTS[0],PROFILE,request,lambda:[{'role':'user','text':'이어가기'}])
    @contextmanager
    def native_prepare(self):
        home=Path(tempfile.mkdtemp(prefix='target-home-',dir=self.temp.name)).resolve()
        destination=home/'project';destination.mkdir()
        native_id='019aaaaa-0000-7000-8000-000000000001'
        calls=[]
        class Native:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,method,params):
                calls.append((method,params))
                if method=='thread/start':return {'thread':{'id':native_id,'cwd':str(destination)}}
                if method=='thread/read':return {'thread':{'id':native_id,'turns':[{'id':'fixture-turn'}]}}
                return {'turn':{'id':'fixture-turn'}}
        original=self.flow.rpc
        def rpc(host,op,args=None,timeout=30):
            if op!='prepare':return original(host,op,args,timeout)
            self.calls.append((host['name'],op))
            # Exercise the real worker and binder; only native IO and workspace
            # import/build boundaries are synthetic. No model is executed.
            return transfer_worker.rpc({'op':op,'args':args})
        spec={'mode':'transfer','argv':['/fixture/codex','fixture initial prompt'],
              'cwd':str(destination),'env':{},'handoffPath':str(home/'handoff.md')}
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path,'home',return_value=home))
            stack.enter_context(patch('transfer_native.profiles',return_value=[PROFILE]))
            stack.enter_context(patch('transfer_workspace.import_workspace',return_value={
                'cwd':str(destination),'root':str(destination)}))
            stack.enter_context(patch('launch.build_transferred_launch',return_value=spec))
            stack.enter_context(patch('transfer_native._socket_path',return_value=home/'fixture.sock'))
            stack.enter_context(patch('native_chat._client',return_value=Native()))
            stack.enter_context(patch.object(self.flow,'rpc',side_effect=rpc))
            yield calls
    def test_interrupt_export_copy_prepare_order_and_once(self):
        result=self.execute()
        self.assertEqual(self.calls,[('remote','profiles'),('local','interrupt'),('local','export'),('transport','copy'),('local','inspect'),('local','state'),('remote','prepare'),('local','cleanup'),('remote','cleanup')])
        self.assertEqual(result['destinationCwd'],'/new/work')
        self.assertEqual(result['argv'][-2],'exec')
        with self.assertRaises(ValueError): self.execute()
        self.assertEqual(sum(op=='interrupt' for _,op in self.calls),1)
    def test_source_changed_after_copy_never_returns_launch(self):
        self.fingerprint='changed'
        with self.assertRaisesRegex(ValueError,'파일이 바뀌'): self.execute()
        records=list((Path(self.temp.name)/'transfers').glob('*/transfer.json'))
        self.assertEqual(json.loads(records[0].read_text())['status'],'failed')
        self.assertEqual(json.loads(records[0].read_text())['targetPreparation'],'not_started')
        self.assertFalse(any(op=='prepare' for _,op in self.calls))
    def test_source_resumed_after_copy_never_returns_launch(self):
        self.phase='working'
        with self.assertRaisesRegex(ValueError,'원본 작업이 다시'): self.execute()
        self.assertFalse(any(op=='prepare' for _,op in self.calls))
    def test_real_prepare_materializes_native_initial_input_only_after_source_revalidation(self):
        with self.native_prepare() as calls:
            result=self.execute()
        self.assertEqual([method for method,_ in calls],['thread/start','turn/start','thread/read'])
        self.assertEqual(calls[1][1]['input'],[{'type':'text','text':'fixture initial prompt'}])
        self.assertEqual(result['connectionMode'],'attach')
        operations=[op for _,op in self.calls]
        self.assertLess(operations.index('state'),operations.index('prepare'))
    def test_copy_time_source_changes_never_prime_destination_native_input(self):
        for change in ('working','files'):
            self.phase='idle';self.fingerprint='snapshot1'
            with self.subTest(change=change),self.native_prepare() as calls:
                def copy(*args):
                    self.calls.append(('transport','copy'))
                    if change=='working':self.phase='working'
                    else:self.fingerprint='changed'
                self.flow._copy=copy
                with self.assertRaises(ValueError):self.execute('copy-change-'+change)
                self.assertEqual(calls,[])
                self.phase='idle';self.fingerprint='snapshot1'
    def test_prepare_response_loss_preserves_possible_destination_initialization_without_retry(self):
        with self.native_prepare() as calls:
            original=self.flow.rpc
            def rpc(host,op,args=None,timeout=30):
                result=original(host,op,args,timeout)
                if op=='prepare':raise RuntimeError('fixture response lost after native turn start')
                return result
            self.flow.rpc=rpc
            with self.assertRaisesRegex(RuntimeError,'대상.*확인'):self.execute()
            record=json.loads(next((Path(self.temp.name)/'transfers').glob('*/transfer.json')).read_text())
            self.assertEqual(record['targetPreparation'],'unknown')
            self.assertTrue(record['sourceStopped'])
            self.assertEqual(sum(method=='turn/start' for method,_ in calls),1)
            with self.assertRaises(ValueError):self.execute()
            self.assertEqual(sum(method=='turn/start' for method,_ in calls),1)
    def test_source_resumes_during_final_file_inspection_blocks_native_preparation(self):
        with self.native_prepare() as calls:
            original=self.flow.rpc
            def rpc(host,op,args=None,timeout=30):
                result=original(host,op,args,timeout)
                if op=='inspect':self.phase='working'
                return result
            self.flow.rpc=rpc
            with self.assertRaisesRegex(ValueError,'원본 작업이 다시'):self.execute()
            self.assertEqual(calls,[])
    def test_target_invalid_never_interrupts_source(self):
        self.flow.profiles=lambda *a,**k:[]
        with self.assertRaises(ValueError): self.execute()
        self.assertFalse(self.calls)
    def test_target_environment_change_blocks_before_interruption(self):
        self.flow.profiles=lambda *a,**k:[{**PROFILE,'environment':'different'}]
        with self.assertRaisesRegex(ValueError,'환경이 바뀌'):
            self.execute()
        self.assertFalse(self.calls)
    def test_exported_source_environment_cannot_cross_target_boundary(self):
        original=self.flow.rpc
        self.flow.rpc=lambda h,op,args=None,timeout=30: ({'sha256':'b'*64,'summary':{
            'fingerprint':'snapshot1','head':'c'*40,'sourceEnvironment':'different'}}
            if op=='export' else original(h,op,args,timeout))
        with self.assertRaisesRegex(ValueError,'환경 경계'):
            self.execute()
        self.assertFalse(any(op=='prepare' for _,op in self.calls))
    def test_interruption_unconfirmed_never_exports(self):
        old=self.flow.rpc
        self.flow.rpc=lambda h,op,args=None,timeout=30: {'confirmed':False,'safeToTransfer':False} if op=='interrupt' else old(h,op,args,timeout)
        with self.assertRaises(ValueError): self.execute()
        self.assertFalse(any(op=='export' for _,op in self.calls))
    def test_remote_terminal_command_has_absolute_binary_and_quoted_args(self):
        command=self.flow.command(HOSTS[1],['-I','/path with space/runtime.pyz','exec','f'*32],tty=True)
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertIn('-tt',command)
        self.assertIn("'/path with space/runtime.pyz'",command[-1])
    def test_profile_failure_does_not_retry_automatically(self):
        def fail(*a,**k):self.calls.append(('host','probe'));raise RuntimeError('offline')
        self.flow.rpc=fail
        for _ in range(2):
            with self.assertRaises(RuntimeError):self.flow.profiles(HOSTS[1])
        self.assertEqual(len(self.calls),1)
        with self.assertRaises(RuntimeError):self.flow.profiles(HOSTS[1],refresh=True)
        self.assertEqual(len(self.calls),2)
    def test_failed_postcopy_validation_cleans_both_hosts_and_preserves_error(self):
        self.fingerprint='changed'
        with self.assertRaisesRegex(ValueError,'파일이 바뀌'):self.execute()
        self.assertEqual([host for host,op in self.calls if op=='cleanup'],['local','remote'])
        record=json.loads(next((Path(self.temp.name)/'transfers').glob('*/transfer.json')).read_text())
        self.assertEqual(record['status'],'failed')
        self.assertFalse(record['archiveCleanupPending'])
    def test_failed_cleanup_is_pending_and_explicit_retry_keeps_failure_state(self):
        original=self.flow.rpc
        def fail(host,op,args=None,timeout=30):
            if op=='cleanup' and host['name']=='remote':raise RuntimeError('fixture cleanup busy')
            return original(host,op,args,timeout)
        self.flow.rpc=fail;self.fingerprint='changed'
        with self.assertRaises(ValueError):self.execute()
        path=next((Path(self.temp.name)/'transfers').glob('*/transfer.json'))
        record=json.loads(path.read_text())
        self.assertEqual(record['archiveCleanupHosts'],['remote'])
        self.assertTrue(record['archiveCleanupPending'])
        self.flow.rpc=original
        result=self.flow.cleanup_pending(record['id'])
        self.assertFalse(result['archiveCleanupPending'])
        self.assertEqual(json.loads(path.read_text())['status'],'failed')
    def test_unknown_prepare_defers_target_until_status_confirms_no_active_prepare(self):
        original=self.flow.rpc
        status=[WorkerError('이전 파일을 사용 중입니다.')]
        def lost(host,op,args=None,timeout=30):
            if op=='prepare':raise RuntimeError('fixture response lost')
            if op=='status':
                if isinstance(status[0],Exception):raise status[0]
                return status[0]
            return original(host,op,args,timeout)
        self.flow.rpc=lost
        with self.assertRaises(RuntimeError):self.execute()
        path=next((Path(self.temp.name)/'transfers').glob('*/transfer.json'))
        record=json.loads(path.read_text())
        self.assertEqual([host for host,op in self.calls if op=='cleanup'],['local'])
        # A busy helper lock or an inconsistent receipt keeps the archive.
        for reply in (WorkerError('이전 파일을 사용 중입니다.'),RuntimeError('fixture ssh down'),{'prepared':False,'started':True},{'prepared':'no'}):
            status[0]=reply
            result=self.flow.cleanup_pending(record['id'])
            self.assertEqual(result['deferredHosts'],['remote'],reply)
            self.assertTrue(result['archiveCleanupPending'])
        # Status succeeded with no launch or start receipt: no prepare holds the archive.
        status[0]={'prepared':False,'started':False}
        self.assertFalse(self.flow.cleanup_pending(record['id'])['archiveCleanupPending'])
        self.assertEqual([host for host,op in self.calls if op=='cleanup'],['local','remote'])
        self.assertEqual(json.loads(path.read_text())['targetPreparation'],'unknown')
    def test_pre_bind_worker_failure_is_failed_with_reason_and_cleans_target(self):
        original=self.flow.rpc
        for stage in ('validate','import','build'):
            self.calls.clear()
            def fail(host,op,args=None,timeout=30):
                if op=='prepare':
                    self.calls.append((host['name'],op))
                    raise WorkerError('fixture 대상 기기에 native CLI가 없습니다. ('+stage+')',stage)
                return original(host,op,args,timeout)
            self.flow.rpc=fail
            with self.subTest(stage=stage):
                with self.assertRaisesRegex(WorkerError,'native CLI가 없습니다') as raised:self.execute('pre-bind-'+stage)
                self.assertEqual(raised.exception.stage,stage)
                record=json.loads((Path(self.temp.name)/'transfers'/raised_id('pre-bind-'+stage)/'transfer.json').read_text())
                self.assertEqual(record['status'],'failed')
                self.assertEqual(record['targetPreparation'],'failed')
                self.assertIn('native CLI가 없습니다',record['error'])
                self.assertEqual([host for host,op in self.calls if op=='cleanup'],['local','remote'])
                self.assertFalse(record['archiveCleanupPending'])
    def test_bind_untagged_and_transport_prepare_failures_stay_unknown(self):
        original=self.flow.rpc
        for index,error in enumerate((WorkerError('fixture bind failed','bind'),WorkerError('fixture save failed','save'),
                                      WorkerError('fixture lock busy'),WorkerError('fixture earlier receipt','prepared'),
                                      RuntimeError('원격 실행 오류: fixture crash'))):
            self.calls.clear()
            def fail(host,op,args=None,timeout=30):
                if op=='prepare':raise error
                return original(host,op,args,timeout)
            self.flow.rpc=fail
            with self.subTest(error=error):
                with self.assertRaisesRegex(RuntimeError,'대상 준비 결과를 확인하지 못했습니다') as raised:self.execute('unknown-%d'%index)
                self.assertNotIsInstance(raised.exception,WorkerError)
                record=json.loads((Path(self.temp.name)/'transfers'/raised_id('unknown-%d'%index)/'transfer.json').read_text())
                self.assertEqual(record['targetPreparation'],'unknown')
                self.assertEqual([host for host,op in self.calls if op=='cleanup'],['local'])
    def test_profile_failure_expires_after_bounded_time(self):
        def fail(*a,**k):self.calls.append(('host','probe'));raise RuntimeError('offline')
        self.flow.rpc=fail
        with patch('transfer.time.monotonic',return_value=1000.0):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError,'offline'):self.flow.profiles(HOSTS[1])
        self.assertEqual(len(self.calls),1)
        with patch('transfer.time.monotonic',return_value=1000.0+transfer.PROFILE_FAILURE_TTL+1):
            with self.assertRaises(RuntimeError):self.flow.profiles(HOSTS[1])
        self.assertEqual(len(self.calls),2)
    def test_handoff_metadata_omits_preview_size_listing(self):
        original=self.flow.rpc
        seen={}
        def rpc(host,op,args=None,timeout=30):
            if op=='export':return {'sha256':'b'*64,'summary':{'fingerprint':'snapshot1','head':'c'*40,'sourceEnvironment':'isolated',
                                    'largestFiles':[{'path':'big.bin','bytes':9}],'excludedSensitive':['token.json']}}
            if op=='prepare':seen.update(args['metadata']['workspace'])
            return original(host,op,args,timeout)
        self.flow.rpc=rpc
        self.execute()
        self.assertNotIn('largestFiles',seen)
        self.assertEqual(seen['excludedSensitive'],['token.json'])
    def test_active_copy_cannot_be_cleaned_in_rpc_gap(self):
        entered,release=threading.Event(),threading.Event()
        identifier=hashlib.sha256(b'active-copy-fixture').hexdigest()[:32]
        archive=Path(self.temp.name)/'transfers'/identifier/'workspace.tar.gz'
        def copy(*args):
            archive.write_bytes(b'fixture active archive');archive.chmod(0o600)
            entered.set()
            if not release.wait(3):raise RuntimeError('fixture timeout')
            archive.unlink()
        self.flow._copy=copy
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            active=pool.submit(self.execute,'active-copy-fixture')
            try:
                self.assertTrue(entered.wait(3))
                with self.assertRaisesRegex(RuntimeError,'진행 중'):self.flow.cleanup_pending(identifier)
                self.assertTrue(archive.exists())
                self.assertFalse(any(op=='cleanup' for _,op in self.calls))
            finally:release.set()
            active.result(3)
    def test_cleanup_and_mark_started_are_serialized_without_overwriting_terminal_id(self):
        result=self.execute()
        identifier=result['transferId'];path=Path(self.temp.name)/'transfers'/identifier/'transfer.json'
        record=json.loads(path.read_text());record.update(archiveCleanupHosts=['local'],archiveCleanupPending=True)
        transfer_worker.save(path,record)
        original=self.flow.rpc;entered,release,marking=threading.Event(),threading.Event(),threading.Event()
        def rpc(host,op,args=None,timeout=30):
            if op=='cleanup':
                entered.set()
                if not release.wait(3):raise RuntimeError('fixture timeout')
            return original(host,op,args,timeout)
        self.flow.rpc=rpc
        def mark():
            marking.set();self.flow.mark_started(identifier,'a'*32)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            clean=pool.submit(self.flow.cleanup_pending,identifier)
            try:
                self.assertTrue(entered.wait(3))
                started=pool.submit(mark)
                self.assertTrue(marking.wait(3));self.assertFalse(started.done())
            finally:release.set()
            clean.result(3);started.result(3)
        saved=json.loads(path.read_text())
        self.assertEqual(saved['status'],'started');self.assertEqual(saved['terminalId'],'a'*32)
        self.assertFalse(saved['archiveCleanupPending'])
    def test_stale_coordinator_archive_is_exact_private_regular_cleanup_only(self):
        identifier=self.execute()['transferId'];folder=Path(self.temp.name)/'transfers'/identifier
        path=folder/'transfer.json';record=json.loads(path.read_text());record['status']='copying'
        transfer_worker.save(path,record)
        archive=folder/'workspace.tar.gz';archive.write_bytes(b'fixture crash archive');archive.chmod(0o600)
        self.assertFalse(self.flow.cleanup_pending(identifier)['archiveCleanupPending'])
        self.assertFalse(archive.exists())
        external=Path(self.temp.name)/'original';external.write_bytes(b'preserve');external.chmod(0o600)
        archive.symlink_to(external)
        with self.assertRaises(ValueError):self.flow.cleanup_pending(identifier)
        self.assertEqual(external.read_bytes(),b'preserve')


class WorkflowTransferTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.rows={name:{'agent':'codex','id':('a' if name=='local' else 'b')*32,'home':'.codex-isolated',
                       'cwd':'/Users/test/Isolated/work','title':'검증','phase':'working'} for name in ('remote','local')}
        def runner(host,args,timeout):
            if args[0]=='snapshot':return {'codex':[self.rows[host['name']]],'claude':[],'errors':[]}
            return {'messages':[{'role':'user','text':'최근 작업'}]}
        self.board=server.Board(HOSTS,60,runner)
        for h in HOSTS:self.board.poll(h)
        class Terminals:
            max_terminals=4
            def __init__(self):self.records=[]
            def list(self):return list(self.records)
            @contextmanager
            def reserve(self,key):
                if sum(bool(row.get('alive')) for row in self.records)>=self.max_terminals:
                    raise ValueError('열린 터미널이 가득 찼습니다.')
                yield
            def create(self,key,argv,cwd,env,metadata):
                t={'id':str(len(self.records)+1)*32,'key':key,'alive':True,**metadata};self.records.append(t);return t
        self.terms=Terminals();self.flow=server.Workflow(self.board,self.terms,self.temp.name)
        self.flow.profiles=lambda:[PROFILE]
        self.executions=[]
        self.flow.transfers.profiles=lambda *a,**k:[PROFILE]
        self.flow.transfers.preview=lambda *a:{'requiresInterrupt':True,'workspace':{'fileCount':2,'bytes':100,'sourceEnvironment':'isolated'}}
        def execute(source,target,sourcehost,targethost,profile,request,read):
            self.assertEqual(read()[0]['text'],'최근 작업')
            self.executions.append((sourcehost['name'],targethost['name']))
            return {'argv':['/usr/bin/true'],'cwd':'/tmp','env':{},'transferId':'f'*32,'destinationCwd':'/new/work'}
        self.flow.transfers.execute=execute;self.flow.transfers.mark_started=lambda *a:None
        self.which=patch('server.shutil.which',return_value='/usr/bin/tmux');self.which.start()
    def tearDown(self):self.which.stop();self.temp.cleanup()
    def test_both_directions_allow_working_and_start_on_target(self):
        for source,target in (('local','remote'),('remote','local')):
            ref={k:self.rows[source][k] for k in ('agent','id','home')};ref['host']=source
            plan=self.flow.plan({'source':ref,'target':{'host':target,'agent':'codex','account':PROFILE['id']}})
            self.assertTrue(plan['allowed'],plan['reason']);self.assertEqual(plan['mode'],'transfer')
            self.assertTrue(plan['transfer']['requiresInterrupt'])
            result=self.flow.launch(plan['id'],'request-'+source)
            self.assertEqual(result['terminal']['host'],target)
            self.assertEqual(result['terminal']['sourceHost'],source)
            again=self.flow.launch(plan['id'],'request-'+source)
            self.assertEqual(again,result)
        self.assertEqual(self.executions,[('local','remote'),('remote','local')])
    def test_concurrent_route_launches_transfer_once(self):
        import concurrent.futures
        import threading
        entered, release = threading.Event(), threading.Event()
        old = self.flow.transfers.execute
        def delayed(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return old(*args)
        self.flow.transfers.execute = delayed
        ref={'host':'local',**{k:self.rows['local'][k] for k in ('agent','id','home')}}
        plan=self.flow.plan({'source':ref,'target':{'host':'remote','agent':'codex','account':PROFILE['id']}})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.flow.launch,plan['id'],'concurrent-first')
            self.assertTrue(entered.wait(2))
            second=pool.submit(self.flow.launch,plan['id'],'concurrent-second')
            release.set()
            a,b=first.result(3),second.result(3)
        self.assertEqual(a['terminal']['id'],b['terminal']['id'])
        self.assertEqual(len(self.executions),1)
        self.assertTrue(b['reused'])
    def test_offline_target_blocks_before_interruption(self):
        self.board.state['remote']['ok']=False
        ref={'host':'local',**{k:self.rows['local'][k] for k in ('agent','id','home')}}
        plan=self.flow.plan({'source':ref,'target':{'host':'remote','agent':'codex','account':PROFILE['id']}})
        self.assertFalse(plan['allowed']);self.assertFalse(self.executions)
    def test_capacity_blocks_before_interruption(self):
        self.terms.max_terminals=0
        ref={'host':'local',**{k:self.rows['local'][k] for k in ('agent','id','home')}}
        plan=self.flow.plan({'source':ref,'target':{'host':'remote','agent':'codex','account':PROFILE['id']}})
        with self.assertRaisesRegex(ValueError,'가득'):self.flow.launch(plan['id'],'capacity-test')
        self.assertFalse(self.executions)
    def test_imported_claude_environment_context_controls_target_profile(self):
        source={'host':'local','hostLabel':'로컬 기기','agent':'claude','home':'.claude',
                'id':'claude-id','cwd':'/Users/other/.local/share/sessionholic/workspaces/'+'a'*32+'/project',
                'phase':'idle','online':True}
        self.board.source=lambda ref:source
        self.flow.transfers.preview=lambda *a:{'requiresInterrupt':False,'workspace':{'sourceEnvironment':'isolated'}}
        default_environment={'id':'codex:.codex','agent':'codex','home':'.codex','available':True}
        self.flow.profiles=lambda:[PROFILE,default_environment]
        def plan(account):
            return self.flow.plan({'source':{},'target':{'host':'remote','agent':'codex','account':account}})
        self.assertTrue(plan(PROFILE['id'])['allowed'])
        self.assertFalse(plan(default_environment['id'])['allowed'])
        self.assertFalse(self.executions)


class WorkerCleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        self.home_patch=patch.object(Path,'home',return_value=self.home)
        self.home_patch.start();self.addCleanup(self.home_patch.stop)
        self.identifier='a'*32
        self.folder=transfer_worker.job(self.identifier)
        (self.folder/'export').mkdir(mode=0o700)
        self.archive=self.file(self.folder/'export'/('workspace-'+'b'*32+'.tar.gz'))
        self.incoming=self.file(self.folder/'incoming.tar.gz')
    def file(self,path):
        path.write_bytes(b'fixture');path.chmod(0o600);return path
    def cleanup(self):return transfer_worker.rpc({'op':'cleanup','args':{'transferId':self.identifier}})
    def test_cleanup_removes_only_exact_regular_helper_archives(self):
        temporary=self.file(self.folder/('incoming.'+'c'*12+'.tmp'))
        preserved=[self.file(self.folder/name) for name in ('launch.json','handoff.md.native.json','transfer.json','started.json')]
        preserved.append(self.file(self.folder/'export'/'unrelated-file.txt'))
        workdir=self.home/'.local/share/sessionholic/workspaces'/self.identifier
        workdir.mkdir(parents=True)
        preserved.append(self.file(workdir/'original.txt'))
        result=self.cleanup()
        self.assertEqual(result['removed'],3)
        self.assertFalse(any(path.exists() for path in (self.archive,self.incoming,temporary)))
        self.assertTrue(all(path.exists() for path in preserved))
    def test_busy_operation_never_unlinks_files_in_use(self):
        with transfer_worker.job_operation(self.identifier):
            with self.assertRaisesRegex(ValueError,'사용 중'):self.cleanup()
            self.assertTrue(self.archive.exists());self.assertTrue(self.incoming.exists())
        self.assertEqual(self.cleanup()['removed'],2)
    def test_exact_archive_symlink_is_rejected_and_external_target_preserved(self):
        self.incoming.unlink()
        external=self.file(self.home/'external')
        self.incoming.symlink_to(external)
        with self.assertRaises(ValueError):self.cleanup()
        self.assertEqual(external.read_bytes(),b'fixture')
    def test_export_without_receipt_is_still_cleaned_after_source_verification_failure(self):
        self.assertFalse((self.folder/'export.json').exists())
        self.assertEqual(self.cleanup()['removed'],2)


class RpcOutcomeTest(unittest.TestCase):
    """Explicit helper failures are distinguishable from transport failures."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.flow=Transfers(HOSTS,self.temp.name)
    def reply(self,returncode,stdout=b'',stderr=b'',host=HOSTS[1]):
        done=subprocess.CompletedProcess([],returncode,stdout,stderr)
        with patch('transfer.subprocess.run',return_value=done):
            return self.flow.rpc(host,'prepare',{'transferId':'a'*32})
    def test_completed_helper_failure_is_worker_error_with_scrubbed_reason_and_stage(self):
        secret='sk-ant-api03-'+'x'*40
        stdout=json.dumps({'ok':False,'error':'준비 실패 '+secret,'stage':'import'}).encode()
        with self.assertRaises(WorkerError) as raised:self.reply(0,stdout)
        self.assertIsInstance(raised.exception,RuntimeError)
        self.assertEqual(raised.exception.stage,'import')
        self.assertIn('준비 실패',str(raised.exception));self.assertNotIn(secret,str(raised.exception))
        with self.assertRaises(WorkerError) as untagged:self.reply(0,b'{"ok":false,"error":"busy"}')
        self.assertIsNone(untagged.exception.stage)
    def test_ssh_exit_255_is_a_connection_failure_not_a_worker_error(self):
        with self.assertRaisesRegex(RuntimeError,'SSH 연결을 확인') as raised:self.reply(255,b'',b'ssh: connect to host local port 22: Connection refused\n')
        self.assertNotIsInstance(raised.exception,WorkerError)
    def test_other_exit_reports_scrubbed_last_stderr_line(self):
        secret='ghp_'+'a'*36
        stderr=('Traceback (most recent call last):\n  File "x", line 1\nValueError: boom '+secret+'\n').encode()
        with self.assertRaises(RuntimeError) as raised:self.reply(1,b'',stderr)
        self.assertNotIsInstance(raised.exception,WorkerError)
        self.assertTrue(str(raised.exception).startswith('원격 실행 오류: ValueError: boom'))
        self.assertNotIn(secret,str(raised.exception))
        with self.assertRaisesRegex(RuntimeError,'원격 실행 오류: 종료 코드 3'):self.reply(3)
        # A local helper has no SSH layer; 255 is an ordinary exit status there.
        with self.assertRaisesRegex(RuntimeError,'원격 실행 오류') as local:self.reply(255,b'',b'fatal\n',host=HOSTS[0])
        self.assertNotIsInstance(local.exception,WorkerError)
    def test_unreadable_reply_is_not_a_worker_error(self):
        for stdout in (b'not json',b'[]'):
            with self.assertRaisesRegex(RuntimeError,'읽지 못했') as raised:self.reply(0,stdout)
            self.assertNotIsInstance(raised.exception,WorkerError)


class WorkerPrepareStageTest(unittest.TestCase):
    """The helper reports which prepare step failed; pre-bind failures leave nothing behind."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        for context in (patch.object(Path,'home',return_value=self.home),patch('transfer_native.profiles',return_value=[PROFILE])):
            context.start();self.addCleanup(context.stop)
        self.identifier='d'*32
        self.workspaces=self.home/'.local/share/sessionholic/workspaces'
    def main(self,args=None):
        request={'op':'prepare','args':{'transferId':self.identifier,'sha256':'e'*64,'metadata':{},'messages':[],
                 'source':{},'target':{'account':PROFILE['id'],'agent':'codex','host':'remote'},**(args or {})}}
        output=io.StringIO()
        stdin=io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))
        with patch.object(sys,'argv',['worker.pyz']),patch.object(sys,'stdin',stdin),contextlib.redirect_stdout(output):
            transfer_worker.main()
        return json.loads(output.getvalue())
    def imported(self,*args):
        project=self.workspaces/self.identifier/'project';project.mkdir(parents=True)
        return {'cwd':str(project),'root':str(project)}
    def test_validate_and_import_failures_are_tagged(self):
        reply=self.main({'target':{'account':'codex:.codex-missing','agent':'codex','host':'remote'}})
        self.assertEqual((reply['ok'],reply['stage']),(False,'validate'))
        reply=self.main()
        self.assertEqual((reply['ok'],reply['stage']),(False,'import'))
        self.assertIn('번들',reply['error'])
    def test_build_failure_is_tagged_and_discards_the_fresh_import(self):
        with patch('transfer_workspace.import_workspace',side_effect=self.imported),\
             patch('launch.build_transferred_launch',side_effect=ValueError('이 호스트에 native CLI가 없습니다.')):
            reply=self.main()
        self.assertEqual((reply['ok'],reply['stage']),(False,'build'))
        self.assertIn('native CLI',reply['error'])
        self.assertFalse((self.workspaces/self.identifier).exists())
        self.assertTrue(self.workspaces.is_dir())
    def test_app_server_start_failure_is_a_build_failure_and_discards_the_import(self):
        spec={'mode':'transfer','argv':['/fixture/codex','prompt'],'cwd':'/fixture','env':{}}
        with patch('transfer_workspace.import_workspace',side_effect=self.imported),\
             patch('launch.build_transferred_launch',return_value=spec),\
             patch('managed_launch.app_server_running',return_value=False),\
             patch('managed_launch.bind') as bind:
            reply=self.main()
        self.assertEqual((reply['ok'],reply['stage']),(False,'build'))
        self.assertIn('앱 서버를 시작하지 못했습니다',reply['error'])
        bind.assert_not_called()
        self.assertFalse((self.workspaces/self.identifier).exists())
    def test_bind_failure_is_tagged_and_keeps_the_import(self):
        spec={'mode':'transfer','argv':['/fixture/codex','prompt'],'cwd':'/fixture','env':{}}
        with patch('transfer_workspace.import_workspace',side_effect=self.imported),\
             patch('launch.build_transferred_launch',return_value=spec),\
             patch('managed_launch.ensure_app_server',return_value=False),\
             patch('managed_launch.bind',side_effect=RuntimeError('fixture native thread result unknown')):
            reply=self.main()
        self.assertEqual((reply['ok'],reply['stage']),(False,'bind'))
        self.assertTrue((self.workspaces/self.identifier/'project').is_dir())
        self.assertFalse((transfer_worker.job(self.identifier)/'launch.json').exists())
    def test_existing_launch_receipt_is_never_reported_as_pre_bind(self):
        transfer_worker.save(transfer_worker.job(self.identifier)/'launch.json',{'argv':['/fixture']})
        reply=self.main()
        self.assertEqual((reply['ok'],reply['stage']),(False,'prepared'))
        self.assertNotIn(reply['stage'],transfer.PRE_BIND_STAGES)


class RuntimeInstallTest(unittest.TestCase):
    """The helper archive is reproducible and installed atomically on the host."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        env=patch.dict(os.environ,{'HOME':str(self.home)});env.start();self.addCleanup(env.stop)
        self.host={'name':'self','label':'이 기기','local':True}
        self.flow=Transfers([self.host],self.home/'board-state')
        self.root=self.home/'.local/state/sessionholic-transfer-runtime'
    def status(self):
        return self.flow.rpc(self.host,'status',{'transferId':'f'*32},timeout=30)
    def test_same_sources_produce_the_same_runtime_sha_at_any_time(self):
        first=Transfers([],self.home/'one').runtime()
        with patch('time.time',return_value=time.time()+10**8):
            second=Transfers([],self.home/'two').runtime()
        self.assertEqual(first['sha256'],second['sha256'])
        archive=zipfile.ZipFile(io.BytesIO(base64.b64decode(first['runtime'])))
        self.assertEqual({info.date_time for info in archive.infolist()},{(1980,1,1,0,0,0)})
        self.assertIn('__main__.py',archive.namelist())
    def test_helper_is_reused_reinstalled_when_corrupt_and_stale_versions_removed(self):
        self.assertEqual(self.status(),{'prepared':False,'started':False})
        current=self.root/('worker-'+self.flow.runtime()['sha256']+'.pyz')
        self.assertEqual(self.flow.runtime_paths['self'],str(current))
        self.status()
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),[current.name])
        current.write_bytes(b'corrupt helper')
        self.assertEqual(self.status(),{'prepared':False,'started':False})
        self.assertEqual(hashlib.sha256(current.read_bytes()).hexdigest(),self.flow.runtime()['sha256'])
        current.unlink();current.symlink_to(self.home/'elsewhere.pyz')
        self.status()
        self.assertFalse(current.is_symlink())
        self.assertEqual(current.stat().st_mode&0o777,0o600)
        stale,fresh=self.root/('worker-'+'0'*64+'.pyz'),self.root/('worker-'+'1'*64+'.pyz')
        leftover=self.root/('.worker-'+'2'*64+'.'+'3'*12+'.tmp')
        unrelated=self.root/'notes.txt'
        for path in (stale,fresh,leftover,unrelated):
            path.write_bytes(b'fixture');path.chmod(0o600)
        old=time.time()-3*86400
        for path in (stale,leftover,unrelated):os.utime(path,(old,old))
        self.status()
        self.assertFalse(stale.exists());self.assertFalse(leftover.exists())
        self.assertTrue(fresh.exists());self.assertTrue(unrelated.exists());self.assertTrue(current.exists())
    def recorded(self):
        """Run real bootstrap processes while recording whether each one carried the helper."""
        sent,real=[],subprocess.run
        def run(*args,**kwargs):
            sent.append('runtime' in json.loads(kwargs['input']))
            return real(*args,**kwargs)
        return sent,patch('transfer.subprocess.run',side_effect=run)
    def test_helper_written_by_the_previous_bootstrap_is_reused(self):
        # An earlier board wrote worker-<sha>.pyz directly with O_EXCL; the same bytes are accepted as is.
        sha=self.flow.runtime()['sha256']
        self.root.mkdir(parents=True,mode=0o700)
        current=self.root/('worker-'+sha+'.pyz')
        current.write_bytes(base64.b64decode(self.flow.runtime()['runtime']));current.chmod(0o600)
        before=current.stat().st_ino
        sent,recording=self.recorded()
        with recording:
            self.assertEqual(self.status(),{'prepared':False,'started':False})
        self.assertEqual(sent,[False])
        self.assertEqual(current.stat().st_ino,before)
    def test_installed_helper_is_reached_with_its_sha_only(self):
        sent,recording=self.recorded()
        with recording:
            # A fresh host is asked with the SHA first, answers need-runtime, then gets the archive.
            self.assertEqual(self.status(),{'prepared':False,'started':False})
            self.assertEqual(sent,[False,True])
            self.assertIs(self.flow.runtime_installed['self'],True)
            del sent[:]
            self.assertEqual(self.status(),{'prepared':False,'started':False})
            self.assertEqual(sent,[False])
            # Another coordinator process (a restart) finds the installed helper by SHA alone.
            restarted=Transfers([self.host],self.home/'board-state')
            self.assertEqual(restarted.rpc(self.host,'status',{'transferId':'f'*32}),{'prepared':False,'started':False})
            self.assertEqual(sent,[False,False])
            # A helper removed on the host is reinstalled after one explicit need-runtime reply.
            current=self.root/('worker-'+self.flow.runtime()['sha256']+'.pyz')
            current.unlink();del sent[:]
            self.status()
            self.assertEqual(sent,[False,True])
            self.assertEqual(hashlib.sha256(current.read_bytes()).hexdigest(),self.flow.runtime()['sha256'])
    def test_sha_only_bootstrap_never_runs_or_installs_a_missing_helper(self):
        runtime=self.flow.runtime()
        payload={'sha256':runtime['sha256'],'request':{'op':'status','args':{'transferId':'f'*32}}}
        done=subprocess.run([sys.executable,'-I','-c',transfer.BOOTSTRAP],input=json.dumps(payload).encode(),
                            capture_output=True,timeout=30)
        self.assertEqual(done.returncode,0)
        self.assertEqual(json.loads(done.stdout),{'ok':False,'needRuntime':True,'sha256':runtime['sha256']})
        self.assertEqual(list(self.root.iterdir()),[])
        # A runtime that does not match its SHA is still refused before anything is written.
        payload.update(runtime=base64.b64encode(b'not the helper').decode())
        done=subprocess.run([sys.executable,'-I','-c',transfer.BOOTSTRAP],input=json.dumps(payload).encode(),
                            capture_output=True,timeout=30)
        self.assertNotEqual(done.returncode,0)
        self.assertEqual(list(self.root.iterdir()),[])


class RuntimeMemoryTest(unittest.TestCase):
    """Per-host memory of the installed helper decides whether the archive is sent."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.flow=Transfers(HOSTS,self.temp.name)
        self.sha=self.flow.runtime()['sha256']
        self.sent,self.replies=[],[]
        def run(command,input=None,**kwargs):
            payload=json.loads(input)
            self.assertEqual(payload['sha256'],self.sha)
            self.sent.append(('runtime' in payload,command[command.index('--')+1] if '--' in command else 'local'))
            reply=self.replies.pop(0)
            if isinstance(reply,Exception):raise reply
            return subprocess.CompletedProcess(command,0,json.dumps(reply).encode(),b'')
        patcher=patch('transfer.subprocess.run',side_effect=run);patcher.start();self.addCleanup(patcher.stop)
    def ok(self,result=None):
        return {'ok':True,'result':result or {},'runtime':'/fixture/worker-'+self.sha+'.pyz'}
    def need(self):
        return {'ok':False,'needRuntime':True,'sha256':self.sha}
    def test_need_runtime_resends_the_archive_once_and_remembers_the_host(self):
        remote=HOSTS[1]
        self.replies=[self.need(),self.ok({'step':1}),self.ok({'step':2})]
        self.assertEqual(self.flow.rpc(remote,'status',{}),{'step':1})
        self.assertEqual(self.flow.rpc(remote,'status',{}),{'step':2})
        self.assertEqual(self.sent,[(False,'local'),(True,'local'),(False,'local')])
        self.assertEqual(self.flow.runtime_installed,{'local':True})
        # Memory is per host: another host starts with its own SHA-first probe.
        self.replies=[self.ok()]
        self.flow.rpc(HOSTS[0],'status',{})
        self.assertEqual(self.sent[-1],(False,'local'))
        self.assertEqual(self.flow.runtime_installed,{'local':True,'remote':True})
    def test_need_runtime_forgets_the_install_until_a_full_copy_is_confirmed(self):
        remote=HOSTS[1]
        self.replies=[self.ok(),self.need(),RuntimeError('fixture transport')]
        self.flow.rpc(remote,'status',{})
        with self.assertRaisesRegex(RuntimeError,'fixture transport'):
            self.flow.rpc(remote,'status',{})
        self.assertIs(self.flow.runtime_installed['local'],False)
        # Known to be missing: the next call sends the archive at once, without another probe.
        self.replies=[self.ok({'again':True})]
        self.assertEqual(self.flow.rpc(remote,'status',{}),{'again':True})
        self.assertEqual(self.sent,[(False,'local'),(False,'local'),(True,'local'),(True,'local')])
        self.assertIs(self.flow.runtime_installed['local'],True)
    def test_need_runtime_for_another_sha_or_after_a_full_copy_is_an_error(self):
        remote=HOSTS[1]
        self.replies=[{'ok':False,'needRuntime':True,'sha256':'0'*64}]
        with self.assertRaises(WorkerError):
            self.flow.rpc(remote,'status',{})
        self.replies=[self.need(),self.need()]
        with self.assertRaisesRegex(RuntimeError,'도우미를 설치하지 못했습니다'):
            self.flow.rpc(remote,'status',{})
        self.assertEqual(self.sent[-2:],[(False,'local'),(True,'local')])


class SshReuseTest(unittest.TestCase):
    def setUp(self):
        # macOS limits socket paths to 104 bytes, so the reusable case needs a short base.
        self.base=Path(tempfile.mkdtemp(prefix='sh',dir='/tmp'))
        self.addCleanup(shutil.rmtree,self.base,True)
    def options(self,command):
        return [command[index+1] for index,value in enumerate(command[:-2]) if value=='-o']
    def test_private_control_socket_is_shared_by_rpc_and_terminal_commands(self):
        flow=Transfers(HOSTS,self.base)
        for tty in (False,True):
            command=flow.command(HOSTS[1],['-I','/fixture/worker.pyz','exec','f'*32],tty=tty)
            options=self.options(command)
            self.assertIn('ControlMaster=auto',options);self.assertIn('ControlPersist=60',options)
            self.assertIn('ControlPath='+str(self.base/'ssh'/'cm-%C'),options)
            self.assertLess(command.index('ControlPath='+str(self.base/'ssh'/'cm-%C')),command.index('--'))
        self.assertEqual((self.base/'ssh').stat().st_mode&0o777,0o700)
        (self.base/'ssh').chmod(0o755)
        flow.command(HOSTS[1],['x'])
        self.assertEqual((self.base/'ssh').stat().st_mode&0o777,0o700)
        self.assertEqual(flow.command(HOSTS[0],['x']),[sys.executable,'x'])
    def test_control_socket_is_omitted_when_unsafe_or_missing(self):
        missing=self.base/'missing'
        linked=self.base/'linked';linked.mkdir();(linked/'ssh').symlink_to(self.base)
        for base in (missing,linked):
            with self.subTest(base=base.name[:8]):
                command=Transfers(HOSTS,base).command(HOSTS[1],['x'])
                self.assertFalse(any(value.startswith('Control') for value in self.options(command)))
                self.assertIn('BatchMode=yes',self.options(command))
        self.assertFalse(missing.exists())
    def test_long_state_paths_share_the_collection_fallback_socket_folder(self):
        import server
        long_base=self.base/('d'*60);long_base.mkdir()
        with patch.object(server,'SSH_FALLBACK_ROOT',self.base):
            options=self.options(Transfers(HOSTS,long_base).command(HOSTS[1],['x']))
        fallback=self.base/('sessionholic-%d'%os.getuid())
        self.assertIn('ControlPath='+str(fallback/'cm-%C'),options)
        self.assertEqual(fallback.stat().st_mode&0o777,0o700)
        self.assertFalse((long_base/'ssh').exists())


@unittest.skipUnless(shutil.which('git'),'requires local git')
class LocalTransferEndToEndTest(unittest.TestCase):
    """preview→export→send→receive→prepare with real local helpers; only bind is synthetic.

    Native session state and turn interruption need a live CLI, so those replies
    and export/inspect/prepare run in-process with the real worker code. The
    helper install, profile probe, archive streaming and cleanup are real processes.
    """
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name).resolve()
        env=patch.dict(os.environ,{'HOME':str(self.home)});env.start();self.addCleanup(env.stop)
        config=self.home/'.config/sessionholic/config.json';config.parent.mkdir(parents=True)
        config.write_text(json.dumps({'version':1,'environments':{'isolated':{}},'profiles':[
            {'agent':'codex','home':'.codex-isolated','label':'격리','environment':'isolated'}]}))
        (self.home/'.codex-isolated').mkdir();(self.home/'.codex-isolated/auth.json').write_text('fixture-only-never-read')
        binary=self.home/'.local/bin/codex';binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexit 1\n');binary.chmod(0o700)
        self.repo=self.home/'work/project';self.repo.mkdir(parents=True)
        def git(*args):subprocess.run(['git','-C',str(self.repo),*args],check=True,capture_output=True,env=transfer_workspace_env())
        git('init','-q','-b','main');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
        for name in ('README.md','.claude/agents/reviewer.md','.claude/settings.json','.codex/AGENTS.md','.gitignore'):
            path=self.repo/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('target/\n' if name=='.gitignore' else 'shared '+name+'\n')
        git('add','.');git('commit','-qm','initial')
        (self.repo/'notes.txt').write_text('untracked work\n')
        (self.repo/'client_secret_x.json').write_text('fixture-secret-never-transport')
        (self.repo/'.claude/settings.local.json').write_text('fixture-personal-never-transport')
        (self.repo/'target/debug').mkdir(parents=True);(self.repo/'target/debug/app').write_text('build output')
        self.hosts=[{'name':'source','label':'원본','local':True},{'name':'target','label':'대상','local':True}]
        self.flow=Transfers(self.hosts,self.home/'board-state')
        self.source={'host':'source','agent':'codex','home':'.codex-isolated','id':'019aaaaa-0000-7000-8000-000000000001','cwd':str(self.repo)}
        self.target={'host':'target','agent':'codex','account':PROFILE['id']}
        self.ops=[]
        real=self.real=self.flow.rpc
        def rpc(host,op,args=None,timeout=30):
            self.ops.append((host['name'],op))
            if op=='interrupt':return {'confirmed':True,'safeToTransfer':True,'phase':'idle','revision':'r1'}
            if op=='state':return {'safeToTransfer':True,'revision':'r1'}
            if op in ('export','inspect','prepare'):
                return json.loads(json.dumps(transfer_worker.rpc({'op':op,'args':args})))
            return real(host,op,args,timeout)
        self.flow.rpc=rpc
        self.bound=[]
        def bind(spec,profile,host,**kwargs):
            self.bound.append(spec['cwd'])
            return {**spec,'nativeSource':{'host':host,'agent':'codex','id':'019bbbbb-0000-7000-8000-000000000002'},'connectionMode':'attach'}
        for context in (patch('transfer_worker.source_ready',return_value={'safeToTransfer':True,'revision':'r1'}),
                        patch('managed_launch.bind',side_effect=bind)):
            context.start();self.addCleanup(context.stop)
    def test_workspace_moves_through_real_helper_processes(self):
        preview=self.flow.preview(self.source,self.target,self.hosts[0],self.hosts[1])
        workspace=preview['workspace']
        self.assertEqual(workspace['excludedSensitive'],['client_secret_x.json'])
        self.assertIn('.claude/settings.local.json',workspace['excluded'])
        self.assertIn('target/',workspace['excluded'])
        self.assertIn('README.md',[row['path'] for row in workspace['largestFiles']])
        # In real use the source helper path is known from an earlier poll.
        self.real(self.hosts[0],'status',{'transferId':'0'*32})
        result=self.flow.execute(self.source,self.target,self.hosts[0],self.hosts[1],{'environment':'isolated'},
                                 'e2e-request',lambda:[{'role':'user','text':'이어가기'}])
        destination=Path(result['destinationCwd'])
        self.assertEqual(destination,self.home/'.local/share/sessionholic/workspaces'/result['transferId']/'project')
        self.assertEqual(self.bound,[str(destination)])
        for name in ('README.md','.claude/agents/reviewer.md','.claude/settings.json','.codex/AGENTS.md','notes.txt'):
            self.assertEqual((destination/name).read_text(),(self.repo/name).read_text(),name)
        for name in ('client_secret_x.json','.claude/settings.local.json','target'):
            self.assertFalse((destination/name).exists(),name)
        self.assertIn(('target','profiles'),self.ops)
        self.assertEqual([host for host,op in self.ops if op=='cleanup'],['source','target'])
        job=self.home/'.local/state/sessionholic-transfers'/result['transferId']
        self.assertTrue((job/'launch.json').exists())
        self.assertFalse((job/'incoming.tar.gz').exists())
        self.assertEqual(list((job/'export').glob('workspace-*.tar.gz')),[])
        record=json.loads((self.home/'board-state/transfers'/result['transferId']/'transfer.json').read_text())
        self.assertEqual((record['status'],record['archiveCleanupPending']),('ready',False))
        handoff=next(job.glob('handoff-*.md')).read_text()
        self.assertNotIn('largestFiles',handoff);self.assertNotIn('fixture-secret-never-transport',handoff)
        installed=list((self.home/'.local/state/sessionholic-transfer-runtime').glob('worker-*.pyz'))
        self.assertEqual([path.name for path in installed],['worker-'+self.flow.runtime()['sha256']+'.pyz'])
        self.assertEqual(result['argv'][:2],[sys.executable,'-I'])
        self.assertEqual(result['argv'][-2:],['exec',result['transferId']])


def transfer_workspace_env():
    import transfer_workspace
    return transfer_workspace._git_env()


# Another agent or account on the same remote host (remote handoff). The board plans with
# names only, the remote helper stages (validate, build, bind) with its own rules, and the
# terminal runs the helper's `handoff <token>` step. HOME is a temporary folder; app-servers,
# CLIs and SSH are fixtures. No agent or model is started.
HANDOFF_CONFIG = {
    'version': 2,
    'environments': {
        'default': {'label': 'Standard'},
        'private': {'label': 'Private', 'variables': {'GIT_CONFIG_NOSYSTEM': '1'}},
    },
    'profileRules': [{'agent': 'codex', 'homePrefix': '.codex-private', 'environment': 'private'}],
    'projects': [{'id': 'private', 'label': 'Private', 'root': 'Private', 'match': 'component',
                  'environment': 'private'}],
}
CLAUDE_SOURCE = {'host': 'air', 'agent': 'claude', 'id': 'claude-agent-0001', 'home': '.claude',
                 'sessionId': '0f9c3a52-1b7e-4c1a-9d6e-5a4b3c2d1e0f', 'kind': 'interactive', 'phase': 'idle'}
CODEX_SOURCE = {'host': 'air', 'agent': 'codex', 'id': '019aaaaa-0000-7000-8000-000000000001',
                'home': '.codex', 'phase': 'idle'}
HANDOFF_MESSAGES = [{'role': 'user', 'text': '이 작업을 이어서 진행해 주세요.'},
            {'role': 'assistant', 'text': '테스트를 추가했습니다.'}]
NATIVE_ID = '019bbbbb-0000-7000-8000-000000000002'
HANDOFF_ROUTE = 'c' * 64


def file_mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class RemoteHandoffHome(unittest.TestCase):
    """A fake remote HOME with two Codex homes, Claude and fixture binaries that never run."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-handoff-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        config = self.home / '.config/sessionholic/config.json'
        config.parent.mkdir(parents=True, mode=0o700)
        config.write_text(json.dumps(HANDOFF_CONFIG))
        for key in ('.codex', '.codex-private'):
            (self.home / key).mkdir()
            (self.home / key / 'auth.json').write_text('fixture-only-never-read')
        (self.home / '.claude').mkdir()
        binaries = self.home / '.local/bin'
        binaries.mkdir(parents=True)
        for name in ('claude', 'codex'):
            (binaries / name).write_text('#!/bin/sh\nexit 1\n')
            (binaries / name).chmod(0o700)
        self.work = self.home / 'Work/project'
        self.work.mkdir(parents=True)
        self.private = self.home / 'Private/notes'
        self.private.mkdir(parents=True)
        self.native_calls = []
        test = self

        class Native:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def call(self, method, params):
                test.native_calls.append(method)
                if method == 'thread/start':
                    return {'thread': {'id': NATIVE_ID, 'cwd': params['cwd']}}
                if method == 'thread/read':
                    return {'thread': {'id': NATIVE_ID, 'turns': [{'id': 'fixture-turn'}]}}
                return {'turn': {'id': 'fixture-turn'}}
        self.socket = patch('transfer_native._socket_path', return_value=self.home / 'fixture.sock')
        # Only the fixture binaries: an installed CLI on this machine is never chosen.
        fixture = lambda agent, home: str(home / '.local/bin' / agent) if (home / '.local/bin' / agent).exists() else None
        for patcher in (patch.dict(os.environ, {'HOME': str(self.home)}),
                        patch.object(collector, 'HOME', self.home),
                        patch.object(launch, '_binary', side_effect=fixture),
                        self.socket,
                        patch('native_chat._client', side_effect=lambda *a: Native())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.handoffs = self.home / '.local/state/sessionholic-transfers/handoffs'

    def args(self, source=CLAUDE_SOURCE, account='codex:.codex', agent='codex', request='request-0001',
             cwd=None, messages=HANDOFF_MESSAGES):
        return {'source': {**source, 'cwd': str(cwd or self.work)},
                'target': {'host': source['host'], 'agent': agent, 'account': account},
                'messages': messages, 'requestId': request, 'routeKey': HANDOFF_ROUTE}

    def stage(self, **kwargs):
        return transfer_worker.rpc({'op': 'stage_handoff', 'args': self.args(**kwargs)})

    def refused(self, **kwargs):
        with self.assertRaises(ValueError) as caught:
            self.stage(**kwargs)
        return caught.exception


class StageHandoffTest(RemoteHandoffHome):
    def test_stage_writes_private_launch_and_binds_the_first_input_once(self):
        result = self.stage()
        token = transfer_worker.handoff_token('request-0001')
        self.assertEqual(result['token'], token)
        self.assertEqual(result['cwd'], str(self.work))
        self.assertEqual(result['connectionMode'], 'attach')
        self.assertEqual(result['nativeSource'], {'host': 'air', 'agent': 'codex', 'home': '.codex',
                                                  'cwd': str(self.work), 'id': NATIVE_ID})
        self.assertEqual(self.native_calls.count('thread/start'), 1)
        self.assertEqual(self.native_calls.count('turn/start'), 1)
        folder = self.handoffs / token
        for path in (self.handoffs, folder):
            self.assertEqual(file_mode(path), 0o700, path)
        launch_file = folder / 'launch.json'
        document = next(folder.glob('handoff-*.md'))
        receipt = self.handoffs / 'native-launches' / (HANDOFF_ROUTE + '.json')
        for path in (launch_file, document, receipt):
            self.assertEqual(file_mode(path), 0o600, path)
        saved = json.loads(launch_file.read_text())
        self.assertEqual(saved['requestId'], 'request-0001')
        self.assertEqual(saved['source']['sessionId'], CLAUDE_SOURCE['sessionId'])
        self.assertEqual(saved['target'], {'host': 'air', 'agent': 'codex', 'account': 'codex:.codex'})
        self.assertEqual(saved['argv'][0], str(self.home / '.local/bin/codex'))
        self.assertEqual(saved['argv'][1:3], ['resume', NATIVE_ID])
        self.assertEqual(saved['env']['CODEX_HOME'], str(self.home / '.codex'))
        self.assertEqual(saved['handoffPath'], str(document))
        self.assertIn('이 작업을 이어서 진행해 주세요.', document.read_text())

    def test_same_request_is_processed_once(self):
        self.stage()
        error = self.refused()
        self.assertEqual(error.transfer_stage, 'prepared')
        self.assertIn('이미 처리한 인계 요청', str(error))
        self.assertEqual(self.native_calls.count('turn/start'), 1)
        self.assertEqual(len(list((self.handoffs / transfer_worker.handoff_token('request-0001')).glob('handoff-*.md'))), 1)

    def test_a_new_request_on_an_unattached_route_reopens_its_thread_without_resending(self):
        self.stage()
        again = self.stage(request='request-0002')
        self.assertEqual(again['nativeSource']['id'], NATIVE_ID)
        self.assertEqual(self.native_calls.count('turn/start'), 1)

    def test_remote_boundary_is_decided_again_with_the_host_rules(self):
        # The board's estimate may differ; the host resolves the real folder and refuses.
        error = self.refused(cwd=self.private)
        self.assertEqual(error.transfer_stage, 'validate')
        self.assertIn('경계', str(error))
        self.assertEqual(self.native_calls, [])
        folder = self.handoffs / transfer_worker.handoff_token('request-0001')
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['.operation.lock'])
        allowed = self.stage(cwd=self.private, account='codex:.codex-private', request='request-0003')
        self.assertEqual(allowed['nativeSource']['home'], '.codex-private')

    def test_missing_or_unavailable_profile_is_refused_before_anything_starts(self):
        self.assertEqual(self.refused(account='codex:.codex-none').transfer_stage, 'validate')
        (self.home / '.codex-private/auth.json').unlink()
        error = self.refused(cwd=self.private, account='codex:.codex-private', request='request-0004')
        self.assertEqual(error.transfer_stage, 'validate')
        self.assertIn('로그인 파일', str(error))
        self.assertEqual(self.native_calls, [])

    def fixture_daemon(self):
        """A fixture `codex` whose only command is `app-server daemon start`: it records the
        variables it ran with and leaves a socket file where the real daemon publishes one."""
        codex = self.home / '.local/bin/codex'
        codex.write_text('#!%s -I\n' % sys.executable + (
            'import json, os, socket, sys\n'
            'if sys.argv[1:] != ["app-server", "daemon", "start"]: sys.exit(1)\n'
            'home = os.environ["CODEX_HOME"]\n'
            'with open(os.path.join(os.environ["HOME"], "daemon-starts.jsonl"), "a") as out:\n'
            '    out.write(json.dumps({"home": home, "nosystem": os.environ.get("GIT_CONFIG_NOSYSTEM"),\n'
            '                          "cwd": os.getcwd()}) + "\\n")\n'
            'folder = os.path.join(home, "app-server-control")\n'
            'os.makedirs(folder, mode=0o700, exist_ok=True)\n'
            'socket.socket(socket.AF_UNIX).bind(os.path.join(folder, "app-server-control.sock"))\n'
            'print(json.dumps({"status": "started"}))\n'))
        codex.chmod(0o700)
        return self.home / 'daemon-starts.jsonl'

    def test_codex_target_without_app_server_starts_it_in_the_target_environment(self):
        starts = self.fixture_daemon()
        self.socket.stop()
        try:
            request = {'source': {**CLAUDE_SOURCE, 'cwd': str(self.private)},
                       'target': {'agent': 'codex', 'account': 'codex:.codex-private'}}
            summary = transfer_worker.check_handoff(request)
            # The check only reports it; nothing runs.
            self.assertEqual(summary['appServer'], 'start')
            self.assertFalse(starts.exists())
            result = self.stage(cwd=self.private, account='codex:.codex-private')
            self.stage(cwd=self.private, account='codex:.codex-private', request='request-0002')
        finally:
            self.socket.start()
        self.assertEqual(result['nativeSource']['home'], '.codex-private')
        # Started once, with the target home and its environment's variables, from that home.
        home = str(self.home / '.codex-private')
        self.assertEqual([json.loads(line) for line in starts.read_text().splitlines()],
                         [{'home': home, 'nosystem': '1', 'cwd': home}])
        # The same route reopens its unattached thread: the first prompt was sent once.
        self.assertEqual(self.native_calls.count('turn/start'), 1)

    def test_codex_target_whose_app_server_cannot_start_is_refused_before_bind(self):
        self.socket.stop()
        try:
            error = self.refused()
        finally:
            self.socket.start()
        self.assertEqual(error.transfer_stage, 'build')
        self.assertIn('앱 서버를 시작하지 못했습니다', str(error))
        self.assertEqual(self.native_calls, [])
        folder = self.handoffs / transfer_worker.handoff_token('request-0001')
        self.assertFalse((folder / 'launch.json').exists())

    def test_same_account_and_other_hosts_are_not_staged(self):
        error = self.refused(source=CODEX_SOURCE, account='codex:.codex')
        self.assertIn('기존 세션 연결', str(error))
        args = self.args()
        args['target']['host'] = 'other'
        with self.assertRaisesRegex(ValueError, '같은 기기'):
            transfer_worker.rpc({'op': 'stage_handoff', 'args': args})

    def test_working_source_and_empty_conversation_are_refused_at_build(self):
        error = self.refused(source={**CLAUDE_SOURCE, 'phase': 'working'})
        self.assertEqual(error.transfer_stage, 'build')
        error = self.refused(messages=[], request='request-0005')
        self.assertEqual(error.transfer_stage, 'build')
        self.assertEqual(self.native_calls, [])

    def test_request_identifiers_are_validated(self):
        args = self.args()
        args['routeKey'] = '../x'
        with self.assertRaisesRegex(ValueError, '경로 식별자'):
            transfer_worker.rpc({'op': 'stage_handoff', 'args': args})
        with self.assertRaisesRegex(ValueError, '요청 식별자'):
            self.stage(request='bad id')

    def test_handoff_document_has_the_same_meaning_as_a_local_handoff(self):
        # Same source, target and messages: the board's own build_launch writes the same text.
        for source, agent, account in ((CODEX_SOURCE, 'claude', 'claude:default'),
                                       (CLAUDE_SOURCE, 'codex', 'codex:.codex')):
            request = 'request-' + agent
            self.stage(source=source, agent=agent, account=account, request=request)
            staged = next((self.handoffs / transfer_worker.handoff_token(request)).glob('handoff-*.md'))
            profile = next(p for p in launch.discover_profiles() if p['id'] == account)
            local = launch.build_launch({**source, 'cwd': str(self.work)}, {**profile, 'host': 'air'},
                                        HANDOFF_MESSAGES, self.home / ('board-' + agent))
            self.assertEqual(staged.read_text(), Path(local['handoffPath']).read_text())
            self.assertEqual(local['mode'], 'handoff')


class HandoffStepTest(RemoteHandoffHome):
    def setUp(self):
        super().setUp()
        self.result = self.stage()
        self.token = self.result['token']
        self.folder = self.handoffs / self.token

    @contextmanager
    def no_exec(self):
        with patch('transfer_worker.os.execve') as execve, patch('transfer_worker.os.chdir') as chdir:
            yield execve, chdir

    def main(self, *args, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(sys, 'stdin', io.StringIO(stdin)), \
                patch.object(sys, 'argv', ['worker.pyz', 'handoff'] + list(args)):
            try:
                transfer_worker.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_handoff_step_consumes_the_launch_and_execs_once(self):
        saved = json.loads((self.folder / 'launch.json').read_text())
        with self.no_exec() as (execve, chdir):
            code, out, err = self.main(self.token)
        self.assertEqual((code, out, err), (0, '', ''))
        chdir.assert_called_once_with(str(self.work))
        execve.assert_called_once_with(saved['argv'][0], saved['argv'], saved['env'])
        self.assertFalse((self.folder / 'launch.json').exists())
        self.assertEqual(file_mode(self.folder / 'started.json'), 0o600)
        receipt = json.loads((self.handoffs / 'native-launches' / (HANDOFF_ROUTE + '.json')).read_text())
        self.assertEqual(receipt['terminalId'], 'handoff-' + self.token)
        with self.no_exec() as (execve, _):
            code, out, err = self.main(self.token)
        execve.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn('원격 인계를 시작하지 못했습니다. 이미 실행한 인계입니다.', err)
        # After the step, a new request on this route starts a new native thread.
        self.stage(request='request-0009')
        self.assertEqual(self.native_calls.count('turn/start'), 2)

    def test_failures_print_a_korean_reason_without_values(self):
        with self.no_exec() as (execve, _):
            code, out, err = self.main('f' * 32)
            self.assertEqual(code, 1)
            self.assertIn('준비된 인계를 찾지 못했습니다', err)
            code, _, err = self.main('../escape')
            self.assertEqual(code, 1)
            self.assertIn('인계 식별자가 올바르지 않습니다', err)
            (self.home / '.local/bin/codex').unlink()
            code, _, err = self.main(self.token)
        execve.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn('대상 CLI 실행 파일을 찾지 못했습니다', err)
        self.assertTrue((self.folder / 'launch.json').exists())
        self.assertFalse((self.folder / 'started.json').exists())

    def test_check_mode_reports_names_only_and_runs_nothing(self):
        saved = json.loads((self.folder / 'launch.json').read_text())
        with self.no_exec() as (execve, chdir):
            code, out, err = self.main('--check', self.token)
        execve.assert_not_called()
        chdir.assert_not_called()
        self.assertEqual((code, err), (0, ''))
        summary = json.loads(out)
        self.assertEqual(summary, {'ok': True, 'mode': 'handoff', 'binary': 'codex', 'cwd': str(self.work),
                                   'envKeys': sorted(saved['env']), 'connectionMode': 'attach', 'prepared': True,
                                   'agent': 'codex', 'profile': 'codex:.codex', 'sourceEnvironment': 'default',
                                   'firstInput': 'app-server'})
        self.assertIn('CODEX_HOME', summary['envKeys'])
        for value in (saved['env']['PATH'], saved['env']['HOME'] + '"', NATIVE_ID, 'unix://', 'fixture.sock'):
            self.assertNotIn(value, out)
        self.assertTrue((self.folder / 'launch.json').exists())
        self.assertFalse((self.folder / 'started.json').exists())

    def test_dry_run_check_validates_a_request_and_keeps_nothing(self):
        request = {'source': {**CLAUDE_SOURCE, 'cwd': str(self.private)},
                   'target': {'agent': 'codex', 'account': 'codex:.codex-private'}}
        before = sorted(str(p) for p in self.handoffs.rglob('*'))
        calls = list(self.native_calls)
        code, out, err = self.main('--check', '-', stdin=json.dumps(request))
        self.assertEqual((code, err), (0, ''))
        summary = json.loads(out)
        self.assertEqual((summary['prepared'], summary['binary'], summary['cwd']), (False, 'codex', str(self.private)))
        self.assertEqual(summary['sourceEnvironment'], 'private')
        self.assertIn('GIT_CONFIG_NOSYSTEM', summary['envKeys'])
        self.assertEqual((summary['firstInput'], summary['appServer']), ('app-server', 'running'))
        self.assertEqual(self.native_calls, calls)
        self.assertEqual(sorted(str(p) for p in self.handoffs.rglob('*')), before)
        # The host's own boundary verdict is reported as a refusal.
        request['target']['account'] = 'codex:.codex'
        code, out, err = self.main('--check', '-', stdin=json.dumps(request))
        self.assertEqual((code, out), (1, ''))
        self.assertIn('인계 점검에 실패했습니다. 원본과 대상의 실행 환경 경계가 다릅니다.', err)
        code, _, err = self.main('--check', '-', stdin='{not json')
        self.assertEqual(code, 1)
        self.assertIn('점검 요청 JSON', err)


class RuntimeBundleTest(RemoteHandoffHome):
    def test_runtime_bundle_carries_every_module_the_remote_handoff_imports(self):
        for name in ('transfer_worker.py', 'launch.py', 'managed_launch.py', 'transfer_native.py',
                     'native_chat.py', 'collector.py', 'settings.py', 'instance.py', 'agent_router.py'):
            self.assertIn(name, transfer.RUNTIME_MODULES)
        flow = Transfers([], self.home / 'board')
        data = base64.b64decode(flow.runtime()['runtime'])
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            self.assertIn(b'def stage_handoff', bundle.read('transfer_worker.py'))
        # Any change to the helper changes the SHA, so hosts reinstall it automatically.
        self.assertEqual(flow.runtime()['sha256'], hashlib.sha256(data).hexdigest())

    def test_installed_helper_runs_the_check_in_its_own_process(self):
        flow = Transfers([], self.home / 'board')
        helper = self.home / 'worker.pyz'
        helper.write_bytes(base64.b64decode(flow.runtime()['runtime']))
        request = {'source': {**CODEX_SOURCE, 'cwd': str(self.work)},
                   'target': {'agent': 'claude', 'account': 'claude:default'}}
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
        result = subprocess.run([sys.executable, '-I', str(helper), 'handoff', '--check', '-'],
                                input=json.dumps(request).encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        summary = json.loads(result.stdout)
        self.assertEqual((summary['binary'], summary['agent'], summary['firstInput'], summary['connectionMode']),
                         ('claude', 'claude', 'argument', 'direct'))
        self.assertNotIn('CODEX_HOME', summary['envKeys'])


HANDOFF_HOSTS = [{'name': 'board', 'label': '보드', 'local': True},
         {'name': 'air', 'label': '원격', 'local': False, 'ssh': 'air', 'python': '/usr/bin/python3'}]
HANDOFF_PROFILES = [
    {'id': 'claude:default', 'agent': 'claude', 'home': '.claude', 'label': 'Claude', 'available': True,
     'environment': 'default'},
    {'id': 'codex:.codex', 'agent': 'codex', 'home': '.codex', 'label': 'Codex', 'available': True,
     'environment': 'default'},
    {'id': 'codex:.codex-private', 'agent': 'codex', 'home': '.codex-private', 'label': 'Private Codex',
     'available': True, 'environment': 'private'},
    {'id': 'codex:.codex-off', 'agent': 'codex', 'home': '.codex-off', 'label': 'Off', 'available': False,
     'reason': '이 호스트에 저장된 로그인 파일이 없습니다.', 'environment': 'default'},
]


class HandoffTerminals:
    max_terminals = 4

    def __init__(self):
        self.records, self.created = [], []

    def list(self):
        return [dict(row) for row in self.records]

    @contextmanager
    def reserve(self, key):
        yield

    def create(self, key, argv, cwd, env, metadata):
        self.created.append((argv, cwd, metadata))
        record = {'id': str(len(self.records) + 1) * 32, 'key': key, 'alive': True, **metadata}
        self.records.append(record)
        return dict(record)


class WorkflowRemoteHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='sh-board-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        config = self.home / '.config/sessionholic/config.json'
        config.parent.mkdir(parents=True, mode=0o700)
        config.write_text(json.dumps(HANDOFF_CONFIG))
        self.state = self.home / 'state'
        self.row = {**CLAUDE_SOURCE, 'cwd': '/Users/u/Work/project', 'title': '원격 작업', 'account': 'Claude'}
        self.reads = []

        def runner(host, args, timeout):
            if args[0] == 'snapshot':
                return {'claude': [self.row] if host['name'] == 'air' else [], 'codex': [], 'errors': []}
            self.reads.append((host['name'], args))
            return {'messages': HANDOFF_MESSAGES}
        self.board = server.Board(HANDOFF_HOSTS, 60, runner)
        for host in HANDOFF_HOSTS:
            self.board.poll(host)
        self.terms = HandoffTerminals()
        self.flow = server.Workflow(self.board, self.terms, self.state)
        self.flow.profiles = lambda: []
        self.profile_calls = []

        def profiles(host, refresh=False):
            self.profile_calls.append((host['name'], refresh))
            return HANDOFF_PROFILES
        self.flow.transfers.profiles = profiles
        self.rpcs = []
        self.reply = None

        def rpc(host, op, args=None, timeout=30):
            self.rpcs.append((host['name'], op, args, timeout))
            self.flow.transfers.runtime_paths[host['name']] = '/Users/u/.local/state/rt/worker-' + 'a' * 64 + '.pyz'
            if isinstance(self.reply, Exception):
                raise self.reply
            if self.reply is not None:
                return self.reply
            return {'token': transfer_worker.handoff_token(args['requestId']), 'cwd': args['source']['cwd'],
                    'connectionMode': 'attach', 'warnings': [],
                    'nativeSource': {'host': 'air', 'agent': 'codex', 'home': '.codex',
                                     'cwd': args['source']['cwd'], 'id': NATIVE_ID}}
        self.flow.transfers.rpc = rpc
        for patcher in (patch.dict(os.environ, {'HOME': str(self.home)}),
                        patch('server.shutil.which', side_effect=lambda name, **kw: '/usr/bin/' + name)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def plan(self, account='codex:.codex', agent='codex', host='air'):
        ref = {key: self.row[key] for key in ('agent', 'id', 'home')}
        ref['host'] = 'air'
        return self.flow.plan({'source': ref, 'target': {'host': host, 'agent': agent, 'account': account}})

    def test_same_remote_handoff_is_planned_like_a_local_handoff(self):
        plan = self.plan()
        self.assertTrue(plan['allowed'], plan['reason'])
        self.assertEqual(plan['mode'], 'handoff')
        self.assertEqual(plan['summary'], '작업 맥락을 자동으로 넘겨 새 터미널을 준비합니다.')
        self.assertEqual(plan['warnings'], ['최근 대화와 파일 변경 상태를 넘겨 새 실행을 준비합니다. 기존 프로세스·승인 상태는 이어지지 않습니다.'])
        self.assertEqual(self.profile_calls, [('air', True)])
        self.assertEqual(self.rpcs, [])
        # The same account on the same remote host is still a plain attach.
        self.assertEqual(self.plan(agent='claude', account='claude:default')['mode'], 'attach')

    def test_missing_unavailable_or_unreachable_profiles_refuse(self):
        plan = self.plan(account='codex:.codex-none')
        self.assertFalse(plan['allowed'])
        self.assertEqual(plan['reason'], '원격 기기의 대상 계정을 확인해 주세요.')
        plan = self.plan(account='codex:.codex-off')
        self.assertEqual((plan['allowed'], plan['reason']), (False, '이 호스트에 저장된 로그인 파일이 없습니다.'))

        def failing(host, refresh=False):
            raise RuntimeError('기기의 이전 도우미에 연결하지 못했습니다. SSH 연결을 확인해 주세요.')
        self.flow.transfers.profiles = failing
        plan = self.plan()
        self.assertEqual((plan['allowed'], plan['reason']),
                         (False, '기기의 이전 도우미에 연결하지 못했습니다. SSH 연결을 확인해 주세요.'))

    def test_working_or_waiting_source_is_refused(self):
        for phase in ('working', 'needs_input'):
            self.row['phase'] = phase
            self.board.poll(HANDOFF_HOSTS[1])
            plan = self.plan()
            self.assertFalse(plan['allowed'])
            self.assertIn('진행 중인 실행', plan['reason'])

    def test_board_estimate_of_the_boundary_refuses_first(self):
        self.row['cwd'] = '/Users/u/Private/notes'
        self.board.poll(HANDOFF_HOSTS[1])
        plan = self.plan()
        self.assertEqual((plan['allowed'], plan['reason']), (False, '원본과 대상의 실행 환경 경계가 다릅니다.'))
        self.assertTrue(self.plan(account='codex:.codex-private')['allowed'])

    def test_offline_remote_is_refused_without_contacting_it(self):
        self.board.state['air']['ok'] = False
        plan = self.plan()
        self.assertFalse(plan['allowed'])
        self.assertEqual(self.profile_calls, [])

    def test_launch_stages_on_the_host_and_opens_its_handoff_step(self):
        plan = self.plan()
        result = self.flow.launch(plan['id'], 'request-0001')
        token = transfer_worker.handoff_token('request-0001')
        self.assertEqual(self.reads, [('air', ['read', 'claude', CLAUDE_SOURCE['sessionId']])])
        (name, op, args, timeout), = self.rpcs
        self.assertEqual((name, op), ('air', 'stage_handoff'))
        self.assertGreaterEqual(timeout, 90)
        route = hashlib.sha256(json.dumps([plan['source'], plan['target']], sort_keys=True).encode()).hexdigest()
        self.assertEqual(args['routeKey'], route)
        self.assertEqual(args['messages'], HANDOFF_MESSAGES)
        self.assertEqual(args['target'], {'host': 'air', 'agent': 'codex', 'account': 'codex:.codex'})
        self.assertEqual(set(args['source']), set(transfer_worker.SELECTOR))
        self.assertEqual(args['source']['cwd'], '/Users/u/Work/project')
        (argv, cwd, metadata), = self.terms.created
        self.assertEqual(argv[0], '/usr/bin/ssh')
        self.assertIn('-tt', argv)
        self.assertEqual(argv[-2:], ['air', 'exec /usr/bin/python3 -I /Users/u/.local/state/rt/worker-'
                                     + 'a' * 64 + '.pyz handoff ' + token])
        self.assertEqual(cwd, str(self.home))
        self.assertEqual((metadata['mode'], metadata['launchMode'], metadata['host']), ('attach', 'handoff', 'air'))
        self.assertEqual(metadata['nativeSource']['id'], NATIVE_ID)
        self.assertEqual(result['terminal']['nativeSource']['host'], 'air')
        marker = self.state / 'handoffs' / (token + '.json')
        self.assertEqual(file_mode(marker), 0o600)
        self.assertEqual(json.loads(marker.read_text())['status'], 'prepared')
        self.assertEqual(self.flow.launch(plan['id'], 'request-0001'), result)
        self.assertEqual(len(self.rpcs), 1)

    def test_a_request_is_staged_once_even_after_a_board_restart(self):
        plan = self.plan()
        self.reply = WorkerError('원본과 대상의 실행 환경 경계가 다릅니다.', 'validate')
        with self.assertRaisesRegex(ValueError, '경계'):
            self.flow.launch(plan['id'], 'request-0002')
        marker = self.state / 'handoffs' / (transfer_worker.handoff_token('request-0002') + '.json')
        self.assertEqual(json.loads(marker.read_text())['status'], 'failed')
        restarted = server.Workflow(self.board, self.terms, self.state)
        restarted.transfers.profiles, restarted.transfers.rpc = self.flow.transfers.profiles, self.flow.transfers.rpc
        self.flow = restarted
        with self.assertRaisesRegex(ValueError, '이미 처리한 인계 요청'):
            self.flow.launch(self.plan()['id'], 'request-0002')
        self.assertEqual(len(self.rpcs), 1)
        self.assertEqual(self.terms.created, [])

    def test_unknown_preparation_is_never_reported_as_not_started(self):
        for number, reply in enumerate((WorkerError('bind failed', 'bind'), RuntimeError('기기 응답 시간이 초과됐습니다.'),
                                        {'token': 'f' * 32, 'cwd': '/x'})):
            self.reply = reply
            with self.assertRaises(RuntimeError) as caught:
                self.flow.launch(self.plan()['id'], 'request-unknown-%d' % number)
            self.assertNotIsInstance(caught.exception, server.LaunchFailure)
            self.assertEqual(str(caught.exception), transfer.UNKNOWN_HANDOFF)
        self.assertEqual(self.terms.created, [])

    def test_state_change_after_plan_blocks_before_staging(self):
        plan = self.plan()
        self.row['phase'] = 'working'
        with self.assertRaisesRegex(ValueError, '실행 중이거나 승인'):
            self.flow.launch(plan['id'], 'request-0003')
        self.assertEqual(self.rpcs, [])

    def test_unexpected_native_identity_is_not_recorded(self):
        plan = self.plan()
        self.reply = {'token': transfer_worker.handoff_token('request-0004'), 'cwd': '/Users/u/Work/project',
                      'connectionMode': 'bogus',
                      'nativeSource': {'host': 'elsewhere', 'agent': 'codex', 'home': '.codex', 'cwd': '/x', 'id': NATIVE_ID}}
        self.flow.launch(plan['id'], 'request-0004')
        (_, _, metadata), = self.terms.created
        self.assertNotIn('nativeSource', metadata)
        self.assertEqual(metadata['mode'], 'handoff')

    def test_terminal_failure_after_staging_reports_the_prepared_target(self):
        plan = self.plan()

        def broken(*args):
            raise RuntimeError('tmux fixture failure')
        self.terms.create = broken
        with self.assertRaises(server.LaunchFailure) as caught:
            self.flow.launch(plan['id'], 'request-0005')
        recovery = caught.exception.recovery
        self.assertEqual((recovery['host'], recovery['destinationCwd']), ('air', '/Users/u/Work/project'))
        self.assertEqual(recovery['nativeSource']['id'], NATIVE_ID)


if __name__=='__main__':unittest.main()
