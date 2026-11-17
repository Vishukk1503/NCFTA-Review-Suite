"""Exercise Windows entry points with synthetic input, without inference or fetches."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


@unittest.skipUnless(os.name == 'nt', 'Windows launchers require Windows')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ncfta-launcher-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'suite & launcher test'
        self.root.mkdir()
        for name in ('Start-NCFTA.bat', 'Start-NCFTA.cmd', 'Start-NCFTA.ps1',
                     'Check-Intel-GPU.bat', 'Check-Intel-GPU.ps1',
                     'Open-Driver-Help.bat', 'Install-Work-PC.bat', 'Install-Work-PC.ps1'):
            shutil.copyfile(HERE / name, self.root / name)
        (self.root / 'suite.py').write_text(
            "import json, os, sys\n"
            "print('LAUNCH_TEST:' + json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'terminal': os.getenv('NCFTA_TERMINAL')}))\n"
            "sys.exit(7 if '--fail' in sys.argv else 0)\n", encoding='utf8')
        self.env = dict(os.environ, NCFTA_PYTHON=sys.executable)

    def invoke(self, name, *args):
        command = f'"{os.environ["COMSPEC"]}" /d /c call "{self.root / name}"'
        if args:
            command += ' ' + subprocess.list2cmdline(list(args))
        result = subprocess.run(command, cwd=self.temp.name, env=self.env, input='',
                                capture_output=True, text=True, encoding='utf8',
                                errors='replace', timeout=60)
        return result.returncode, result.stdout + result.stderr

    def test_arguments_working_directory_and_terminal(self):
        for name, prefix in [('Start-NCFTA.bat', []), ('Start-NCFTA.cmd', []),
                             ('Check-Intel-GPU.bat', ['check-intel'])]:
            with self.subTest(launcher=name):
                args = ['--run', r'C:\review files\saved & synthetic', '--samples', '2']
                code, output = self.invoke(name, *args)
                self.assertEqual(code, 0, output)
                payload = json.loads(next(line.split('LAUNCH_TEST:', 1)[1]
                                          for line in output.splitlines() if line.startswith('LAUNCH_TEST:')))
                self.assertEqual(payload['args'], prefix + args)
                self.assertEqual(Path(payload['cwd']), self.root)
                self.assertEqual(payload['terminal'], '1')
                self.assertEqual(output.count('Press any key to CLOSE this window.'), 1)

    def test_python_failures_propagate_through_all_aliases(self):
        for name in ('Start-NCFTA.bat', 'Start-NCFTA.cmd', 'Check-Intel-GPU.bat'):
            with self.subTest(launcher=name):
                code, output = self.invoke(name, '--fail')
                self.assertEqual(code, 7, output)
                self.assertIn('Exit code: 7', output)

    def test_missing_powershell_companion_stays_visible(self):
        (self.root / 'Start-NCFTA.ps1').unlink()
        code, output = self.invoke('Start-NCFTA.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('Start-NCFTA.ps1 is missing', output)
        self.assertIn('Press any key to CLOSE', output)

    def test_missing_shared_launcher_stays_visible(self):
        (self.root / 'Start-NCFTA.bat').unlink()
        for name in ('Start-NCFTA.cmd', 'Check-Intel-GPU.bat'):
            with self.subTest(launcher=name):
                code, output = self.invoke(name)
                self.assertEqual(code, 1, output)
                self.assertIn('Start-NCFTA.bat is missing', output)

    def test_invalid_explicit_python_does_not_silently_fall_back(self):
        self.env['NCFTA_PYTHON'] = str(self.root / 'missing-python.exe')
        code, output = self.invoke('Start-NCFTA.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('NCFTA_PYTHON points to a missing executable', output)

    def test_missing_python_explains_installation(self):
        self.env.pop('NCFTA_PYTHON', None)
        code, output = self.invoke('Start-NCFTA.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('Run Install-Work-PC.bat first', output)

    def test_missing_suite_is_reported(self):
        (self.root / 'suite.py').unlink()
        code, output = self.invoke('Start-NCFTA.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('suite.py is missing', output)

    def test_driver_links_can_be_listed_without_opening_browser(self):
        code, output = self.invoke('Open-Driver-Help.bat', '--list')
        self.assertEqual(code, 0, output)
        self.assertIn('configurations-intel-gpu.html', output)
        self.assertIn('configurations-intel-npu.html', output)

    def test_installer_missing_companion(self):
        (self.root / 'Install-Work-PC.ps1').unlink()
        code, output = self.invoke('Install-Work-PC.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('Install-Work-PC.ps1 is missing', output)

    def test_installer_silent_exit_is_not_success(self):
        (self.root / 'Install-Work-PC.ps1').write_text('', encoding='utf8')
        code, output = self.invoke('Install-Work-PC.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('without confirming that setup completed', output)

    def test_installer_parse_error_is_visible(self):
        (self.root / 'Install-Work-PC.ps1').write_text('param(\n', encoding='utf8')
        code, output = self.invoke('Install-Work-PC.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('ParserError', output)
        self.assertIn('Startup log:', output)

    def test_installer_incomplete_extraction(self):
        code, output = self.invoke('Install-Work-PC.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('Missing file: requirements-work-pc.txt', output)

    def test_powershell_intel_shortcut_forwards_arguments_and_failure(self):
        executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        result = subprocess.run([str(executable), '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(self.root / 'Check-Intel-GPU.ps1'), '--fail'],
                                cwd=self.root, env=self.env, capture_output=True, text=True,
                                encoding='utf8', errors='replace', timeout=60)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertIn('["check-intel", "--fail"]', result.stdout)


if __name__ == '__main__':
    unittest.main()
