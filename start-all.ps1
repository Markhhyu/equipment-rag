[CmdletBinding()]
param(
    # 默认会重新构建两个应用镜像。代码没有变化时传入 -SkipBuild 可以明显加快启动。
    [Alias("NoBuild")]
    [switch]$SkipBuild,

    # 只启动业务必需组件：数据库、向量库、两个 API 和 MinerU。
    # Langfuse、Prometheus/Grafana、Attu 都不会启动，适合低内存机器临时开发。
    [switch]$CoreOnly,

    # 以下开关用于单独跳过某个可选组件；正常使用时不需要传入。
    [switch]$NoLangfuse,
    [switch]$NoObservability,
    [switch]$NoAttu,
    [switch]$NoMineru,

    # 单个服务最长等待时间。首次下载镜像或模型较慢时可以适当调大。
    [ValidateRange(30, 7200)]
    [int]$TimeoutSeconds = 600,

    # MinerU 在宿主机监听的端口，必须与根目录 .env 中的 MINERU_API_BASE_URL 对应。
    [ValidateRange(1, 65535)]
    [int]$MineruPort = 8002
)

$ErrorActionPreference = "Stop"

# 所有路径都以脚本所在的仓库根目录为基准，因此从任意目录执行脚本都能正常工作。
$Root = $PSScriptRoot
$CoreCompose = Join-Path $Root "compose.yaml"
$LangfuseRoot = Join-Path $Root "deploy\langfuse"
$LangfuseCompose = Join-Path $LangfuseRoot "docker-compose.yml"
$LangfuseEnv = Join-Path $LangfuseRoot ".env"
$LangfuseEnvExample = Join-Path $LangfuseRoot ".env.example"
$AttuCompose = Join-Path $Root "deploy\attu\compose.yaml"
$EnvFile = Join-Path $Root ".env"
$EnvExampleFile = Join-Path $Root ".env.example"

$MineruRoot = Join-Path $Root "deploy\mineru-runtime"
$MineruPidFile = Join-Path $MineruRoot ".mineru.pid"
$MineruLogDir = Join-Path $MineruRoot "logs"
$MineruOutLog = Join-Path $MineruLogDir "mineru.stdout.log"
$MineruErrLog = Join-Path $MineruLogDir "mineru.stderr.log"
$MineruHealthUrl = "http://127.0.0.1:$MineruPort/health"

# -CoreOnly 是一组开关的简写，避免用户记住多个参数。
if ($CoreOnly) {
    $NoLangfuse = $true
    $NoObservability = $true
    $NoAttu = $true
}

Set-Location $Root

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-FileExists {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "缺少必需文件：$Path"
    }
}

function Invoke-DockerCommand {
    param([Parameter(Mandatory = $true)][string[]]$DockerArgs)

    Write-Host "> docker $($DockerArgs -join ' ')" -ForegroundColor DarkCyan
    & docker @DockerArgs

    if ($LASTEXITCODE -ne 0) {
        throw "Docker 命令执行失败：docker $($DockerArgs -join ' ')"
    }
}

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$DefaultValue
    )

    # 进程环境变量优先级最高，随后读取根目录 .env，最后才使用脚本内默认值。
    $processValue = [Environment]::GetEnvironmentVariable($Name, "Process")
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return $processValue
    }

    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($line in Get-Content -LiteralPath $EnvFile) {
            $trimmed = $line.Trim()
            if ($trimmed.StartsWith("#") -or $trimmed.IndexOf("=") -le 0) {
                continue
            }

            $separatorIndex = $trimmed.IndexOf("=")
            if ($trimmed.Substring(0, $separatorIndex).Trim() -ne $Name) {
                continue
            }

            $value = $trimmed.Substring($separatorIndex + 1).Trim()
            if (
                $value.Length -ge 2 -and
                (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                 ($value.StartsWith("'") -and $value.EndsWith("'")))
            ) {
                $value = $value.Substring(1, $value.Length - 2)
            }

            if (-not [string]::IsNullOrWhiteSpace($value)) {
                return $value
            }
        }
    }

    return $DefaultValue
}

function New-SecureHex {
    param([ValidateRange(16, 128)][int]$ByteCount = 32)

    # 使用操作系统的密码学安全随机数生成器，不能用 Get-Random 生成部署密钥。
    $bytes = New-Object byte[] $ByteCount
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }

    return -join ($bytes | ForEach-Object { $_.ToString("x2") })
}

function Set-DotEnvLine {
    param(
        [Parameter(Mandatory = $true)][string]$Content,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $pattern = "(?m)^$([regex]::Escape($Name))=.*$"
    if (-not [regex]::IsMatch($Content, $pattern)) {
        throw "Langfuse 配置模板中缺少变量：$Name"
    }

    return [regex]::Replace($Content, $pattern, "$Name=$Value")
}

function Initialize-LangfuseEnvironment {
    if (Test-Path -LiteralPath $LangfuseEnv) {
        return
    }

    Assert-FileExists -Path $LangfuseEnvExample

    # 首次启动自动生成互相匹配的数据库、缓存和对象存储密码。
    # 生成后的 deploy/langfuse/.env 已被 .gitignore 排除，不会被提交到仓库。
    $nextAuthSecret = New-SecureHex
    $salt = New-SecureHex
    $encryptionKey = New-SecureHex
    $postgresPassword = New-SecureHex
    $clickhousePassword = New-SecureHex
    $redisPassword = New-SecureHex
    $minioPassword = New-SecureHex

    $content = Get-Content -LiteralPath $LangfuseEnvExample -Raw
    $content = Set-DotEnvLine $content "NEXTAUTH_SECRET" $nextAuthSecret
    $content = Set-DotEnvLine $content "SALT" $salt
    $content = Set-DotEnvLine $content "ENCRYPTION_KEY" $encryptionKey
    $content = Set-DotEnvLine $content "POSTGRES_PASSWORD" $postgresPassword
    $content = Set-DotEnvLine $content "DATABASE_URL" "postgresql://postgres:$postgresPassword@postgres:5432/postgres"
    $content = Set-DotEnvLine $content "CLICKHOUSE_PASSWORD" $clickhousePassword
    $content = Set-DotEnvLine $content "REDIS_AUTH" $redisPassword
    $content = Set-DotEnvLine $content "MINIO_ROOT_PASSWORD" $minioPassword
    $content = Set-DotEnvLine $content "LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY" $minioPassword
    $content = Set-DotEnvLine $content "LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY" $minioPassword
    $content = Set-DotEnvLine $content "LANGFUSE_S3_BATCH_EXPORT_SECRET_ACCESS_KEY" $minioPassword

    # 显式写入无 BOM 的 UTF-8，避免旧版 Windows PowerShell 给第一个变量添加隐藏字符。
    [IO.File]::WriteAllText(
        $LangfuseEnv,
        $content,
        (New-Object Text.UTF8Encoding($false))
    )

    Write-Host "已自动生成 Langfuse 本地密钥：$LangfuseEnv" -ForegroundColor Green
    Write-Host "请妥善备份此文件；丢失 ENCRYPTION_KEY 后可能无法读取已有加密数据。" -ForegroundColor Yellow
}

function Test-HttpService {
    param([Parameter(Mandatory = $true)][string]$Url)

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
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][int]$Timeout
    )

    $deadline = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpService -Url $Url) {
            Write-Host "$Name 已就绪：$Url" -ForegroundColor Green
            return
        }

        Write-Host "等待 $Name：$Url"
        Start-Sleep -Seconds 5
    }

    throw "$Name 启动超时。检查地址：$Url"
}

function Get-ContainerStatus {
    param([Parameter(Mandatory = $true)][string]$ContainerName)

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
        [Parameter(Mandatory = $true)][string]$ContainerName,
        [Parameter(Mandatory = $true)][int]$Timeout
    )

    $deadline = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $deadline) {
        $status = (Get-ContainerStatus -ContainerName $ContainerName).Trim()
        Write-Host "等待容器 $ContainerName，当前状态：$status"

        if ($status -eq "healthy") {
            Write-Host "$ContainerName 已就绪" -ForegroundColor Green
            return
        }

        if ($status -in @("unhealthy", "exited", "dead")) {
            & docker logs --tail 200 $ContainerName
            throw "容器启动失败：$ContainerName，状态：$status"
        }

        Start-Sleep -Seconds 5
    }

    & docker logs --tail 200 $ContainerName
    throw "容器启动超时：$ContainerName"
}

function Get-ListeningProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)

    return @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
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
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    $command = Get-Command "mineru-api" -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    return $null
}

function Import-MineruEnvironment {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        return
    }

    # 只导入 MinerU 进程确实需要的变量，避免把根 .env 中的密钥全部暴露给本地进程。
    $supportedKeys = @{
        "MINERU_MODEL_SOURCE" = $true
        "MODELSCOPE_OFFLINE" = $true
        "MODELSCOPE_CACHE" = $true
        "HF_HOME" = $true
        "MINERU_CONFIG_FILE" = $true
    }

    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#")) {
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

        $value = $trimmed.Substring($separatorIndex + 1).Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }

    if ([string]::IsNullOrWhiteSpace($env:MINERU_MODEL_SOURCE)) {
        $env:MINERU_MODEL_SOURCE = "modelscope"
    }
}

function Show-MineruLogs {
    Write-Host "MinerU 最近输出：" -ForegroundColor Yellow
    if (Test-Path -LiteralPath $MineruOutLog) {
        Get-Content -LiteralPath $MineruOutLog -Tail 200
    }
    if (Test-Path -LiteralPath $MineruErrLog) {
        Get-Content -LiteralPath $MineruErrLog -Tail 200
    }
}

function Start-Mineru {
    if (Test-HttpService -Url $MineruHealthUrl) {
        Write-Host "MinerU 已在运行：$MineruHealthUrl" -ForegroundColor Green
        return $true
    }

    $listeningPids = @(Get-ListeningProcessIds -Port $MineruPort)
    if ($listeningPids.Count -gt 0) {
        throw "端口 $MineruPort 已被 PID $($listeningPids -join ', ') 占用，但 MinerU 健康检查失败。"
    }

    $mineruExecutable = Resolve-MineruExecutable
    if ($null -eq $mineruExecutable) {
        Write-Warning "未找到 MinerU。Markdown 仍可导入，但 PDF 解析不可用。安装方法见 README 的“MinerU 独立服务”章节，或使用 -NoMineru 跳过此提示。"
        return $false
    }

    Import-MineruEnvironment
    New-Item -ItemType Directory -Path $MineruLogDir -Force *> $null
    Remove-Item -LiteralPath $MineruOutLog -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $MineruErrLog -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $MineruPidFile -Force -ErrorAction SilentlyContinue

    Write-Host "启动 MinerU：$mineruExecutable"
    $process = Start-Process `
        -FilePath $mineruExecutable `
        -ArgumentList @("--host", "0.0.0.0", "--port", "$MineruPort") `
        -WorkingDirectory $MineruRoot `
        -RedirectStandardOutput $MineruOutLog `
        -RedirectStandardError $MineruErrLog `
        -WindowStyle Hidden `
        -PassThru

    Set-Content -LiteralPath $MineruPidFile -Value $process.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpService -Url $MineruHealthUrl) {
            Write-Host "MinerU 已就绪：$MineruHealthUrl" -ForegroundColor Green
            return $true
        }

        if ($null -eq (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
            Show-MineruLogs
            throw "MinerU 在健康检查通过前已经退出。"
        }

        Write-Host "等待 MinerU：$MineruHealthUrl"
        Start-Sleep -Seconds 5
    }

    Show-MineruLogs
    & taskkill.exe /PID $process.Id /T /F *> $null
    Remove-Item -LiteralPath $MineruPidFile -Force -ErrorAction SilentlyContinue
    throw "MinerU 启动超时：$MineruHealthUrl"
}

function Test-MineruFromImportApi {
    Invoke-DockerCommand -DockerArgs @(
        "compose", "--project-name", "equipment-rag", "-f", $CoreCompose,
        "exec", "-T", "import-api", "python", "-c",
        "import os, requests; base=(os.getenv('MINERU_API_BASE_URL') or '').rstrip('/'); response=requests.get(base + '/health', timeout=10); print('MinerU:', base, response.status_code); response.raise_for_status()"
    )
}

try {
    Write-Host "==============================================" -ForegroundColor Cyan
    Write-Host " Equipment RAG Agent 一键启动" -ForegroundColor Cyan
    Write-Host "==============================================" -ForegroundColor Cyan

    Write-Step "启动前检查"
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "未找到 docker 命令，请先安装并启动 Docker Desktop。"
    }

    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Engine 不可用，请确认 Docker Desktop 已启动。"
    }

    Assert-FileExists -Path $CoreCompose
    Assert-FileExists -Path $EnvExampleFile
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Copy-Item -LiteralPath $EnvExampleFile -Destination $EnvFile
        Write-Warning "已从 .env.example 创建 .env。服务可以启动，但调用模型前必须填写 OPENAI_API_KEY 等模型配置。"
    }

    # 先验证 Compose，尽早发现端口、缩进或变量格式错误，避免启动到一半才失败。
    Invoke-DockerCommand -DockerArgs @(
        "compose", "--project-name", "equipment-rag", "-f", $CoreCompose,
        "--profile", "observability", "config", "--quiet"
    )

    if (-not $NoLangfuse) {
        Assert-FileExists -Path $LangfuseCompose
        Initialize-LangfuseEnvironment
        Invoke-DockerCommand -DockerArgs @(
            "compose", "--project-name", "equipment-rag-langfuse",
            "--env-file", $LangfuseEnv, "-f", $LangfuseCompose,
            "config", "--quiet"
        )

        Write-Step "启动 Langfuse"
        Invoke-DockerCommand -DockerArgs @(
            "compose", "--project-name", "equipment-rag-langfuse",
            "--env-file", $LangfuseEnv, "-f", $LangfuseCompose,
            "up", "-d"
        )
        Wait-HttpService -Name "Langfuse" -Url "http://127.0.0.1:3000/api/public/health" -Timeout $TimeoutSeconds
    }

    Write-Step "启动 MongoDB、MinIO 和 etcd"
    Invoke-DockerCommand -DockerArgs @(
        "compose", "--project-name", "equipment-rag", "-f", $CoreCompose,
        "up", "-d", "mongo", "minio", "etcd"
    )
    Wait-ContainerHealthy -ContainerName "equipment-rag-mongo-1" -Timeout $TimeoutSeconds
    Wait-ContainerHealthy -ContainerName "equipment-rag-minio-1" -Timeout $TimeoutSeconds
    Wait-ContainerHealthy -ContainerName "equipment-rag-etcd-1" -Timeout $TimeoutSeconds

    Write-Step "启动 Milvus"
    Invoke-DockerCommand -DockerArgs @(
        "compose", "--project-name", "equipment-rag", "-f", $CoreCompose,
        "up", "-d", "milvus"
    )
    Wait-ContainerHealthy -ContainerName "equipment-rag-milvus-1" -Timeout $TimeoutSeconds

    $mineruStarted = $false
    if (-not $NoMineru) {
        Write-Step "启动 MinerU（未安装时自动跳过，仅影响 PDF）"
        $mineruStarted = Start-Mineru
    }

    Write-Step "构建并启动导入 API 与查询 API"
    $apiArgs = @(
        "compose", "--project-name", "equipment-rag", "-f", $CoreCompose,
        "up", "-d"
    )
    if (-not $SkipBuild) {
        $apiArgs += "--build"
    }
    $apiArgs += @("import-api", "query-api")
    Invoke-DockerCommand -DockerArgs $apiArgs

    $importPort = Get-DotEnvValue -Name "IMPORT_API_PORT" -DefaultValue "8000"
    $queryPort = Get-DotEnvValue -Name "QUERY_API_PORT" -DefaultValue "8001"
    Wait-HttpService -Name "导入 API" -Url "http://127.0.0.1:$importPort/health" -Timeout $TimeoutSeconds
    Wait-HttpService -Name "查询 API" -Url "http://127.0.0.1:$queryPort/health" -Timeout $TimeoutSeconds
    if ($mineruStarted) {
        Test-MineruFromImportApi
    }

    if (-not $NoObservability) {
        Write-Step "启动 Prometheus 与 Grafana"
        Invoke-DockerCommand -DockerArgs @(
            "compose", "--project-name", "equipment-rag", "-f", $CoreCompose,
            "--profile", "observability", "up", "-d", "prometheus", "grafana"
        )

        $prometheusPort = Get-DotEnvValue -Name "PROMETHEUS_PORT" -DefaultValue "9090"
        $grafanaPort = Get-DotEnvValue -Name "GRAFANA_PORT" -DefaultValue "3001"
        Wait-HttpService -Name "Prometheus" -Url "http://127.0.0.1:$prometheusPort/-/ready" -Timeout $TimeoutSeconds
        Wait-HttpService -Name "Grafana" -Url "http://127.0.0.1:$grafanaPort/api/health" -Timeout $TimeoutSeconds
    }

    if (-not $NoAttu) {
        Assert-FileExists -Path $AttuCompose
        Write-Step "启动 Attu（Milvus 管理页面）"
        Invoke-DockerCommand -DockerArgs @(
            "compose", "--project-name", "equipment-rag-attu",
            "--env-file", $EnvFile, "-f", $AttuCompose,
            "up", "-d"
        )

        $attuPort = Get-DotEnvValue -Name "ATTU_PORT" -DefaultValue "3002"
        Wait-HttpService -Name "Attu" -Url "http://127.0.0.1:$attuPort" -Timeout $TimeoutSeconds
    }

    Write-Host ""
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host " 所有已选服务启动成功" -ForegroundColor Green
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host "导入页面：          http://127.0.0.1:$importPort/import.html"
    Write-Host "聊天页面：          http://127.0.0.1:$queryPort/chat.html"
    Write-Host "导入 API 文档：     http://127.0.0.1:$importPort/docs"
    Write-Host "查询 API 文档：     http://127.0.0.1:$queryPort/docs"
    if ($mineruStarted) { Write-Host "MinerU API：        http://127.0.0.1:$MineruPort/docs" }
    if (-not $NoLangfuse) { Write-Host "Langfuse：          http://127.0.0.1:3000" }
    if (-not $NoObservability) {
        Write-Host "Prometheus：        http://127.0.0.1:$prometheusPort"
        Write-Host "Grafana：           http://127.0.0.1:$grafanaPort"
    }
    if (-not $NoAttu) { Write-Host "Attu：              http://127.0.0.1:$attuPort" }
    Write-Host ""
    Write-Host "查看状态：docker compose --profile observability ps"
    Write-Host "暂停全部：.\stop-all.ps1"
    if (-not $NoLangfuse) {
        Write-Host "提示：首次进入 Langfuse 后创建项目/API Key，再按 docs/observability.md 开启 Trace。" -ForegroundColor Yellow
    }
}
catch {
    Write-Host ""
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host "核心日志：docker compose logs --tail 200" -ForegroundColor Yellow
    Write-Host "服务状态：docker compose --profile observability ps" -ForegroundColor Yellow
    Write-Host "安全暂停：.\stop-all.ps1" -ForegroundColor Yellow
    exit 1
}
