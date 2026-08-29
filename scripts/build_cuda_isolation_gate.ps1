[CmdletBinding()]
param(
    [string]$ModelPath = ".runtime\Models\small"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$spec = Join-Path $repoRoot "packaging\VideoChapterTool.spec"
$distRoot = Join-Path $repoRoot "build\cuda-isolation-dist"
$workRoot = Join-Path $repoRoot "build\cuda-isolation-pyinstaller"
$matplotlibCache = Join-Path $repoRoot "build\matplotlib"
$appFolder = Join-Path $distRoot "VideoChapterTool"
$exePath = Join-Path $appFolder "VideoChapterTool.exe"
$reportPath = Join-Path $workRoot "cuda-isolation-report.json"
$testData = Join-Path $workRoot "UserData"
$resolvedModel = (Resolve-Path -LiteralPath (Join-Path $repoRoot $ModelPath)).Path

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project virtual environment not found: $python"
}
if (-not (Test-Path -LiteralPath $resolvedModel -PathType Container)) {
    throw "Whisper model directory not found: $resolvedModel"
}

$cudaRoot = $env:CUDA_PATH
if (-not $cudaRoot -or -not (Test-Path -LiteralPath (Join-Path $cudaRoot "bin\cublas64_12.dll"))) {
    $cudaRoot = Get-ChildItem -LiteralPath "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "v12.*" } |
        Sort-Object Name -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $cudaRoot) {
    throw "CUDA 12 build source was not found. It is needed only to assemble the NVIDIA package."
}
$cudaBin = Join-Path $cudaRoot "bin"
foreach ($library in ("cublas64_12.dll", "cublasLt64_12.dll")) {
    if (-not (Test-Path -LiteralPath (Join-Path $cudaBin $library) -PathType Leaf)) {
        throw "CUDA 12 build source is missing: $library"
    }
}

$previousLocation = Get-Location
$originalPath = $env:PATH
$originalCudaPath = $env:CUDA_PATH
$originalCudnnPath = $env:CUDNN_PATH
$originalDisableDiscovery = $env:VCT_DISABLE_EXTERNAL_CUDA_DISCOVERY
try {
    Set-Location -LiteralPath $repoRoot
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:MPLCONFIGDIR = $matplotlibCache
    $env:VCT_CUDA12_BIN = $cudaBin

    & $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot $spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller CUDA isolation build failed."
    }
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Packaged executable was not found: $exePath"
    }

    $env:PATH = "$env:WINDIR\System32;$env:WINDIR"
    Remove-Item Env:CUDA_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:CUDNN_PATH -ErrorAction SilentlyContinue
    $env:VCT_DISABLE_EXTERNAL_CUDA_DISCOVERY = "1"

    $gateProcess = Start-Process -FilePath $exePath -ArgumentList @(
        "--data-dir", ('"' + $testData + '"'),
        "--cuda-isolation-test-report", ('"' + $reportPath + '"'),
        "--cuda-isolation-model-dir", ('"' + $resolvedModel + '"')
    ) -PassThru -Wait -WindowStyle Hidden
    if ($gateProcess.ExitCode -ne 0) {
        throw "Packaged CUDA isolation Gate failed; inspect $reportPath"
    }

    $report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
    if ($report.status -ne "pass") {
        throw "Packaged CUDA isolation report did not pass: $reportPath"
    }
    if (-not $report.external_cuda_discovery_disabled) {
        throw "CUDA isolation report did not disable external discovery."
    }
    Write-Host "Packaged CUDA isolation Gate: pass"
    Write-Host "GPU: $($report.gpu)"
    Write-Host "Whisper: $($report.whisper_runtime.resolved_device)/$($report.whisper_runtime.compute_type)"
    Write-Host "Report: $reportPath"
}
finally {
    Set-Location -LiteralPath $previousLocation
    $env:PATH = $originalPath
    if ($null -eq $originalCudaPath) {
        Remove-Item Env:CUDA_PATH -ErrorAction SilentlyContinue
    }
    else {
        $env:CUDA_PATH = $originalCudaPath
    }
    if ($null -eq $originalCudnnPath) {
        Remove-Item Env:CUDNN_PATH -ErrorAction SilentlyContinue
    }
    else {
        $env:CUDNN_PATH = $originalCudnnPath
    }
    if ($null -eq $originalDisableDiscovery) {
        Remove-Item Env:VCT_DISABLE_EXTERNAL_CUDA_DISCOVERY -ErrorAction SilentlyContinue
    }
    else {
        $env:VCT_DISABLE_EXTERNAL_CUDA_DISCOVERY = $originalDisableDiscovery
    }
    Remove-Item Env:VCT_CUDA12_BIN -ErrorAction SilentlyContinue
}
