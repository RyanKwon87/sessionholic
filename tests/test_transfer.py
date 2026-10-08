import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server
from transfer import Transfers

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
    def test_interrupt_export_copy_prepare_order_and_once(self):
        result=self.execute()
        self.assertEqual(self.calls,[('remote','profiles'),('local','interrupt'),('local','export'),('transport','copy'),('remote','prepare'),('local','state'),('local','inspect'),('local','cleanup'),('remote','cleanup')])
        self.assertEqual(result['destinationCwd'],'/new/work')
        self.assertEqual(result['argv'][-2],'exec')
        with self.assertRaises(ValueError): self.execute()
        self.assertEqual(sum(op=='interrupt' for _,op in self.calls),1)
    def test_source_changed_after_copy_never_returns_launch(self):
        self.fingerprint='changed'
        with self.assertRaisesRegex(ValueError,'파일이 바뀌'): self.execute()
        records=list((Path(self.temp.name)/'transfers').glob('*/transfer.json'))
        self.assertEqual(json.loads(records[0].read_text())['status'],'failed')
    def test_source_resumed_after_copy_never_returns_launch(self):
        self.phase='working'
        with self.assertRaisesRegex(ValueError,'원본 작업이 다시'): self.execute()
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


if __name__=='__main__':unittest.main()
