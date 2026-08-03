[CmdletBinding()]
param(
    # 必须与启动脚本使用的 MinerU 端口一致，默认无需修改。
    [ValidateRange(1, 65535)]
    [int]$MineruPort = 8002,

    # 默认只暂停容器，所以下次启动更快。
    # 传入此开关会删除容器和网络，但仍然保留所有命名卷与业务数据。
    [switch]$RemoveContainers
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$CoreCompose = Join-Path $Root "compose.yaml"
$LangfuseCompose = Join-Path $Root "deploy\langfuse\docker-compose.yml"
$LangfuseEnv = Join-Path $Root "deploy\langfuse\.env"
$AttuCompose = Join-Path $Root "deploy\attu\compose.yaml"
$EnvFile = Join-Path $Root ".env"
$MineruRoot = Join-Path $Root "deploy\mineru-runtime"
$MineruPidFile = Join-Path $MineruRoot ".mineru.pid"

# 停止某个组件失败时继续处理剩余组件，最后再统一返回失败，避免半数服务遗留运行。
$script:Failures = @()
Set-Location $Root

function Invoke-Safely {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Action
    )

    try {
        & $Action
    }
    catch {
        $message = "$Name：$($_.Exception.Message)"
        $script:Failures += $message
        Write-Warning $message
    }
}

function Stop-ComposeProject {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectName,
        [Parameter(Mandatory = $true)][string]$ComposeFile,
        [string]$EnvironmentFile,
        [switch]$EnableObservabilityProfile
    )

    if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) {
        Write-Warning "Compose 文件不存在，已跳过：$ComposeFile"
        return
    }

    $arguments = @("compose", "--project-name", $ProjectName)
    if (-not [string]::IsNullOrWhiteSpace($EnvironmentFile) -and (Test-Path -LiteralPath $EnvironmentFile)) {
        $arguments += @("--env-file", $EnvironmentFile)
    }
    $arguments += @("-f", $ComposeFile)
    if ($EnableObservabilityProfile) {
        $arguments += @("--profile", "observability")
    }

    if ($RemoveContainers) {
        # 不添加 --volumes：MongoDB、Milvus、对象、Trace 和仪表盘数据都会保留。
        $arguments += @("down", "--remove-orphans")
    }
    else {
        $arguments += "stop"
    }

    Write-Host ""
    Write-Host "暂停项目：$ProjectName" -ForegroundColor Cyan
    Write-Host "> docker $($arguments -join ' ')" -ForegroundColor DarkCyan
    & docker @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker 命令返回退出码 $LASTEXITCODE"
    }
}

function Get-ListeningProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)

    return @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { $_.OwningProcess } |
            Sort-Object -Unique
    )
}

function Get-MineruProcessCandidates {
    return @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $commandLine = [string]$_.CommandLine
                $executablePath = [string]$_.ExecutablePath
                ($commandLine -match "mineru-api" -and $commandLine -match "--port\s+$MineruPort") -or
                (
                    -not [string]::IsNullOrWhiteSpace($executablePath) -and
                    $executablePath.StartsWith($MineruRoot, [StringComparison]::OrdinalIgnoreCase) -and
                    $commandLine -match "--port\s+$MineruPort"
                )
            }
    )
}

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        return
    }

    Write-Host "停止 MinerU 进程树，PID：$ProcessId"
    & taskkill.exe /PID $ProcessId /T /F *> $null
    if (
        $LASTEXITCODE -ne 0 -and
        $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
    ) {
        throw "无法停止 PID $ProcessId"
    }
}

function Stop-Mineru {
    Write-Host ""
    Write-Host "暂停 MinerU" -ForegroundColor Cyan
    $stoppedPids = @{}

    # PID 文件只作为线索；停止前仍会检查命令行，防止 PID 被系统复用后误杀其他程序。
    if (Test-Path -LiteralPath $MineruPidFile) {
        try {
            $pidText = (Get-Content -LiteralPath $MineruPidFile | Select-Object -First 1).Trim()
            $savedPid = 0
            if ([int]::TryParse($pidText, [ref]$savedPid)) {
                $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
                if ($null -ne $processInfo) {
                    $commandLine = [string]$processInfo.CommandLine
                    $executablePath = [string]$processInfo.ExecutablePath
                    $isMineru = $commandLine -match "mineru-api" -or (
                        -not [string]::IsNullOrWhiteSpace($executablePath) -and
                        $executablePath.StartsWith($MineruRoot, [StringComparison]::OrdinalIgnoreCase)
                    )

                    if ($isMineru) {
                        Stop-ProcessTree -ProcessId $savedPid
                        $stoppedPids[$savedPid] = $true
                    }
                    else {
                        Write-Warning "PID 文件对应的不是 MinerU，已拒绝停止该进程：$savedPid"
                    }
                }
            }
        }
        finally {
            Remove-Item -LiteralPath $MineruPidFile -Force -ErrorAction SilentlyContinue
        }
    }

    # 处理 PID 文件丢失或旧脚本启动的 MinerU，但仍严格限定命令和端口。
    foreach ($candidate in @(Get-MineruProcessCandidates)) {
        $candidatePid = [int]$candidate.ProcessId
        if (-not $stoppedPids.ContainsKey($candidatePid)) {
            Stop-ProcessTree -ProcessId $candidatePid
            $stoppedPids[$candidatePid] = $true
        }
    }

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-ListeningProcessIds -Port $MineruPort).Count -eq 0) {
            Write-Host "MinerU 已暂停。" -ForegroundColor Green
            return
        }
        Start-Sleep -Seconds 1
    }

    $remainingPids = @(Get-ListeningProcessIds -Port $MineruPort)
    if ($remainingPids.Count -gt 0) {
        throw "端口 $MineruPort 仍被 PID $($remainingPids -join ', ') 占用。"
    }
}

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " Equipment RAG Agent 一键暂停" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan

$dockerAvailable = $null -ne (Get-Command docker -ErrorAction SilentlyContinue)
if ($dockerAvailable) {
    & docker info *> $null
    $dockerAvailable = $LASTEXITCODE -eq 0
}

if ($dockerAvailable) {
    # 先停外围管理工具，再停核心依赖；启动时按相反顺序恢复。
    Invoke-Safely "Attu" {
        Stop-ComposeProject -ProjectName "equipment-rag-attu" -ComposeFile $AttuCompose -EnvironmentFile $EnvFile
    }
    Invoke-Safely "核心服务与仪表盘" {
        Stop-ComposeProject -ProjectName "equipment-rag" -ComposeFile $CoreCompose -EnvironmentFile $EnvFile -EnableObservabilityProfile
    }
}
else {
    Write-Warning "Docker Engine 不可用，Docker 容器无法暂停；仍会继续尝试停止本机 MinerU。"
    $script:Failures += "Docker Engine 不可用"
}

Invoke-Safely "MinerU" { Stop-Mineru }

if ($dockerAvailable) {
    Invoke-Safely "Langfuse" {
        Stop-ComposeProject -ProjectName "equipment-rag-langfuse" -ComposeFile $LangfuseCompose -EnvironmentFile $LangfuseEnv
    }
}

Write-Host ""
if ($script:Failures.Count -gt 0) {
    Write-Host "部分组件未能正常暂停：" -ForegroundColor Red
    $script:Failures | ForEach-Object { Write-Host "- $_" -ForegroundColor Red }
    exit 1
}

Write-Host "所有服务均已暂停，MongoDB、Milvus、MinIO、Langfuse 和 Grafana 数据已保留。" -ForegroundColor Green
if (-not $RemoveContainers) {
    Write-Host "下次直接运行 .\start-all.ps1 即可恢复。"
}
