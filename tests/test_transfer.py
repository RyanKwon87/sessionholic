import json
import hashlib
import concurrent.futures
import threading
from contextlib import contextmanager, ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
from transfer import Transfers
import transfer_worker

PROFILE = {'id':'codex:.codex-isolated','agent':'codex','home':'.codex-isolated','label':'격리 환경','environment':'isolated','available':True}
HOSTS = [{'name':'remote','label':'원격 기기','local':True},
         {'name':'local','label':'로컬 기기','local':False,'ssh':'local','python':'/usr/bin/python3'}]


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
                if method=='thread/queue/add':return {'queuedSubmission':{
                    'id':'fixture-queue','clientUserMessageId':params['clientUserMessageId']}}
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
        self.assertEqual([method for method,_ in calls],['thread/start','thread/queue/add','thread/queue/start','thread/read'])
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
                if op=='prepare':raise RuntimeError('fixture response lost after native queue start')
                return result
            self.flow.rpc=rpc
            with self.assertRaisesRegex(RuntimeError,'대상.*확인'):self.execute()
            record=json.loads(next((Path(self.temp.name)/'transfers').glob('*/transfer.json')).read_text())
            self.assertEqual(record['targetPreparation'],'unknown')
            self.assertTrue(record['sourceStopped'])
            self.assertEqual(sum(method=='thread/queue/add' for method,_ in calls),1)
            with self.assertRaises(ValueError):self.execute()
            self.assertEqual(sum(method=='thread/queue/add' for method,_ in calls),1)
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
    def test_unknown_prepare_defers_target_until_explicit_prepared_readback(self):
        original=self.flow.rpc
        def lost(host,op,args=None,timeout=30):
            if op=='prepare':raise RuntimeError('fixture response lost')
            if op=='status':return {'prepared':False,'started':False}
            return original(host,op,args,timeout)
        self.flow.rpc=lost
        with self.assertRaises(RuntimeError):self.execute()
        path=next((Path(self.temp.name)/'transfers').glob('*/transfer.json'))
        record=json.loads(path.read_text())
        self.assertEqual([host for host,op in self.calls if op=='cleanup'],['local'])
        result=self.flow.cleanup_pending(record['id'])
        self.assertEqual(result['deferredHosts'],['remote'])
        self.assertTrue(result['archiveCleanupPending'])
        self.flow.rpc=lambda host,op,args=None,timeout=30: {'prepared':True,'started':False} if op=='status' else original(host,op,args,timeout)
        self.assertFalse(self.flow.cleanup_pending(record['id'])['archiveCleanupPending'])
        self.assertEqual(json.loads(path.read_text())['targetPreparation'],'unknown')
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


if __name__=='__main__':unittest.main()
