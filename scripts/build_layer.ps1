<#
.SYNOPSIS
Build the arm64 dependency layers without Docker: the PowerShell version of
scripts/build_layer.sh, for Windows.

.DESCRIPTION
ingestion -> build\layer\python            (layer\requirements.txt)
knowledge -> build\knowledge-layer\python  (layer\knowledge-requirements.txt)

pip downloads ready-built Linux/arm64 wheels for Lambda's Python version, so this works
on an x86 Windows machine. Run it from the project folder with the virtual environment
active.

.EXAMPLE
.\scripts\build_layer.ps1
.\scripts\build_layer.ps1 knowledge
#>
param(
    [ValidateSet("all", "ingestion", "knowledge")]
    [string]$What = "all"
)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$PyVersion = (Get-Content -Raw -Encoding utf8 (Join-Path $Root ".lambda-python-version")).Trim()
$PyTag = $PyVersion.Replace(".", "")
$Abi = "cpython-$PyTag-aarch64-linux-gnu.so"

# The Lambda runtime ships boto3 and its dependency tree. A second copy in the layer
# would shadow the runtime's versions (layers come first on sys.path).
$RuntimeProvided = @(
    "boto3", "boto3-*", "botocore", "botocore-*", "s3transfer", "s3transfer-*",
    "urllib3", "urllib3-*", "dateutil", "python_dateutil-*", "six.py", "six-*",
    "jmespath", "jmespath-*"
)

function Build-Layer {
    param([string]$Name, [string]$Requirements, [string]$Out, [string[]]$Compiled)

    $Target = Join-Path $Out "python"
    if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
    New-Item -ItemType Directory -Force -Path $Target | Out-Null

    python -m pip install `
        --platform manylinux2014_aarch64 `
        --implementation cp `
        --python-version $PyVersion `
        --only-binary=:all: `
        --target $Target `
        --no-compile `
        --quiet `
        -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "pip failed while building the $Name layer" }

    foreach ($pattern in $RuntimeProvided) {
        Get-ChildItem -Path $Target -Filter $pattern -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force
    }
    Get-ChildItem -Path $Target -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    # Fail loudly if a compiled package came out for the wrong platform or ABI.
    foreach ($pkg in $Compiled) {
        $so = Get-ChildItem -Path (Join-Path $Target $pkg) -Recurse -Filter "*.so" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $so) { throw "no compiled extension found for $pkg ($Name layer)" }
        if (-not $so.Name.EndsWith($Abi)) {
            throw "$pkg extension is not cp$PyTag/aarch64: $($so.Name)"
        }
    }

    $megabytes = (Get-ChildItem -Recurse -File $Out | Measure-Object -Property Length -Sum).Sum / 1MB
    "{0} layer built for python{1}/arm64 at {2} ({3:N0} MB)" -f $Name, $PyVersion, $Out, $megabytes
}

if ($What -in @("all", "ingestion")) {
    Build-Layer -Name "ingestion" `
        -Requirements (Join-Path $Root "layer\requirements.txt") `
        -Out (Join-Path $Root "build\layer") `
        -Compiled @("pydantic_core", "ijson")
}
if ($What -in @("all", "knowledge")) {
    Build-Layer -Name "knowledge" `
        -Requirements (Join-Path $Root "layer\knowledge-requirements.txt") `
        -Out (Join-Path $Root "build\knowledge-layer") `
        -Compiled @("pydantic_core", "lxml")
}
