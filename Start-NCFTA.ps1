$ErrorActionPreference = 'Stop'
try {
    Set-Location -LiteralPath $PSScriptRoot
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'suite.py') -PathType Leaf)) {
        throw 'suite.py is missing. Extract the complete suite before launching.'
    }
    if ($env:NCFTA_PYTHON) {
        $pythonPath = $env:NCFTA_PYTHON
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            throw 'NCFTA_PYTHON points to a missing executable. Correct it, or clear it to use the installed environment.'
        }
    } else {
        $pythonCandidates = @((Join-Path $PSScriptRoot '.venv\Scripts\python.exe'), (Join-Path $PSScriptRoot '..\..\work\gpu-env\Scripts\python.exe'))
        $pythonPath = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    }
    if (-not $pythonPath) { throw 'Python environment not found. Run Install-Work-PC.bat first.' }
    # Set terminal capabilities only for this launcher and its child process.
    Remove-Item Env:NO_COLOR -ErrorAction SilentlyContinue
    $env:TERM = 'xterm-256color'
    $env:COLORTERM = 'truecolor'
    $env:NCFTA_TERMINAL = '1'
    & $pythonPath -X utf8 (Join-Path $PSScriptRoot 'suite.py') @args
    exit $LASTEXITCODE
} catch {
    Write-Host ('NCFTA launch failed: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
