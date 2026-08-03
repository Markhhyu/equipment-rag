param(
    [switch]$NoBuild,
    [int]$TimeoutSeconds = 600,
    [int]$MineruPort = 8002
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$CoreCompose = Join-Path $Root "compose.yaml"
$LangfuseCompose = Join-Path $Root "deploy\langfuse\docker-compose.yml"
$AttuCompose = Join-Path $Root "deploy\attu\compose.yaml"
$EnvFile = Join-Path $Root ".env"
$EnvExampleFile = Join-Path $Root ".env.example"

$MineruRoot = Join-Path $Root "deploy\mineru-runtime"
$MineruPidFile = Join-Path $MineruRoot ".mineru.pid"
$MineruLogDir = Join-Path $MineruRoot "logs"
$MineruOutLog = Join-Path $MineruLogDir "mineru.stdout.log"
$MineruErrLog = Join-Path $MineruLogDir "mineru.stderr.log"
$MineruHealthUrl = "http://127.0.0.1:$MineruPort/health"

Set-Location $Root

function Assert-FileExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path $Path)) {
        throw "Required file not found: $Path"
    }
}

function Invoke-DockerCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$DockerArgs
    )

    Write-Host ""
    Write-Host "> docker $($DockerArgs -join ' ')" -ForegroundColor Cyan

    & docker @DockerArgs

    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed: docker $($DockerArgs -join ' ')"
    }
}

function Get-ContainerStatus {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ContainerName
    )

    $status = & docker inspect `
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' `
        $ContainerName 2>$null

    if ($LASTEXITCODE -ne 0) {
        return "missing"
    }

    return [string]($status | Select-Object -First 1)
}

function Wait-ContainerHealthy {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ContainerName,

        [Parameter(Mandatory = $true)]
        [int]$Timeout
    )

    $deadline = (Get-Date).AddSeconds($Timeout)

    while ((Get-Date) -lt $deadline) {
        $status = (Get-ContainerStatus -ContainerName $ContainerName).Trim()

        Write-Host "Waiting for container: $ContainerName, status: $status"

        if ($status -eq "healthy") {
            Write-Host "Container is healthy: $ContainerName" -ForegroundColor Green
            return
        }

        if (
            $status -eq "unhealthy" -or
            $status -eq "exited" -or
            $status -eq "dead"
        ) {
            Write-Host ""
            Write-Host "Container logs:" -ForegroundColor Yellow
            & docker logs --tail 200 $ContainerName

            throw "Container failed: $ContainerName, status: $status"
        }

        Start-Sleep -Seconds 5
    }

    Write-Host ""
    Write-Host "Container logs:" -ForegroundColor Yellow
    & docker logs --tail 200 $ContainerName

    throw "Container startup timeout: $ContainerName"
}

function Test-HttpService {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url
    )

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 400
    }
    catch {
        return $false
    }
}

function Wait-HttpService {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Url,

        [Parameter(Mandatory = $true)]
        [int]$Timeout
    )

    $deadline = (Get-Date).AddSeconds($Timeout)

    while ((Get-Date) -lt $deadline) {
        if (Test-HttpService -Url $Url) {
            Write-Host ("{0} is ready: {1}" -f $Name, $Url) -ForegroundColor Green
            return
        }

        Write-Host ("Waiting for {0}: {1}" -f $Name, $Url)
        Start-Sleep -Seconds 5
    }

    throw ("HTTP service startup timeout: {0}, URL: {1}" -f $Name, $Url)
}

function Get-ListeningProcessIds {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $connections = @(
        Get-NetTCPConnection `
            -LocalPort $Port `
            -State Listen `
            -ErrorAction SilentlyContinue
    )

    return @(
        $connections |
            ForEach-Object { $_.OwningProcess } |
            Sort-Object -Unique
    )
}

function Resolve-MineruExecutable {
    $candidates = @(
        (Join-Path $MineruRoot ".venv\Scripts\mineru-api.exe"),
        (Join-Path $MineruRoot "venv\Scripts\mineru-api.exe"),
        (Join-Path $MineruRoot "Scripts\mineru-api.exe")
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $command = Get-Command "mineru-api" -ErrorAction SilentlyContinue

    if ($null -ne $command) {
        return $command.Source
    }

    throw "MinerU executable not found. Expected deploy\mineru-runtime\.venv\Scripts\mineru-api.exe"
}

function Import-MineruEnvironment {
    if (-not (Test-Path $EnvFile)) {
        return
    }

    $supportedKeys = @{
        "MINERU_MODEL_SOURCE" = $true
        "MODELSCOPE_OFFLINE" = $true
        "MODELSCOPE_CACHE" = $true
        "HF_HOME" = $true
        "MINERU_CONFIG_FILE" = $true
    }

    foreach ($line in Get-Content $EnvFile) {
        $trimmed = $line.Trim()

        if (
            [string]::IsNullOrWhiteSpace($trimmed) -or
            $trimmed.StartsWith("#")
        ) {
            continue
        }

        $separatorIndex = $trimmed.IndexOf("=")

        if ($separatorIndex -le 0) {
            continue
        }

        $name = $trimmed.Substring(0, $separatorIndex).Trim()

        if (-not $supportedKeys.ContainsKey($name)) {
            continue
        }

        $currentValue = [Environment]::GetEnvironmentVariable($name, "Process")

        if (-not [string]::IsNullOrWhiteSpace($currentValue)) {
            continue
        }

        $value = $trimmed.Substring($separatorIndex + 1).Trim()

        if (
            $value.Length -ge 2 -and
            (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }

    if ([string]::IsNullOrWhiteSpace($env:MINERU_MODEL_SOURCE)) {
        $env:MINERU_MODEL_SOURCE = "modelscope"
    }
}

function Show-MineruLogs {
    Write-Host ""
    Write-Host "MinerU stdout:" -ForegroundColor Yellow

    if (Test-Path $MineruOutLog) {
        Get-Content $MineruOutLog -Tail 200
    }

    Write-Host ""
    Write-Host "MinerU stderr:" -ForegroundColor Yellow

    if (Test-Path $MineruErrLog) {
        Get-Content $MineruErrLog -Tail 200
    }
}

function Start-Mineru {
    if (Test-HttpService -Url $MineruHealthUrl) {
        Write-Host "MinerU is already running: $MineruHealthUrl" -ForegroundColor Green
        return
    }

    $listeningPids = @(Get-ListeningProcessIds -Port $MineruPort)

    if ($listeningPids.Count -gt 0) {
        throw "Port $MineruPort is occupied by PID(s): $($listeningPids -join ', '), but MinerU health check failed."
    }

    if (-not (Test-Path $MineruRoot)) {
        throw "MinerU runtime directory not found: $MineruRoot"
    }

    $mineruExecutable = Resolve-MineruExecutable

    Import-MineruEnvironment

    New-Item `
        -ItemType Directory `
        -Path $MineruLogDir `
        -Force *> $null

    Remove-Item $MineruOutLog -Force -ErrorAction SilentlyContinue
    Remove-Item $MineruErrLog -Force -ErrorAction SilentlyContinue
    Remove-Item $MineruPidFile -Force -ErrorAction SilentlyContinue

    Write-Host "Starting MinerU..." -ForegroundColor Cyan
    Write-Host "Executable: $mineruExecutable"
    Write-Host "Health URL: $MineruHealthUrl"
    Write-Host "Model source: $env:MINERU_MODEL_SOURCE"

    $process = Start-Process `
        -FilePath $mineruExecutable `
        -ArgumentList @(
            "--host",
            "0.0.0.0",
            "--port",
            "$MineruPort"
        ) `
        -WorkingDirectory $MineruRoot `
        -RedirectStandardOutput $MineruOutLog `
        -RedirectStandardError $MineruErrLog `
        -WindowStyle Hidden `
        -PassThru

    Set-Content `
        -Path $MineruPidFile `
        -Value $process.Id `
        -Encoding ASCII

    Write-Host "MinerU process started, PID: $($process.Id)"

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    while ((Get-Date) -lt $deadline) {
        if (Test-HttpService -Url $MineruHealthUrl) {
            Write-Host "MinerU is ready: $MineruHealthUrl" -ForegroundColor Green
            return
        }

        $runningProcess = Get-Process `
            -Id $process.Id `
            -ErrorAction SilentlyContinue

        if ($null -eq $runningProcess) {
            Show-MineruLogs
            throw "MinerU exited before becoming ready."
        }

        Write-Host "Waiting for MinerU: $MineruHealthUrl"
        Start-Sleep -Seconds 5
    }

    Show-MineruLogs

    & taskkill.exe /PID $process.Id /T /F *> $null
    Remove-Item $MineruPidFile -Force -ErrorAction SilentlyContinue

    throw "MinerU startup timeout: $MineruHealthUrl"
}

function Test-MineruFromImportApi {
    Write-Host ""
    Write-Host "Checking MinerU from import-api container..." -ForegroundColor Cyan

    Invoke-DockerCommand -DockerArgs @(
        "compose",
        "--project-name", "equipment-rag",
        "-f", $CoreCompose,
        "exec", "-T",
        "import-api",
        "python",
        "-c",
        "import os, requests; base=(os.getenv('MINERU_API_BASE_URL') or '').rstrip('/'); url=base + '/health'; print('MinerU URL:', url); response=requests.get(url, timeout=10); print('Status:', response.status_code); print('Body:', response.text); response.raise_for_status()"
    )
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " Equipment RAG Agent Full Startup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

& docker info *> $null

if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running or Docker Engine is unavailable."
}

Assert-FileExists -Path $CoreCompose
Assert-FileExists -Path $LangfuseCompose
Assert-FileExists -Path $AttuCompose
Assert-FileExists -Path $EnvExampleFile

if (-not (Test-Path $EnvFile)) {
    Copy-Item $EnvExampleFile $EnvFile
    Write-Warning ".env was created from .env.example. Configure the LLM API settings."
}

Write-Host ""
Write-Host "[1/6] Starting Langfuse..." -ForegroundColor Cyan

Invoke-DockerCommand -DockerArgs @(
    "compose",
    "--project-name", "equipment-rag-langfuse",
    "-f", $LangfuseCompose,
    "up", "-d"
)

Wait-HttpService `
    -Name "Langfuse" `
    -Url "http://127.0.0.1:3000/api/public/health" `
    -Timeout $TimeoutSeconds

Write-Host ""
Write-Host "[2/6] Starting core infrastructure..." -ForegroundColor Cyan

Invoke-DockerCommand -DockerArgs @(
    "compose",
    "--project-name", "equipment-rag",
    "-f", $CoreCompose,
    "up", "-d",
    "mongo", "minio", "etcd"
)

Wait-ContainerHealthy `
    -ContainerName "equipment-rag-mongo-1" `
    -Timeout $TimeoutSeconds

Wait-ContainerHealthy `
    -ContainerName "equipment-rag-minio-1" `
    -Timeout $TimeoutSeconds

Wait-ContainerHealthy `
    -ContainerName "equipment-rag-etcd-1" `
    -Timeout $TimeoutSeconds

Write-Host ""
Write-Host "[3/6] Starting Milvus..." -ForegroundColor Cyan

Invoke-DockerCommand -DockerArgs @(
    "compose",
    "--project-name", "equipment-rag",
    "-f", $CoreCompose,
    "up", "-d",
    "milvus"
)

Wait-ContainerHealthy `
    -ContainerName "equipment-rag-milvus-1" `
    -Timeout $TimeoutSeconds

Write-Host ""
Write-Host "[4/6] Starting MinerU..." -ForegroundColor Cyan

Start-Mineru

Write-Host ""
Write-Host "[5/6] Starting application APIs..." -ForegroundColor Cyan

$ApiArgs = @(
    "compose",
    "--project-name", "equipment-rag",
    "-f", $CoreCompose,
    "up", "-d"
)

if (-not $NoBuild) {
    $ApiArgs += "--build"
}

$ApiArgs += @(
    "import-api",
    "query-api"
)

Invoke-DockerCommand -DockerArgs $ApiArgs

Wait-HttpService `
    -Name "Import API" `
    -Url "http://127.0.0.1:8000/health" `
    -Timeout $TimeoutSeconds

Wait-HttpService `
    -Name "Query API" `
    -Url "http://127.0.0.1:8001/health" `
    -Timeout $TimeoutSeconds

Test-MineruFromImportApi

Write-Host ""
Write-Host "[6/6] Starting Attu..." -ForegroundColor Cyan

Invoke-DockerCommand -DockerArgs @(
    "compose",
    "--project-name", "equipment-rag-attu",
    "-f", $AttuCompose,
    "up", "-d"
)

Wait-HttpService `
    -Name "Attu" `
    -Url "http://127.0.0.1:3001" `
    -Timeout $TimeoutSeconds

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host " All services started successfully" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host "Import page:       http://127.0.0.1:8000/import.html"
Write-Host "Chat page:         http://127.0.0.1:8001/chat.html"
Write-Host "Import Swagger:    http://127.0.0.1:8000/docs"
Write-Host "Query Swagger:     http://127.0.0.1:8001/docs"
Write-Host "MinerU API:        http://127.0.0.1:$MineruPort/docs"
Write-Host "Langfuse:          http://127.0.0.1:3000"
Write-Host "Attu:              http://127.0.0.1:3001"
Write-Host "Milvus WebUI:      http://127.0.0.1:9091/webui/"
Write-Host "MinIO Console:     http://127.0.0.1:19001"
Write-Host "Langfuse MinIO:    http://127.0.0.1:9191"
Write-Host "MinerU stdout:     $MineruOutLog"
Write-Host "MinerU stderr:     $MineruErrLog"