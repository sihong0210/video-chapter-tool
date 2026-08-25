[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipArchive,
    [switch]$WithoutCuda12Runtime
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$spec = Join-Path $repoRoot "packaging\VideoChapterTool.spec"
$releaseRoot = Join-Path $repoRoot "release"
$distRoot = Join-Path $releaseRoot "v0.2.0"
$workRoot = Join-Path $repoRoot "build\pyinstaller-release"
$matplotlibCache = Join-Path $repoRoot "build\matplotlib"
$appFolder = Join-Path $distRoot "VideoChapterTool"
$version = "0.2.0"
$archive = Join-Path $releaseRoot "VideoChapterTool-$version-win64.zip"
$portableGuide = Join-Path $repoRoot "docs\PORTABLE_README_zh-TW.md"
$releaseNotes = Join-Path $repoRoot "docs\RELEASE_NOTES_0.2.0_zh-TW.md"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project virtual environment not found: $python"
}

Remove-Item Env:VCT_CUDA12_BIN -ErrorAction SilentlyContinue
if (-not $WithoutCuda12Runtime) {
    $cudaRoot = $env:CUDA_PATH
    if (-not $cudaRoot -or -not (Test-Path -LiteralPath (Join-Path $cudaRoot "bin\cublas64_12.dll"))) {
        $cudaRoot = Get-ChildItem -LiteralPath "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "v12.*" } |
            Sort-Object Name -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if ($cudaRoot) {
        $cudaBin = Join-Path $cudaRoot "bin"
        if (Test-Path -LiteralPath (Join-Path $cudaBin "cublas64_12.dll")) {
            $env:VCT_CUDA12_BIN = $cudaBin
            Write-Host "Bundling CTranslate2 CUDA 12 BLAS from: $cudaBin"
        }
    }
}

$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $repoRoot
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:MPLCONFIGDIR = $matplotlibCache

    if (-not $SkipTests) {
        & $python -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) {
            throw "Automated tests failed; release build stopped."
        }
    }

    New-Item -ItemType Directory -Path $distRoot -Force | Out-Null
    & $python -m PyInstaller --noconfirm --clean --distpath $distRoot --workpath $workRoot $spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }

    $exePath = Join-Path $appFolder "VideoChapterTool.exe"
    if (-not (Test-Path -LiteralPath $exePath -PathType Leaf)) {
        throw "Build finished but executable was not found: $exePath"
    }

    Copy-Item -LiteralPath $portableGuide -Destination (Join-Path $appFolder "README_zh-TW.md") -Force
    Copy-Item -LiteralPath $releaseNotes -Destination (Join-Path $appFolder "RELEASE_NOTES_zh-TW.md") -Force

    $portableData = Join-Path $appFolder "UserData"
    if (Test-Path -LiteralPath $portableData) {
        throw "Portable build unexpectedly contains UserData before first launch."
    }
    $portableProcess = Start-Process -FilePath $exePath -ArgumentList @(
        "--smoke-test"
    ) -PassThru -Wait -WindowStyle Hidden
    if ($portableProcess.ExitCode -ne 0) {
        throw "Packaged portable sibling-data smoke test failed with exit code $($portableProcess.ExitCode)."
    }
    $portableExpected = @(
        (Join-Path $portableData "Config\settings.json"),
        (Join-Path $portableData "Database\video_chapter_tool.db"),
        (Join-Path $portableData "Logs\application.log")
    )
    foreach ($expected in $portableExpected) {
        if (-not (Test-Path -LiteralPath $expected -PathType Leaf)) {
            throw "Portable sibling-data smoke test did not create: $expected"
        }
    }
    $resolvedAppFolder = (Resolve-Path -LiteralPath $appFolder).Path
    $resolvedPortableData = (Resolve-Path -LiteralPath $portableData).Path
    if ((Split-Path -Parent $resolvedPortableData) -ne $resolvedAppFolder) {
        throw "Refusing to clean unexpected portable test data path: $resolvedPortableData"
    }
    Remove-Item -LiteralPath $resolvedPortableData -Recurse -Force

    $gateRoot = Join-Path $workRoot "release-gates"
    New-Item -ItemType Directory -Path $gateRoot -Force | Out-Null
    $smokeData = Join-Path $gateRoot "GUI Chinese Path"
    $smokeProcess = Start-Process -FilePath $exePath -ArgumentList @(
        "--data-dir", ('"' + $smokeData + '"'), "--smoke-test"
    ) -PassThru -Wait -WindowStyle Hidden
    if ($smokeProcess.ExitCode -ne 0) {
        throw "Packaged GUI smoke test failed with exit code $($smokeProcess.ExitCode)."
    }

    $runtimeReport = Join-Path $gateRoot "runtime-self-test.json"
    $runtimeProcess = Start-Process -FilePath $exePath -ArgumentList @(
        "--data-dir", ('"' + $smokeData + '"'),
        "--runtime-self-test-report", ('"' + $runtimeReport + '"')
    ) -PassThru -Wait -WindowStyle Hidden
    if ($runtimeProcess.ExitCode -ne 0) {
        throw "Packaged runtime self-test failed; inspect $runtimeReport"
    }

    $originalPath = $env:PATH
    try {
        $env:PATH = "$env:WINDIR\System32;$env:WINDIR"
        $env:CUDA_VISIBLE_DEVICES = "-1"
        $cpuReport = Join-Path $gateRoot "runtime-self-test-cpu-fallback.json"
        $cpuData = Join-Path $gateRoot "CPU Chinese Path"
        $cpuProcess = Start-Process -FilePath $exePath -ArgumentList @(
            "--data-dir", ('"' + $cpuData + '"'),
            "--runtime-self-test-report", ('"' + $cpuReport + '"')
        ) -PassThru -Wait -WindowStyle Hidden
    }
    finally {
        $env:PATH = $originalPath
        Remove-Item Env:CUDA_VISIBLE_DEVICES -ErrorAction SilentlyContinue
    }
    if ($cpuProcess.ExitCode -ne 0) {
        throw "Packaged CPU fallback self-test failed; inspect $cpuReport"
    }

    Copy-Item -LiteralPath $runtimeReport -Destination (Join-Path $appFolder "runtime-self-test.json") -Force
    Copy-Item -LiteralPath $cpuReport -Destination (Join-Path $appFolder "runtime-self-test-cpu-fallback.json") -Force

    $fileCount = (Get-ChildItem -LiteralPath $appFolder -Recurse -File | Measure-Object).Count
    $totalBytes = (Get-ChildItem -LiteralPath $appFolder -Recurse -File | Measure-Object -Property Length -Sum).Sum
    $exeHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
    $manifest = [ordered]@{
        product = "VideoChapterTool"
        version = $version
        channel = "stable"
        architecture = "win64"
        format = "one-folder"
        models_bundled = $false
        cuda12_blas_bundled = [bool]$env:VCT_CUDA12_BIN
        gui_smoke_test = "pass"
        portable_sibling_data_test = "pass"
        packaged_runtime_self_test = "pass"
        packaged_cpu_fallback_self_test = "pass"
        file_count = $fileCount
        unpacked_bytes = $totalBytes
        executable_sha256 = $exeHash
        built_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $appFolder "release-manifest.json") -Encoding UTF8

    if (-not $SkipArchive) {
        if (Test-Path -LiteralPath $archive) {
            Remove-Item -LiteralPath $archive -Force
        }
        Compress-Archive -LiteralPath $appFolder -DestinationPath $archive -CompressionLevel Optimal
        $archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        "$archiveHash *$(Split-Path -Leaf $archive)" |
            Set-Content -LiteralPath "$archive.sha256" -Encoding ASCII
    }

    Write-Host "Release build completed: $exePath"
    Write-Host ("Unpacked size: {0:N2} GiB; files: {1}" -f ($totalBytes / 1GB), $fileCount)
    if (-not $SkipArchive) {
        Write-Host "Archive: $archive"
    }
}
finally {
    Set-Location -LiteralPath $previousLocation
}
