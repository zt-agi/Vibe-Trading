"""Real subprocess regression cases for the September 29 execution stall."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('guard', Path(__file__).with_name('guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='guard test ', dir=str(guard.RUNTIME/'tmp'))
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_child(self, code, **options):
        return guard.run_command([sys.executable, '-B', '-c', code], name='test',
                                 cwd=self.root, log_root=self.root,
                                 timeout=options.pop('timeout', 5), heartbeat=0.2,
                                 check=options.pop('check', {'status': 'PASS', 'synthetic_test': True}),
                                 **options)

    def test_success_and_e_thread_environment(self):
        state = self.run_child("import os,json; print(json.dumps({k:os.environ[k] for k in "
                               "['OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS']}))")
        self.assertEqual(state['status'], 'PASS')
        self.assertEqual(set(json.loads(Path(state['stdout']).read_text()).values()), {'2'})
        self.assertIn('heartbeat_at', state)

    def test_nonzero_is_failed(self):
        state = self.run_child('import sys; sys.exit(7)')
        self.assertEqual((state['status'], state['exit_code']), ('FAILED', 7))

    def test_cmd_quoted_executable(self):
        body = f'"{sys.executable}" -c "print(12345)"'
        state = guard.run_command(['cmd.exe', '/d', '/c', body], name='quoted',
                                  cwd=self.root, log_root=self.root, check={'status': 'PASS'})
        self.assertEqual(state['status'], 'PASS')
        self.assertEqual(Path(state['stdout']).read_text().strip(), '12345')

    def test_timeout_kills_descendant(self):
        marker = self.root/'must not appear.txt'
        child = f'import time,pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text("bad")'
        parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-B","-c",{child!r}]); time.sleep(20)'
        started = time.monotonic()
        state = self.run_child(parent, timeout=0.6)
        self.assertEqual((state['status'], state['exit_code']), ('TIMEOUT', 124))
        self.assertLess(time.monotonic()-started, 4)
        time.sleep(1.6)
        self.assertFalse(marker.exists(), 'timed-out descendant survived')

    def test_preflight_failure_never_launches(self):
        marker = self.root/'must not launch.txt'
        state = self.run_child(f'from pathlib import Path; Path({str(marker)!r}).write_text("bad")',
                               check={'status': 'FAIL', 'memory': {'status': 'FAIL'}})
        self.assertEqual(state['status'], 'PREFLIGHT_FAILED')
        self.assertFalse(marker.exists())

    def test_new_memory_pressure_stops_owned_command(self):
        with patch.object(guard, 'memory_status', return_value={'status': 'FAIL', 'synthetic_test': True}):
            state = self.run_child('import time; time.sleep(20)')
        self.assertEqual((state['status'], state['exit_code']), ('RESOURCE_LIMIT', 75))

    def test_missing_executable_preserves_receipt(self):
        state = guard.run_command([str(self.root/'missing.exe')], name='missing', cwd=self.root,
                                  log_root=self.root, check={'status': 'PASS'})
        self.assertEqual(state['status'], 'LAUNCH_FAILED')
        self.assertTrue((Path(state['stdout']).parent/'state.json').exists())

    def test_interruption_preserves_state(self):
        with patch.object(guard.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_child('import time; time.sleep(20)')
        state = json.loads(next(self.root.glob('test-*/state.json')).read_text())
        self.assertEqual(state['status'], 'INTERRUPTED')

    def test_repeated_step_retains_both_receipts(self):
        one = self.run_child('print("one")')
        two = self.run_child('print("two")')
        self.assertNotEqual(one['stdout'], two['stdout'])
        self.assertEqual(len(list(self.root.glob('test-*/state.json'))), 2)

    def test_nonfinite_deadline_rejected(self):
        for timeout in (float('nan'), float('inf'), 0, -1):
            with self.assertRaises(ValueError):
                self.run_child('pass', timeout=timeout)

    def test_missing_drive_fails_quickly(self):
        started = time.monotonic()
        result = guard.project_probe(self.root/'missing', timeout=2)
        self.assertEqual(result['status'], 'FAIL')
        self.assertLess(time.monotonic()-started, 3)

    def test_runtime_journal_cannot_escape_to_c(self):
        with self.assertRaises(ValueError):
            guard.run_command([sys.executable, '-c', 'pass'], name='test', cwd=self.root,
                              log_root=Path('C:/guard-test'), check={'status': 'PASS'})


if __name__ == '__main__':
    unittest.main()
