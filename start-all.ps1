param(
    [switch]$NoBuild,
    [int]$TimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$CoreCompose = Join-Path $Root "compose.yaml"
$LangfuseCompose = Join-Path $Root "deploy\langfuse\docker-compose.yml"
$AttuCompose = Join-Path $Root "deploy\attu\compose.yaml"
$EnvFile = Join-Path $Root ".env"
$EnvExampleFile = Join-Path $Root ".env.example"

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
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5

            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) {
                Write-Host ("{0} is ready: {1}" -f $Name, $Url) -ForegroundColor Green
                return
            }
        }
        catch {
            Write-Host ("Waiting for {0}: {1}" -f $Name, $Url)
        }

        Start-Sleep -Seconds 5
    }

    throw ("HTTP service startup timeout: {0}, URL: {1}" -f $Name, $Url)
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
Write-Host "[1/5] Starting Langfuse..." -ForegroundColor Cyan

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
Write-Host "[2/5] Starting core infrastructure..." -ForegroundColor Cyan

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
Write-Host "[3/5] Starting Milvus..." -ForegroundColor Cyan

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
Write-Host "[4/5] Starting application APIs..." -ForegroundColor Cyan

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

Write-Host ""
Write-Host "[5/5] Starting Attu..." -ForegroundColor Cyan

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
Write-Host "Langfuse:          http://127.0.0.1:3000"
Write-Host "Attu:              http://127.0.0.1:3001"
Write-Host "Milvus WebUI:      http://127.0.0.1:9091/webui/"
Write-Host "MinIO Console:     http://127.0.0.1:19001"
Write-Host "Langfuse MinIO:    http://127.0.0.1:9191"