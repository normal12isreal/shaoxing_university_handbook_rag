param(
    [ValidateSet('flash', 'basic', 'standard')]
    [string]$Tier = 'flash',
    [string]$Pages = 'all',
    [string]$OutputName = 'flash-v1',
    [ValidateSet('', 'auto', 'txt', 'ocr')]
    [string]$OcrMode = ''
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run scripts\setup.ps1 first.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:MINERU_HOME = Join-Path $projectRoot '.runtime\mineru'
$env:MINERU_MODEL_SOURCE = if ($Tier -eq 'flash') { 'local' } else { 'modelscope' }
$env:MINERU_MODEL_SMALL_BACKEND = 'onnx'
$env:GRADIO_ANALYTICS_ENABLED = 'False'
Push-Location (Join-Path $projectRoot 'tools\MinerU')
try {
    $arguments = @((Join-Path $PSScriptRoot 'extract.py'), '--tier', $Tier, '--pages', $Pages, '--output-name', $OutputName)
    if ($OcrMode) { $arguments += @('--ocr-mode', $OcrMode) }
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Extraction failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}
