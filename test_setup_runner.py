import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import install_work_pc as setup


@unittest.skipUnless(os.name == 'nt' and sys.version_info[:2] == (3, 12), 'Windows Python 3.12 setup')
class SetupRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ncfta-setup-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.object(setup, 'HERE', self.root).start()
        for name in ('requirements-work-pc.txt', 'constraints-windows.txt', 'settings.example.json',
                     '.env.example', 'suite.py', 'check_work_pc.py', 'test_install.py', 'export_xlsx.py',
                     'assets/model-v3/model.safetensors', 'accelerators/openvino-v3/encoder-fp32.xml',
                     'accelerators/openvino-v3/encoder-fp32.bin'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('synthetic', encoding='utf8')
        self.calls = []

    def fake_run(self, command, **kwargs):
        command = [str(part) for part in command]
        self.calls.append(command)
        if any(part.endswith('check_work_pc.py') for part in command):
            report = {'profile': 'intel', 'ready': True, 'errors': []}
            (self.root / 'setup/hardware-report.json').write_text(json.dumps(report), encoding='utf8')
        return 0

    def test_subprocess_error_is_visible_and_preserves_code(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(setup.SetupError) as error:
            setup.run([sys.executable, '-c', "import sys; print('synthetic failure',file=sys.stderr); sys.exit(9)"])
        self.assertEqual(error.exception.code, 9)
        self.assertIn('synthetic failure', output.getvalue())

    def test_setup_preserves_existing_credentials_and_settings(self):
        existing = {'.env': b'SYNTHETIC_KEY=do-not-print-this\n', 'settings.json': b'{"custom":"preserve"}'}
        for name, contents in existing.items():
            (self.root / name).write_bytes(contents)
        status, output = {}, io.StringIO()
        with patch.object(setup, 'run', side_effect=self.fake_run), patch.object(setup, 'ensure_vc_runtime'), contextlib.redirect_stdout(output):
            setup.install(setup.parser().parse_args([]), status)
        self.assertEqual(status['result'], 'complete')
        for name, contents in existing.items():
            self.assertEqual((self.root / name).read_bytes(), contents)
        self.assertNotIn('do-not-print-this', output.getvalue())

    def test_offline_packages_use_only_wheelhouse(self):
        wheelhouse = self.root / 'approved wheels'
        wheelhouse.mkdir()
        with patch.object(setup, 'run', side_effect=self.fake_run), patch.object(setup, 'ensure_vc_runtime') as vc, contextlib.redirect_stdout(io.StringIO()):
            setup.install(setup.parser().parse_args(['--wheelhouse', str(wheelhouse)]), {})
        vc.assert_called_once_with(offline=True)
        installs = [command for command in self.calls if 'pip' in command and 'install' in command]
        self.assertEqual(len(installs), 1)
        self.assertIn('--no-index', installs[0])
        self.assertIn('torch==2.14.1+cpu', installs[0])
        self.assertNotIn('--index-url', installs[0])

    def test_package_failure_stops_before_model_and_success(self):
        def fail_install(command, **kwargs):
            if 'pip' in command and 'install' in command:
                raise setup.SetupError('synthetic package failure', 3)
            return self.fake_run(command, **kwargs)
        output = io.StringIO()
        with patch.object(setup, 'run', side_effect=fail_install), patch.object(setup, 'ensure_vc_runtime'), contextlib.redirect_stdout(output), self.assertRaises(setup.SetupError):
            setup.install(setup.parser().parse_args([]), {})
        self.assertNotIn('SETUP COMPLETE', output.getvalue())
        self.assertFalse(any(any(part.endswith('check_work_pc.py') for part in command) for command in self.calls))

    def test_zero_exit_without_ready_hardware_report_is_failure(self):
        def missing_readiness(command, **kwargs):
            result = self.fake_run(command, **kwargs)
            if any(str(part).endswith('check_work_pc.py') for part in command):
                (self.root / 'setup/hardware-report.json').write_text('{"profile":"intel","errors":[],"ready":false}')
            return result
        with patch.object(setup, 'run', side_effect=missing_readiness), patch.object(setup, 'ensure_vc_runtime'), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(setup.SetupError, 'did not confirm model readiness'):
            setup.install(setup.parser().parse_args([]), {})

    def test_missing_model_fails_before_any_command_or_install(self):
        (self.root / 'assets/model-v3/model.safetensors').unlink()
        with patch.object(setup, 'run') as run, patch.object(setup, 'ensure_vc_runtime') as vc, contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(setup.SetupError, 'Missing file'):
            setup.install(setup.parser().parse_args([]), {})
        run.assert_not_called()
        vc.assert_not_called()

    def test_no_offline_runtime_download(self):
        with patch.object(setup, 'vc_runtime_installed', return_value=False), patch.object(setup.shutil, 'which', return_value='winget.exe'), patch.object(setup, 'run') as run, self.assertRaises(setup.SetupError):
            setup.ensure_vc_runtime(offline=True)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
