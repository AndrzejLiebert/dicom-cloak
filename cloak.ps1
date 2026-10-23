# DICOM Cloak launcher for Windows (started by cloak.bat; macOS and Linux: cloak.sh).
#
#   cloak.bat                                asks for the input and output folders (drag and drop works)
#   cloak.bat INPUT_FOLDER OUTPUT_FOLDER
#   cloak.bat check FOLDER                   re-run the leak check on a de-identified folder
#   cloak.bat selftest                       run the built-in tests inside the container
#
# Options (before the folders):
#   --secrets DIR   folder for the secret files (default: %USERPROFILE%\dicom_cloak_secrets)
#   --prefix TEXT   start of automatic pseudonyms (default: SUBJ_ -> SUBJ_0001, SUBJ_0002, ...)
#   --ctp           use the RSNA CTP engine instead of the Python engine
#   --rebuild       rebuild the Docker image (after updating this folder)
#
# The container runs without network access. The input folder is mounted read-only.

$Here = $PSScriptRoot
$Version = (Select-String -Path (Join-Path $Here "src\dicom-cloak\__init__.py") -Pattern '^__version__ = "(.*)"').Matches[0].Groups[1].Value
$UserHome = $env:USERPROFILE
if (-not $UserHome) { $UserHome = $HOME }
$Secrets = $env:DICOM_CLOAK_SECRETS
if (-not $Secrets) { $Secrets = Join-Path $UserHome "dicom_cloak_secrets" }
$Prefix = $env:DICOM_CLOAK_PREFIX
if (-not $Prefix) { $Prefix = "SUBJ_" }
$Engine = "python"
$Rebuild = $false

function Fail([string]$msg) {
    Write-Host ""
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- arguments
$rest = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    $a = [string]$args[$i]
    switch ($a) {
        "--secrets" { $i++; if ($i -ge $args.Count) { Fail "--secrets needs a folder" }; $Secrets = [string]$args[$i] }
        "--prefix"  { $i++; if ($i -ge $args.Count) { Fail "--prefix needs a text" }; $Prefix = [string]$args[$i] }
        "--ctp"     { $Engine = "ctp" }
        "--rebuild" { $Rebuild = $true }
        { $_ -in "-h", "--help", "/?" } {
            Get-Content $PSCommandPath | Select-Object -Skip 1 -First 13 | ForEach-Object { $_ -replace '^# ?', '' }
            exit 0
        }
        default     { $rest += $a }
    }
}
if ($Engine -eq "ctp") { $Image = "dicom-cloak:$Version-ctp" } else { $Image = "dicom-cloak:$Version" }
if ($Prefix -notmatch '^[A-Za-z0-9_-]+$') { Fail "--prefix may only contain letters, digits, _ and -" }

# Paths dragged into the window may come in quotes.
function Clean-Path([string]$p) {
    $p = $p.Trim().Trim('"').Trim("'").Trim()
    if ($p -eq "~" -or $p.StartsWith("~/") -or $p.StartsWith("~\")) { $p = $UserHome + $p.Substring(1) }
    return $p
}

function Resolve-Full([string]$p) {
    # absolute path, also for a folder that does not exist yet (creates nothing)
    if (-not [System.IO.Path]::IsPathRooted($p)) { $p = Join-Path (Get-Location).Path $p }
    $p = [System.IO.Path]::GetFullPath($p).TrimEnd('\', '/')
    if ($p -match '^[A-Za-z]:$') { $p += '\' }   # drive root, e.g. a USB stick or CD: E:\
    return $p
}

function Test-Inside([string]$a, [string]$b) {
    # true if folder a is b or lies inside b (case-insensitive, as on Windows)
    $sep = [System.IO.Path]::DirectorySeparatorChar
    return ($a.TrimEnd('\', '/') + $sep).ToLower().StartsWith(($b.TrimEnd('\', '/') + $sep).ToLower())
}

function Ask([string]$question) {
    Write-Host ""
    Write-Host $question
    return Clean-Path (Read-Host ">")
}

# ---------------------------------------------------------------- Docker
function Need-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail "Docker is not installed. Install Docker Desktop as described in README.md, then try again."
    }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Fail "Docker Desktop is installed but not running. Start Docker Desktop, wait until it says it is running, then try again."
    }
    & docker image inspect $Image *> $null
    if ($Rebuild -or $LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Building the Docker image $Image (first use only, needs internet, takes a few minutes) ..."
        & docker build -f (Join-Path $Here "docker\Dockerfile") --target $Engine -t $Image $Here
        if ($LASTEXITCODE -ne 0) { Fail "Building the Docker image failed (see the messages above). Check the internet connection." }
    }
}

function Invoke-Check([string]$dir) {
    & docker run --rm --network none -v "${dir}:/out:ro" -v "$(Join-Path $Here 'config'):/opt/dicom_cloak/config:ro" `
        --entrypoint dicom-cloak-check $Image /out --require-remapped-uids 2>&1 | ForEach-Object { "$_" }
}

# ---------------------------------------------------------------- commands
if ($rest.Count -ge 1 -and $rest[0] -eq "selftest") {
    Need-Docker
    Write-Host "Running the built-in tests in $Image ..."
    & docker run --rm --network none --entrypoint python $Image -m pytest -q -p no:cacheprovider tests 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -eq 0) { Write-Host ""; Write-Host "SELFTEST PASSED" -ForegroundColor Green; exit 0 }
    Write-Host ""; Write-Host "SELFTEST FAILED" -ForegroundColor Red; exit 1
}

if ($rest.Count -ge 1 -and $rest[0] -eq "check") {
    if ($rest.Count -ne 2) { Fail "usage: cloak.bat check FOLDER" }
    $dir = Resolve-Full (Clean-Path $rest[1])
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { Fail "folder not found: $dir" }
    Need-Docker
    Invoke-Check $dir
    if ($LASTEXITCODE -eq 0) { Write-Host ""; Write-Host "CHECK PASSED: no identifying information found in the headers." -ForegroundColor Green; exit 0 }
    Write-Host ""; Write-Host "CHECK FAILED: see the list above. Do not share this folder." -ForegroundColor Red; exit 1
}

if ($rest.Count -gt 2) { Fail "too many arguments (put folder paths with spaces in quotes). See: cloak.bat --help" }

Write-Host "DICOM Cloak $Version (MAGIC Lab): de-identification of MRI DICOM data"
$In = ""; $Out = ""
if ($rest.Count -ge 1) { $In = Clean-Path $rest[0] }
if ($rest.Count -ge 2) { $Out = Clean-Path $rest[1] }
if (-not $In) { $In = Ask "Drag the INPUT folder (the original DICOM data) into this window and press Enter:" }
if (-not $Out) { $Out = Ask "Drag or type the OUTPUT folder (new or empty) for the de-identified copy and press Enter:" }
if (-not $In -or -not $Out) { Fail "input and output folder are both needed" }

$InAbs = Resolve-Full $In
if (-not (Test-Path -LiteralPath $InAbs -PathType Container)) { Fail "input folder not found: $InAbs" }
$OutAbs = Resolve-Full $Out
$SecAbs = Resolve-Full (Clean-Path $Secrets)

if (Test-Inside $OutAbs $InAbs) { Fail "the output folder must not be inside the input folder" }
if (Test-Inside $InAbs $OutAbs) { Fail "the input folder must not be inside the output folder" }
foreach ($d in @($InAbs, $OutAbs)) {
    if ((Test-Inside $SecAbs $d) -or (Test-Inside $d $SecAbs)) {
        Fail "the secrets folder ($SecAbs) must be separate from the input and output folders"
    }
}
if (Test-Path -LiteralPath $OutAbs) {
    $content = Get-ChildItem -LiteralPath $OutAbs -Force | Where-Object { $_.Name -notin ".DS_Store", "desktop.ini", "Thumbs.db" }
    if ($content) { Fail "the output folder is not empty: $OutAbs`n       Choose a new or empty folder for every run." }
}
New-Item -ItemType Directory -Force -Path $OutAbs -ErrorAction SilentlyContinue | Out-Null
if (-not (Test-Path -LiteralPath $OutAbs)) { Fail "cannot create the output folder: $OutAbs" }
New-Item -ItemType Directory -Force -Path (Join-Path $SecAbs "logs") -ErrorAction SilentlyContinue | Out-Null
if (-not (Test-Path -LiteralPath $SecAbs)) { Fail "cannot create the secrets folder: $SecAbs" }

Need-Docker

$Log = Join-Path $SecAbs ("logs\run_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".txt")
Write-Host ""
Write-Host "Input:   $InAbs (read-only)"
Write-Host "Output:  $OutAbs"
Write-Host "Secrets: $SecAbs (key, pseudonym list, visit registry, logs: keep private)"
Write-Host "Engine:  $Engine"
Write-Host ""

$runArgs = @("run", "--rm", "--network", "none",
    "-v", "${InAbs}:/in:ro", "-v", "${OutAbs}:/out", "-v", "${SecAbs}:/secrets",
    "-v", "$(Join-Path $Here 'config'):/opt/dicom_cloak/config:ro",
    $Image, "run", "--src", "/in", "--dst", "/out")
if ($Engine -eq "ctp") { $runArgs += @("--work", "/tmp/ctp_work") }
$runArgs += @("--pseudonym-map", "/secrets/pseudonym_map.csv", "--auto-pseudonyms", $Prefix,
    "--uid-key", "/secrets/uid_key.hex", "--study-registry", "/secrets/study_registry.csv")
& docker @runArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $Log
$RunStatus = $LASTEXITCODE

Write-Host ""
Write-Host "Checking the result ..."
Invoke-Check $OutAbs | Tee-Object -FilePath $Log -Append
$CheckStatus = $LASTEXITCODE

Write-Host ""
Write-Host "=================================================================="
if ($RunStatus -eq 0 -and $CheckStatus -eq 0) {
    Write-Host " RESULT: PASS" -ForegroundColor Green
    Write-Host " De-identified copy:  $OutAbs"
    Write-Host " Overview tables:     $(Join-Path $OutAbs '_deid_meta')"
    Write-Host " Before sharing, open a few images and look for names or dates burned into the pixels"
    Write-Host " (screenshots, MIPs, scanned forms): this tool only cleans the DICOM headers."
} else {
    Write-Host " RESULT: FAILED (run=$RunStatus, check=$CheckStatus). Do NOT share the output folder." -ForegroundColor Red
    Write-Host " Details: $Log"
}
Write-Host ""
Write-Host " Keep this folder PRIVATE and backed up; never put it next to the shared data:"
Write-Host "   $SecAbs"
Write-Host " It links pseudonyms to real patient IDs and keeps pseudonyms stable across runs."
Write-Host "=================================================================="
if ($RunStatus -eq 0 -and $CheckStatus -eq 0) { exit 0 }
exit 1
