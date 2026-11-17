"""Windows setup using Python's standard library; invoked directly by the BAT."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
PYTHON_CHECK = "import sys,struct,platform; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 and platform.machine().upper()=='AMD64', 'Python 3.12 x64 is required'"


class SetupError(RuntimeError):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code if 1 <= code <= 255 else 1


class Tee:
    def __init__(self, screen, log):
        self.screen, self.log = screen, log

    def write(self, value):
        self.screen.write(value)
        self.screen.flush()
        self.log.write(value)
        self.log.flush()
        return len(value)

    def flush(self):
        self.screen.flush()
        self.log.flush()


def run(command, *, check=True, capture_file=None):
    """Use argument arrays, stream both output channels, and preserve failures."""
    env = dict(os.environ, PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    with subprocess.Popen([str(part) for part in command], cwd=HERE, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding='utf8', errors='replace') as process:
        captured = []
        for line in process.stdout:
            if capture_file is None:
                print(line, end='', flush=True)
            else:
                captured.append(line)
        code = process.wait()
    if capture_file is not None:
        if code:
            print(''.join(captured), end='')
        else:
            capture_file.write_text(''.join(captured), encoding='utf8')
    if code and check:
        raise SetupError(f'Command failed with exit code {code}: {Path(command[0]).name}. Read the output above.', code)
    return code


def vc_runtime_installed():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key, 'Installed')[0] == 1
    except FileNotFoundError:
        return False


def check_bundle(args):
    required = ['requirements-work-pc.txt', 'constraints-windows.txt', 'settings.example.json',
                '.env.example', 'suite.py', 'check_work_pc.py', 'test_install.py', 'export_xlsx.py']
    if not args.skip_model_check:
        required.append('assets/model-v3/model.safetensors')
        if args.profile == 'intel':
            required.extend(['accelerators/openvino-v3/encoder-fp32.xml',
                             'accelerators/openvino-v3/encoder-fp32.bin'])
    for name in required:
        if not (HERE / name).is_file():
            raise SetupError(f'Missing file: {name}. Extract the full Windows bundle, then apply the complete BAT update. The source-code ZIP and update ZIP alone do not include the model.')


def ensure_vc_runtime(offline):
    if vc_runtime_installed():
        print('Visual C++ x64 runtime is installed.')
        return
    winget = shutil.which('winget.exe')
    if offline or not winget:
        raise SetupError('Install Microsoft Visual C++ 2015-2022 x64 Redistributable through IT, then retry. Official installer: https://aka.ms/vs/17/release/vc_redist.x64.exe')
    print('Installing Microsoft Visual C++ x64 runtime. Windows may request administrator approval.')
    code = run([winget, 'install', '--id', 'Microsoft.VCRedist.2015+.x64', '--exact',
                '--source', 'winget', '--accept-package-agreements', '--accept-source-agreements'], check=False)
    if not vc_runtime_installed():
        raise SetupError(f'Visual C++ x64 runtime is still unavailable (WinGet exit {code}). Follow the installer message or contact IT.', code or 1)
    if code:
        print(f'WinGet returned {code}; the x64 runtime is now registered. Package and model checks will verify it.')


def copy_template(source, destination):
    try:
        with destination.open('xb') as stream:
            stream.write(source.read_bytes())
    except FileExistsError:
        pass


def parser():
    result = argparse.ArgumentParser(description='NCFTA Windows BAT/Python installer')
    result.add_argument('--profile', '-Profile', choices=['intel', 'cpu', 'nvidia'], default='intel')
    result.add_argument('--python', '-Python', type=Path, help='Python 3.12 x64 executable')
    result.add_argument('--venv', '-VenvPath', type=Path, default=HERE / '.venv')
    result.add_argument('--wheelhouse', '-Wheelhouse', type=Path)
    result.add_argument('--plan', '-Plan', action='store_true')
    result.add_argument('--skip-model-check', '-SkipModelCheck', action='store_true')
    return result


def install(args, status):
    def stage(name):
        status['stage'] = name
        print(f'\nStage: {name}', flush=True)

    stage('Checking Python and extracted files')
    if os.name != 'nt' or sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8 or platform.machine().upper() != 'AMD64':
        raise SetupError('Windows with Python 3.12 x64 is required.')
    check_bundle(args)
    base_python = args.python.resolve() if args.python else Path(sys.executable)
    run([base_python, '-c', PYTHON_CHECK])
    venv = args.venv.resolve()
    torch_build = '2.14.1+cu126' if args.profile == 'nvidia' else '2.14.1+cpu'
    torch_index = 'https://download.pytorch.org/whl/' + ('cu126' if args.profile == 'nvidia' else 'cpu')
    wheelhouse = args.wheelhouse.resolve() if args.wheelhouse else None
    if wheelhouse and not wheelhouse.is_dir():
        raise SetupError(f'Wheelhouse folder does not exist: {wheelhouse}')
    print(f'Profile: {args.profile} | Python 3.12 x64 | Torch {torch_build} | OpenVINO 2026.4.1')
    print(f'Environment: {venv}')
    print('Intel GPU: FP32 encoder with CPU extraction head. NPU inference is disabled for this FP32 model.')
    if args.plan:
        print((HERE / 'requirements-work-pc.txt').read_text(encoding='utf8'))
        print('PLAN ONLY. No packages or drivers installed.')
        status['result'] = 'plan_only'
        return
    stage('Checking Visual C++ runtime')
    ensure_vc_runtime(offline=wheelhouse is not None)
    stage('Preparing Python environment')
    python = venv / 'Scripts/python.exe'
    if not python.is_file():
        run([base_python, '-m', 'venv', venv])
    run([python, '-c', PYTHON_CHECK])
    stage('Installing pinned packages')
    requirements, constraints = HERE / 'requirements-work-pc.txt', HERE / 'constraints-windows.txt'
    if wheelhouse:
        run([python, '-m', 'pip', 'install', '--no-index', '--find-links', wheelhouse,
             f'torch=={torch_build}', '-r', requirements, '-c', constraints])
    else:
        run([python, '-m', 'pip', 'install', '--upgrade', 'pip==26.3'])
        run([python, '-m', 'pip', 'install', '--index-url', torch_index, f'torch=={torch_build}', '-c', constraints])
        run([python, '-m', 'pip', 'install', '-r', requirements, '-c', constraints])
    stage('Checking installed packages')
    run([python, '-m', 'pip', 'check'])
    copy_template(HERE / 'settings.example.json', HERE / 'settings.json')
    copy_template(HERE / '.env.example', HERE / '.env')
    setup = HERE / 'setup'
    setup.mkdir(exist_ok=True)
    run([python, '-m', 'pip', 'freeze', '--all'], capture_file=setup / 'installed-packages.txt')
    stage('Checking hardware, Excel export and model')
    check = [python, '-X', 'utf8', '-u', HERE / 'check_work_pc.py', '--profile', args.profile]
    if args.skip_model_check:
        check.append('--skip-model-check')
    run(check)
    report = json.loads((setup / 'hardware-report.json').read_text(encoding='utf8'))
    if report.get('errors') or report.get('profile') != args.profile:
        raise SetupError('The hardware check did not confirm a successful check for the requested profile. See setup/hardware-report.json.')
    if args.skip_model_check:
        status['result'] = 'packages_installed_model_unchecked'
        print('\nPACKAGES INSTALLED. Model readiness was NOT checked.')
    else:
        if not report.get('ready'):
            raise SetupError('The hardware check did not confirm model readiness. See setup/hardware-report.json.')
        status['result'] = 'complete'
        print('\nSETUP COMPLETE. Put your NCFTA key in .env, then open Start-NCFTA.bat.')
    print('Select intel-gpu in the CLI for Intel GPU inference. Drivers are managed by your PC manufacturer or IT.')


def main(argv=None):
    args = parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf8', errors='replace')
    name = 'install-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    log_path = HERE / 'setup' / (name + '.log')
    try:
        log_path.parent.mkdir(exist_ok=True)
        log = log_path.open('x', encoding='utf8')
    except OSError:
        log_path = Path(tempfile.gettempdir()) / ('NCFTA-' + name + '.log')
        log = log_path.open('x', encoding='utf8')
    status = {'stage': 'Starting Python setup', 'result': 'failed', 'log': str(log_path)}
    code = 0
    with log, redirect_stdout(Tee(sys.stdout, log)), redirect_stderr(Tee(sys.stderr, log)):
        print('Python installer started.')
        print(f'Log: {log_path}')
        try:
            install(args, status)
        except KeyboardInterrupt:
            code = 130
            status['result'] = 'interrupted'
            print('\nSetup interrupted. Rerun the BAT to retry.')
        except Exception as error:
            code = error.code if isinstance(error, SetupError) else 1
            status['error'] = str(error)
            print(f'\nSETUP FAILED during: {status["stage"]}')
            print(str(error))
        print(f'Log: {log_path}')
        status['exit_code'] = code
        try:
            log_path.with_suffix('.json').write_text(json.dumps(status, indent=2), encoding='utf8')
        except OSError as error:
            print(f'Could not save the status file: {error}')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
