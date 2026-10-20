$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$upstreamCommit = 'b7f3dd0c5f6e6d66a298239ffd02b328b873918e'
$sourceDir = Join-Path $projectRoot 'tools\MinerU'
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $sourceDir)) {
    New-Item -ItemType Directory -Path (Split-Path $sourceDir -Parent) -Force | Out-Null
    git init $sourceDir
    if ($LASTEXITCODE -ne 0) { throw 'Failed to initialize MinerU source directory' }
    git -C $sourceDir remote add origin https://github.com/opendatalab/MinerU.git
    git -C $sourceDir fetch --depth 1 origin $upstreamCommit
    if ($LASTEXITCODE -ne 0) { throw 'Failed to fetch pinned MinerU source' }
    git -C $sourceDir checkout --detach FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Failed to select pinned MinerU source' }
}
$actualCommit = git -C $sourceDir rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $upstreamCommit) {
    throw 'Existing MinerU source does not match the pinned revision; inspect it before proceeding.'
}
if (-not (Test-Path -LiteralPath $python)) {
    python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python environment' }
}
& $python -m pip install uv
if ($LASTEXITCODE -ne 0) { throw 'Failed to install uv' }
Push-Location $projectRoot
try {
    & (Join-Path $projectRoot '.venv\Scripts\uv.exe') pip install --python $python --link-mode copy -r (Join-Path $projectRoot 'requirements.lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install project dependencies' }
} finally {
    Pop-Location
}
