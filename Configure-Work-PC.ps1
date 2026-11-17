param([string]$Node, [string]$ArtifactModules)
$ErrorActionPreference = 'Stop'
$runtimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node'
if (-not $Node) { $Node = Join-Path $runtimeRoot 'bin\node.exe' }
if (-not $ArtifactModules) { $ArtifactModules = Join-Path $runtimeRoot 'node_modules' }
if (-not (Test-Path -LiteralPath $Node)) { throw 'Node not found. Pass -Node with its full executable path.' }
if (-not (Test-Path -LiteralPath (Join-Path $ArtifactModules '@oai\artifact-tool\package.json'))) { throw 'Artifact Tool not found. Pass -ArtifactModules with the approved node_modules directory.' }
$configPath = Join-Path $PSScriptRoot 'settings.json'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$config.node = (Resolve-Path -LiteralPath $Node).Path
$config.artifact_modules = (Resolve-Path -LiteralPath $ArtifactModules).Path
$config.env = '.env'
$config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
Write-Host 'Configured local runtime paths. Put your key in .env and run Start-NCFTA.cmd.'
