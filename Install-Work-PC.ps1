[CmdletBinding()]
param(
    [ValidateSet('intel','cpu','nvidia')][string]$Profile = 'intel',
    [string]$Python,
    [string]$VenvPath,
    [string]$Wheelhouse,
    [switch]$Plan,
    [switch]$SkipModelCheck
)
$ErrorActionPreference = 'Stop'
$stage = 'Starting PowerShell'
$transcriptStarted = $false
$installLog = if ($env:NCFTA_INSTALL_LOG) { $env:NCFTA_INSTALL_LOG } else { Join-Path ([IO.Path]::GetTempPath()) ('NCFTA-Install-' + [guid]::NewGuid().ToString('N') + '.log') }
function Write-InstallStatus([string]$Status) {
    if ($env:NCFTA_INSTALL_STATUS_FILE) {
        [IO.File]::WriteAllText($env:NCFTA_INSTALL_STATUS_FILE, $Status + [Environment]::NewLine)
    }
}
function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit $LASTEXITCODE). Installation stopped; fix the reported error and rerun." }
}
function Find-Python {
    $candidates = @()
    if ($Python) { $candidates += $Python }
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        try {
            $found = & py.exe -3.12 -c 'import sys;print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) { $candidates += $found }
        } catch { }
    }
    $candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
    $candidates += (Join-Path $env:ProgramFiles 'Python312\python.exe')
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            & $candidate -c 'import sys,struct;sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize(''P'')==8 else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        }
    }
    return $null
}
try {
    Write-Host 'PowerShell installer started.' -ForegroundColor Cyan
    Start-Transcript -LiteralPath $installLog -Append | Out-Null
    $transcriptStarted = $true
    Write-Host "Installation log: $installLog"
    Set-Location -LiteralPath $PSScriptRoot
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    if (-not $VenvPath) { $VenvPath = Join-Path $PSScriptRoot '.venv' }
    $torchBuild = if ($Profile -eq 'nvidia') { '2.14.1+cu126' } else { '2.14.1+cpu' }
    $torchIndex = if ($Profile -eq 'nvidia') { 'https://download.pytorch.org/whl/cu126' } else { 'https://download.pytorch.org/whl/cpu' }
    $stage = 'Checking extracted setup files'
    $requiredFiles = @('requirements-work-pc.txt', 'constraints-windows.txt', 'settings.example.json', '.env.example', 'suite.py', 'check_work_pc.py')
    if (-not $SkipModelCheck) {
        $requiredFiles += 'assets\model-v3\model.safetensors'
        if ($Profile -eq 'intel') { $requiredFiles += @('accelerators\openvino-v3\encoder-fp32.xml', 'accelerators\openvino-v3\encoder-fp32.bin') }
    }
    foreach ($requiredFile in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $requiredFile) -PathType Leaf)) {
            throw "Missing file: $requiredFile. Extract ALL files from NCFTA-Review-Suite-Windows-v3.zip. GitHub's Source code ZIP does not include the model."
        }
    }
    $stage = 'Detecting hardware'
    Write-Host 'Detecting graphics and NPU devices...'
    $gpuNames = @(Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
    $npuNames = @(Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'Intel.*(AI Boost|NPU|Neural)' } | Select-Object -ExpandProperty Name)
    Write-Host ('Graphics: ' + ($gpuNames -join ', '))
    Write-Host ('NPU: ' + $(if ($npuNames.Count) { $npuNames -join ', ' } else { 'Not detected by Windows' }))
    Write-Host "Profile: $Profile | Python 3.12 x64 | Torch $torchBuild | OpenVINO 2026.4.1"
    Write-Host 'Intel GPU uses OpenVINO FP32; the extraction head runs on CPU.'
    Write-Host 'NPU hardware computes FP16: detected, but disabled for this FP32 model.' -ForegroundColor Yellow
    Write-Host 'Packages install only into the selected virtual environment. Existing model weights stay unchanged.'
    if ($Plan) {
        Get-Content -LiteralPath (Join-Path $PSScriptRoot 'requirements-work-pc.txt')
        Write-Host "Environment: $VenvPath"
        Write-Host 'No packages or drivers were installed.'
        Write-InstallStatus 'PLAN ONLY - no packages or drivers installed.'
        exit 0
    }
    $stage = 'Checking Visual C++ runtime'
    Write-Host "Stage: $stage"
    $vcRuntime = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
    if (-not $vcRuntime -or $vcRuntime.Installed -ne 1) {
        if ($Wheelhouse -or -not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
            throw 'Install Microsoft Visual C++ 2015-2022 x64 Redistributable through IT first: https://aka.ms/vs/17/release/vc_redist.x64.exe'
        }
        Write-Host 'Installing Microsoft Visual C++ x64 runtime. Windows may request administrator approval.'
        Invoke-Checked 'winget.exe' @('install','--id','Microsoft.VCRedist.2015+.x64','--exact','--source','winget','--accept-package-agreements','--accept-source-agreements')
    }
    $stage = 'Finding Python 3.12 x64'
    Write-Host "Stage: $stage"
    $basePython = Find-Python
    if (-not $basePython) {
        if ($Python) { throw 'The supplied Python must be Python 3.12 x64.' }
        if ($Wheelhouse) { throw 'Offline installation requires Python 3.12 x64 already installed.' }
        if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { throw 'Install Python 3.12 x64 from python.org or your IT software center, then rerun this BAT.' }
        Write-Host 'Installing Python 3.12 for this user through Windows Package Manager...'
        Invoke-Checked 'winget.exe' @('install','--id','Python.Python.3.12','--exact','--source','winget','--scope','user','--accept-package-agreements','--accept-source-agreements')
        $basePython = Find-Python
        if (-not $basePython) { throw 'Python was not found after installation. Reopen this installer, or supply -Python with the executable path.' }
    }
    $stage = 'Preparing Python environment'
    Write-Host "Stage: $stage"
    $venvPython = Join-Path $VenvPath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) { Invoke-Checked $basePython @('-m','venv',$VenvPath) }
    Invoke-Checked $venvPython @('-c','import sys,struct;assert sys.version_info[:2]==(3,12) and struct.calcsize(''P'')==8,''Existing environment must use Python 3.12 x64''')
    $stage = 'Installing pinned packages'
    Write-Host "Stage: $stage"
    if ($Wheelhouse) {
        $wheelPath = (Resolve-Path -LiteralPath $Wheelhouse).Path
        Invoke-Checked $venvPython @('-m','pip','install','--no-index','--find-links',$wheelPath,"torch==$torchBuild",'-r','requirements-work-pc.txt','-c','constraints-windows.txt')
    } else {
        Invoke-Checked $venvPython @('-m','pip','install','--upgrade','pip==26.3')
        Invoke-Checked $venvPython @('-m','pip','install','--index-url',$torchIndex,"torch==$torchBuild",'-c','constraints-windows.txt')
        Invoke-Checked $venvPython @('-m','pip','install','-r','requirements-work-pc.txt','-c','constraints-windows.txt')
    }
    $stage = 'Checking installed packages'
    Write-Host "Stage: $stage"
    Invoke-Checked $venvPython @('-m','pip','check')
    if (-not (Test-Path -LiteralPath 'settings.json')) { Copy-Item -LiteralPath 'settings.example.json' -Destination 'settings.json' }
    if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
    New-Item -ItemType Directory -Force -Path 'setup' | Out-Null
    & $venvPython -m pip freeze --all | Set-Content -LiteralPath 'setup\installed-packages.txt' -Encoding UTF8
    if ($LASTEXITCODE -ne 0) { throw 'Could not record installed package versions.' }
    $stage = 'Checking hardware, Excel export and model'
    Write-Host "Stage: $stage"
    $checkArgs = @('-X','utf8','check_work_pc.py','--profile',$Profile)
    if ($SkipModelCheck) { $checkArgs += '--skip-model-check' }
    Invoke-Checked $venvPython $checkArgs
    if ($SkipModelCheck) {
        Write-InstallStatus 'PACKAGES INSTALLED - model readiness was NOT checked.'
    } else {
        Write-InstallStatus 'SETUP COMPLETE - put your NCFTA key in .env, then open Start-NCFTA.bat.'
    }
    Write-Host 'Package installation complete. Put your NCFTA key in .env, then launch Start-NCFTA.bat.' -ForegroundColor Green
    Write-Host 'For Intel processing select intel-gpu in the CLI. auto still chooses CUDA or CPU.'
    Write-Host 'Intel GPU and NPU drivers come from your PC manufacturer / IT. This installer does not replace drivers.'
} catch {
    Write-Host "SETUP FAILED during: $stage" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "Installation log: $installLog"
    if ($stage -eq 'Checking hardware, Excel export and model') {
        Write-Host 'Driver help: https://docs.openvino.ai/2026/get-started/install-openvino/configurations/configurations-intel-gpu.html'
    }
    exit 1
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
