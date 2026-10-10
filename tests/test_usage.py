"""The optional usage provider with fake helpers and fake account names."""
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import http.client

import server
import settings
from usage import UsageMeter

TOKEN = 'usage-fixture-token-0123456789'
CODEX = {'schema_version': 1, 'accounts': [
    {'key': 'alpha', 'status': 'ready', 'windows': {'five_hour': None,
     'weekly': {'remaining': 68, 'resets_at': '2026-10-14T13:00:00Z'}},
     'credits': {'has_credits': True, 'unlimited': False, 'balance': '1250.4'}},
    {'key': 'beta', 'status': 'login_required', 'windows': {}},
    {'key': 'gamma', 'status': 'skipped'}]}
CLAUDE = {'schema_version': 1, 'status': 'ready', 'usage': {'windows': {
    'five_hour': {'used_percentage': 14, 'resets_at': '2026-10-10T06:29:59Z'},
    'seven_day': {'used_percentage': 16, 'resets_at': '2026-10-14T22:59:59Z'}}}}


def config(folder, **extra):
    return {'provider': 'helper', 'resources': str(folder), 'python': str(Path(folder, 'python3')),
            'path': '/usr/bin:/bin', 'codex': '/fixture/codex', 'codexHelper': 'codex-usage.py',
            'codexAccounts': [{'key': 'alpha', 'label': '알파'}, {'key': 'beta', 'label': '베타'}],
            'claude': '/fixture/claude', 'claudeHelper': 'claude-usage.py',
            'intervalSeconds': 300, 'manualIntervalSeconds': 20, **extra}


class UsageMeterTest(unittest.TestCase):
    def meter(self, outputs, now=None):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        for name in ('codex-usage.py', 'claude-usage.py', 'python3'):
            Path(folder, name).write_text('')
        calls, clock = [], [now or 1000.0]
        def runner(argv):
            calls.append(argv)
            return outputs.pop(0) if outputs else None
        meter = UsageMeter(config(folder), runner=runner, clock=lambda: clock[0], threaded=False)
        return meter, calls, clock, folder

    def test_snapshot_converts_helpers_into_remaining_shares(self):
        meter, calls, _, folder = self.meter([CODEX, CLAUDE])
        data = meter.snapshot(refresh=True)
        self.assertEqual(calls[0], [str(Path(folder, 'python3')), str(Path(folder, 'codex-usage.py')), '/fixture/codex',
                                    '--json', '--accounts', 'alpha', 'beta'])
        self.assertEqual(calls[1], [str(Path(folder, 'python3')), str(Path(folder, 'claude-usage.py')), '/fixture/claude'])
        codex, claude = data['providers']
        self.assertEqual((codex['short'], codex['remaining'], codex['status']), ('CX', 68, 'ready'))
        self.assertEqual([row['name'] for row in codex['rows']], ['알파', '베타'])
        self.assertEqual(codex['rows'][0]['credits'], '1,250')
        self.assertEqual(codex['rows'][0]['windows'][0]['remaining'], None)
        self.assertEqual(codex['rows'][1]['note'], '로그인 필요')
        self.assertEqual(codex['rows'][1]['windows'], [])
        self.assertEqual((claude['short'], claude['remaining']), ('CC', 84))
        self.assertEqual([w['remaining'] for w in claude['rows'][0]['windows']], [86, 84])
        self.assertIsNotNone(data['fetchedAt'])

    def test_reads_are_spaced_and_failures_keep_marked_last_good_values(self):
        meter, calls, clock, _ = self.meter([CODEX, CLAUDE, None, {'schema_version': 1, 'status': 'unavailable'}])
        meter.snapshot(refresh=True)
        meter.snapshot(refresh=True)
        self.assertEqual(len(calls), 2, 'a second read within five minutes reuses the cache')
        clock[0] += 10
        meter.snapshot(force=True)
        self.assertEqual(len(calls), 2, 'manual refresh is spaced too')
        clock[0] += 400
        data = meter.snapshot(refresh=True)
        self.assertEqual(len(calls), 4)
        codex, claude = data['providers']
        self.assertEqual((codex['status'], codex['remaining']), ('stale', 68))
        self.assertIn('이전 조회', codex['note'])
        self.assertEqual((claude['status'], claude['remaining']), ('stale', 84))
        clock[0] += 25
        meter.snapshot(force=True)
        self.assertEqual(len(calls), 6, 'a manual refresh after its own interval reads again')

    def test_missing_helpers_are_unavailable_and_never_run(self):
        meter = UsageMeter(config('/nonexistent/helpers'), runner=lambda argv: self.fail('ran'), threaded=False)
        data = meter.snapshot(refresh=True, force=True)
        self.assertEqual((data['available'], data['providers']), (False, []))

    def test_one_configured_helper_runs_alone(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        for name in ('claude-usage.py', 'python3'):
            Path(folder, name).write_text('')
        settings_like = {key: value for key, value in config(folder).items()
                         if key not in ('codex', 'codexHelper', 'codexAccounts')}
        calls = []
        meter = UsageMeter(settings_like, runner=lambda argv: calls.append(argv) or CLAUDE, threaded=False)
        self.assertEqual([p['id'] for p in meter.snapshot(refresh=True)['providers']], ['claude'])
        self.assertEqual(len(calls), 1)

    def test_helper_runner_is_bounded_and_passes_no_secrets(self):
        meter = UsageMeter(config('/nonexistent'), home='/fixture/home')
        with patch('usage.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b'{"schema_version": 1, "status": "ready"}')
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture', 'OPENAI_API_KEY': 'fixture'}):
                self.assertEqual(meter.run_helper(['x'])['status'], 'ready')
            env = run.call_args.kwargs['env']
            self.assertEqual(set(env), {'PATH', 'HOME', 'USER', 'LANG'})
            self.assertEqual((env['PATH'], env['HOME']), ('/usr/bin:/bin', '/fixture/home'))
            self.assertEqual(run.call_args.kwargs['timeout'], 45)
            self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b'x' * 70000)
            self.assertIsNone(meter.run_helper(['x']))
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b'{"schema_version": 2}')
            self.assertIsNone(meter.run_helper(['x']))


class UsageRouteTest(unittest.TestCase):
    def serve(self, usage=None):
        board = server.Board([{'name': 'local', 'label': 'Local', 'local': True}], 60,
                             lambda host, args, timeout: {'claude': [], 'codex': [], 'errors': []})
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        httpd = server.make_server('127.0.0.1', 0, board, TOKEN, state_dir=Path(state.name), usage=usage)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd

    def request(self, httpd, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers={'Host': '127.0.0.1', **(headers or {})})
        response = connection.getresponse()
        return response, response.read()

    def test_routes_are_absent_without_a_provider(self):
        httpd = self.serve()
        auth = {'Authorization': 'Bearer ' + TOKEN}
        self.assertEqual(self.request(httpd, 'GET', '/api/usage', auth)[0].status, 404)
        response, _ = self.request(httpd, 'POST', '/api/usage/refresh',
                                   {**auth, 'X-CSRF-Token': httpd.csrf, 'Content-Type': 'application/json'}, b'{}')
        self.assertEqual(response.status, 404)

    def test_usage_reply_is_compressed_like_other_json_and_refresh_is_forced(self):
        reply = {'available': True, 'refreshing': False, 'fetchedAt': 1, 'error': None,
                 'providers': [{'id': 'codex', 'note': '사용량 ' * 300}]}
        meter = UsageMeter(config('/nonexistent'))
        httpd = self.serve(meter)
        auth = {'Authorization': 'Bearer ' + TOKEN}
        with patch.object(meter, 'snapshot', return_value=reply) as snapshot:
            response, data = self.request(httpd, 'GET', '/api/usage', {**auth, 'Accept-Encoding': 'gzip'})
            self.assertEqual((response.status, response.getheader('Content-Encoding')), (200, 'gzip'))
            self.assertEqual(json.loads(gzip.decompress(data)), reply)
            snapshot.assert_called_once_with(refresh=True)
            response, data = self.request(httpd, 'POST', '/api/usage/refresh',
                                          {**auth, 'X-CSRF-Token': httpd.csrf, 'Content-Type': 'application/json'}, b'{}')
            self.assertEqual(response.status, 200)
            snapshot.assert_called_with(force=True)

    def test_settings_section_builds_the_meter(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as folder:
            home = Path(folder).resolve()
            path = home / '.config/sessionholic/config.json'
            path.parent.mkdir(parents=True, mode=0o700)
            path.write_text(json.dumps({'version': 2, 'usage': {
                'provider': 'helper', 'resources': '~/helpers', 'python': '/usr/bin/python3',
                'codex': '/fixture/codex', 'codexHelper': 'codex-usage.py',
                'codexAccounts': [{'key': 'alpha', 'label': '알파'}], 'intervalSeconds': 600}}))
            path.chmod(0o600)
            section = settings.load(home)['usage']
            meter = UsageMeter(section, home=home)
            self.assertEqual((meter.resources, meter.interval, meter.manual_interval), (home / 'helpers', 600, 20))
            self.assertEqual(meter.helpers(), [home / 'helpers/codex-usage.py'])


if __name__ == '__main__':
    unittest.main()
