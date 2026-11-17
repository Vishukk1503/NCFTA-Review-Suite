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
        for name in ('Start-NCFTA.bat', 'Start-NCFTA.cmd', 'Check-Intel-GPU.bat',
                     'Open-Driver-Help.bat', 'Install-Work-PC.bat', 'install_work_pc.py'):
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

    def test_no_launcher_depends_on_powershell(self):
        self.assertFalse(list(self.root.glob('*.ps1')))
        for name in ('Start-NCFTA.bat', 'Start-NCFTA.cmd', 'Check-Intel-GPU.bat',
                     'Open-Driver-Help.bat', 'Install-Work-PC.bat', 'install_work_pc.py'):
            source = (self.root / name).read_text().lower()
            for forbidden in ('powershell', 'pwsh', '.ps1'):
                self.assertNotIn(forbidden, source, name)

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
        (self.root / 'install_work_pc.py').unlink()
        code, output = self.invoke('Install-Work-PC.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('install_work_pc.py is missing', output)

    def test_installer_forwards_arguments_and_python_exit_code(self):
        (self.root / 'install_work_pc.py').write_text(
            "import json,sys; print(json.dumps(sys.argv[1:])); sys.exit(7)\n", encoding='utf8')
        args = ['--python', sys.executable, '--venv', str(self.root / 'separate environment'), '--profile', 'intel']
        code, output = self.invoke('Install-Work-PC.bat', *args)
        self.assertEqual(code, 7, output)
        self.assertIn(json.dumps(args), output)
        self.assertIn('Exit code: 7', output)

    def test_installer_parse_error_is_visible(self):
        (self.root / 'install_work_pc.py').write_text('def broken(\n', encoding='utf8')
        code, output = self.invoke('Install-Work-PC.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('SyntaxError', output)

    def test_installer_incomplete_extraction(self):
        code, output = self.invoke('Install-Work-PC.bat')
        self.assertEqual(code, 1, output)
        self.assertIn('Missing file: requirements-work-pc.txt', output)
        reports = list((self.root / 'setup').glob('install-*.json'))
        self.assertEqual(len(reports), 1)
        self.assertEqual(json.loads(reports[0].read_text())['result'], 'failed')

    def test_installer_rejects_bad_explicit_python_without_bootstrap(self):
        code, output = self.invoke('Install-Work-PC.bat', '--python', str(self.root / 'not-there.exe'))
        self.assertEqual(code, 1, output)
        self.assertIn('selected Python is missing', output)
        self.assertNotIn('Installing Python', output)

    def test_installer_plan_and_legacy_options_make_no_environment(self):
        for name in ('requirements-work-pc.txt', 'constraints-windows.txt', 'settings.example.json',
                     '.env.example', 'check_work_pc.py', 'test_install.py', 'export_xlsx.py'):
            (self.root / name).write_text('', encoding='utf8')
        for options in [('--plan', '--skip-model-check'), ('-Plan', '-SkipModelCheck')]:
            with self.subTest(options=options):
                code, output = self.invoke('Install-Work-PC.bat', *options)
                self.assertEqual(code, 0, output)
                self.assertIn('Python installer started.', output)
                self.assertIn('PLAN ONLY', output)
                self.assertFalse((self.root / '.venv').exists())


if __name__ == '__main__':
    unittest.main()
