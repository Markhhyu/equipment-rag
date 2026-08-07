[CmdletBinding()]
param(
    # 默认会重新构建两个应用镜像。代码没有变化时传入 -SkipBuild 可以明显加快启动。
    [Alias("NoBuild")]
    [switch]$SkipBuild,

    # 只启动业务必需组件：数据库、向量库、三个 API 和 MinerU。
    # Langfuse、Prometheus/Grafana/Loki/Alloy、Attu 都不会启动，适合低内存机器临时开发。
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
        # 部分安全校验需要用空字符串表示“没有默认值”。AllowEmptyString让PowerShell
        # 接受这种调用，否则参数绑定会在真正的配置检查前提前失败。
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$DefaultValue
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

function Get-DotEnvFileValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$DefaultValue
    )

    # 这个函数专门读取指定的 .env 文件。Langfuse 使用独立的
    # deploy/langfuse/.env，不能复用只读取仓库根目录 .env 的 Get-DotEnvValue。
    # 解析时仅按第一个等号分隔，因此 URL 或密码后半段即使含有等号也不会被截断。
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
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

            return $value
        }
    }

    return $DefaultValue
}

function Assert-LlmConfiguration {
    <#
    在启动容器前检查问答必需的模型配置。

    /health只能证明FastAPI进程已经启动，不能证明模型密钥有效。旧逻辑允许空密钥继续启动，
    最终页面显示“API已连接”，但用户第一次提问才看到配置错误。这里提前阻止这种假健康状态。
    出于安全考虑，错误信息只显示缺失的变量名，绝不打印密钥内容。
    #>
    $apiKey = Get-DotEnvValue -Name "OPENAI_API_KEY" -DefaultValue ""
    $baseUrl = Get-DotEnvValue -Name "OPENAI_BASE_URL" -DefaultValue ""
    $modelName = Get-DotEnvValue -Name "LLM_DEFAULT_MODEL" -DefaultValue ""

    $missingNames = @()
    if ([string]::IsNullOrWhiteSpace($apiKey)) { $missingNames += "OPENAI_API_KEY" }
    if ([string]::IsNullOrWhiteSpace($baseUrl)) { $missingNames += "OPENAI_BASE_URL" }
    if ([string]::IsNullOrWhiteSpace($modelName)) { $missingNames += "LLM_DEFAULT_MODEL" }

    if ($missingNames.Count -gt 0) {
        throw (
            "模型配置未完成，缺少：$($missingNames -join '、')。" +
            "请编辑 $EnvFile；密钥只保存在本机，不要粘贴到聊天、日志或Git仓库。填写后重新运行 start-all.cmd。"
        )
    }
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

function Sync-LangfusePostgresPassword {
    <#
    PostgreSQL 只在“第一次创建数据卷”时读取 POSTGRES_PASSWORD。以后即使用户重新生成
    deploy/langfuse/.env，已有数据卷里的数据库密码也不会自动变化。此时 PostgreSQL 健康，
    但 Langfuse Web 会持续报 Prisma P1000，并在 Restarting 状态中反复退出。

    这里通过 PostgreSQL 容器内部的本地 Unix Socket 连接执行 ALTER ROLE，把已有数据库
    角色的密码同步为当前 .env 中的值。该操作不会删除数据库、Trace、用户或项目数据。
    SQL 通过标准输入传入，不把密码放进 docker 命令参数，也不会在控制台打印密码。
    #>
    $postgresUser = Get-DotEnvFileValue -Path $LangfuseEnv -Name "POSTGRES_USER" -DefaultValue "postgres"
    $postgresPassword = Get-DotEnvFileValue -Path $LangfuseEnv -Name "POSTGRES_PASSWORD" -DefaultValue ""
    $postgresDatabase = Get-DotEnvFileValue -Path $LangfuseEnv -Name "POSTGRES_DB" -DefaultValue "postgres"
    $databaseUrl = Get-DotEnvFileValue -Path $LangfuseEnv -Name "DATABASE_URL" -DefaultValue ""

    if ([string]::IsNullOrWhiteSpace($postgresPassword)) {
        throw "Langfuse 配置缺少 POSTGRES_PASSWORD：$LangfuseEnv"
    }

    # 一键脚本自动生成的是十六进制密码。限制可接受字符既能避免 URL 编码歧义，
    # 也能阻止手工编辑 .env 时把引号或 SQL 片段误带入后面的 ALTER ROLE 语句。
    if ($postgresPassword -notmatch '^[0-9a-fA-F]{32,256}$') {
        throw "Langfuse 的 POSTGRES_PASSWORD 必须是 32 到 256 位十六进制字符；请按 .env.example 的说明重新生成。"
    }

    # 当前本地部署固定使用 postgres 用户和 postgres 数据库。保持这两个值固定，
    # 可以确保旧数据卷、新 .env 与容器内本地管理员角色始终能够安全对齐。
    if ($postgresUser -ne "postgres" -or $postgresDatabase -ne "postgres") {
        throw "本地 Langfuse 一键部署要求 POSTGRES_USER 和 POSTGRES_DB 都为 postgres。"
    }

    $expectedDatabaseUrl = "postgresql://postgres:$postgresPassword@postgres:5432/postgres"
    if ($databaseUrl -ne $expectedDatabaseUrl) {
        throw "Langfuse 的 DATABASE_URL 与 POSTGRES_PASSWORD 不一致；请让两项使用同一个密码。"
    }

    Write-Host "同步 Langfuse PostgreSQL 凭据（不会删除已有数据）..."
    $sql = "ALTER ROLE postgres WITH PASSWORD '$postgresPassword';"
    $sql | & docker compose `
        --project-name equipment-rag-langfuse `
        --env-file $LangfuseEnv `
        -f $LangfuseCompose `
        exec -T postgres psql --username postgres --dbname postgres `
        2>&1 | ForEach-Object {
            # psql 成功时只会输出 ALTER ROLE。主动过滤后可以避免未来客户端版本
            # 在诊断信息中意外回显 SQL；失败详情仍由下方的统一异常提示处理。
            if ($_ -notmatch '^ALTER ROLE\s*$') { Write-Host $_ }
        }

    if ($LASTEXITCODE -ne 0) {
        throw "无法同步 Langfuse PostgreSQL 凭据，请检查 postgres 容器日志。"
    }

    Write-Host "Langfuse PostgreSQL 凭据已同步" -ForegroundColor Green
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
        [Parameter(Mandatory = $true)][int]$Timeout,
        # 可选的容器名用于“快速失败”。例如进程已进入 Restarting 时，继续等待 HTTP
        # 没有意义；脚本会立即输出该容器最近日志，让用户直接看到真正根因。
        [AllowEmptyString()][string]$ContainerName = ""
    )

    $deadline = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpService -Url $Url) {
            Write-Host "$Name 已就绪：$Url" -ForegroundColor Green
            return
        }

        if (-not [string]::IsNullOrWhiteSpace($ContainerName)) {
            $containerStatus = (Get-ContainerStatus -ContainerName $ContainerName).Trim()
            if ($containerStatus -in @("restarting", "unhealthy", "exited", "dead")) {
                Write-Host "$Name 容器异常，最近日志如下：" -ForegroundColor Red
                & docker logs --tail 200 $ContainerName
                throw "$Name 容器启动失败：$ContainerName，状态：$containerStatus"
            }
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
        Write-Warning "已从 .env.example 创建 .env。请先填写模型配置；脚本不会在密钥为空时继续启动。"
    }

    Assert-LlmConfiguration

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
        Wait-ContainerHealthy -ContainerName "equipment-rag-langfuse-postgres-1" -Timeout $TimeoutSeconds
        Sync-LangfusePostgresPassword

        # Web/Worker 可能已经用旧密码尝试连接并进入重试退避。凭据同步后主动重启二者，
        # 让它们立即重新迁移和连接数据库，不必等待 Docker 的下一次自动重启周期。
        Invoke-DockerCommand -DockerArgs @(
            "compose", "--project-name", "equipment-rag-langfuse",
            "--env-file", $LangfuseEnv, "-f", $LangfuseCompose,
            "restart", "langfuse-web", "langfuse-worker"
        )
        Wait-HttpService `
            -Name "Langfuse" `
            -Url "http://127.0.0.1:3000/api/public/health" `
            -Timeout $TimeoutSeconds `
            -ContainerName "equipment-rag-langfuse-langfuse-web-1"
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

    Write-Step "构建并启动统一前端、导入 API、查询 API 与工作流 API"
    $apiArgs = @(
        "compose", "--project-name", "equipment-rag", "-f", $CoreCompose,
        "up", "-d"
    )
    if (-not $SkipBuild) {
        $apiArgs += "--build"
    }
    $apiArgs += @("import-api", "query-api", "workflow-api", "web")
    Invoke-DockerCommand -DockerArgs $apiArgs

    $importPort = Get-DotEnvValue -Name "IMPORT_API_PORT" -DefaultValue "8000"
    $queryPort = Get-DotEnvValue -Name "QUERY_API_PORT" -DefaultValue "8001"
    $workflowPort = Get-DotEnvValue -Name "WORKFLOW_API_PORT" -DefaultValue "8002"
    $webPort = Get-DotEnvValue -Name "WEB_PORT" -DefaultValue "8080"
    Wait-HttpService -Name "导入 API" -Url "http://127.0.0.1:$importPort/health" -Timeout $TimeoutSeconds
    Wait-HttpService -Name "查询 API" -Url "http://127.0.0.1:$queryPort/health" -Timeout $TimeoutSeconds
    Wait-HttpService -Name "工作流 API" -Url "http://127.0.0.1:$workflowPort/health" -Timeout $TimeoutSeconds
    Wait-HttpService -Name "统一前端" -Url "http://127.0.0.1:$webPort/healthz" -Timeout $TimeoutSeconds
    if ($mineruStarted) {
        Test-MineruFromImportApi
    }

    if (-not $NoObservability) {
        Write-Step "启动 Prometheus、Loki、Alloy 与 Grafana"
        Invoke-DockerCommand -DockerArgs @(
            "compose", "--project-name", "equipment-rag", "-f", $CoreCompose,
            "--profile", "observability", "up", "-d", "prometheus", "loki", "alloy", "grafana"
        )

        $prometheusPort = Get-DotEnvValue -Name "PROMETHEUS_PORT" -DefaultValue "9090"
        $lokiPort = Get-DotEnvValue -Name "LOKI_PORT" -DefaultValue "3100"
        $alloyPort = Get-DotEnvValue -Name "ALLOY_PORT" -DefaultValue "12345"
        $grafanaPort = Get-DotEnvValue -Name "GRAFANA_PORT" -DefaultValue "3001"
        Wait-HttpService -Name "Prometheus" -Url "http://127.0.0.1:$prometheusPort/-/ready" -Timeout $TimeoutSeconds
        Wait-HttpService -Name "Loki" -Url "http://127.0.0.1:$lokiPort/ready" -Timeout $TimeoutSeconds
        Wait-HttpService -Name "Alloy" -Url "http://127.0.0.1:$alloyPort/-/ready" -Timeout $TimeoutSeconds
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
    Write-Host "知识库治理：        http://127.0.0.1:$importPort/knowledge.html"
    Write-Host "聊天页面：          http://127.0.0.1:$queryPort/chat.html"
    Write-Host "导入 API 文档：     http://127.0.0.1:$importPort/docs"
    Write-Host "查询 API 文档：     http://127.0.0.1:$queryPort/docs"
    Write-Host "工作流 API 文档：   http://127.0.0.1:$workflowPort/docs"
    if ($mineruStarted) { Write-Host "MinerU API：        http://127.0.0.1:$MineruPort/docs" }
    if (-not $NoLangfuse) { Write-Host "Langfuse：          http://127.0.0.1:3000" }
    if (-not $NoObservability) {
        Write-Host "Prometheus：        http://127.0.0.1:$prometheusPort"
        Write-Host "Loki：              http://127.0.0.1:$lokiPort/ready"
        Write-Host "Alloy：             http://127.0.0.1:$alloyPort"
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
