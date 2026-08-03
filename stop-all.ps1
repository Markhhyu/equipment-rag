param(
    [int]$MineruPort = 8002
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$MineruRoot = Join-Path $Root "deploy\mineru-runtime"
$MineruPidFile = Join-Path $MineruRoot ".mineru.pid"

Set-Location $Root

function Stop-Compose {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectName,

        [Parameter(Mandatory = $true)]
        [string]$ComposeFile
    )

    if (-not (Test-Path $ComposeFile)) {
        Write-Warning "Compose file not found: $ComposeFile"
        return
    }

    Write-Host ""
    Write-Host "Stopping project: $ProjectName" -ForegroundColor Cyan

    & docker compose `
        --project-name $ProjectName `
        -f $ComposeFile `
        down `
        --remove-orphans

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stop project: $ProjectName"
    }
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

function Get-MineruProcessCandidates {
    $processes = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $commandLine = [string]$_.CommandLine
                $executablePath = [string]$_.ExecutablePath

                (
                    $commandLine -match "mineru-api" -and
                    $commandLine -match "--port\s+$MineruPort"
                ) -or
                (
                    -not [string]::IsNullOrWhiteSpace($executablePath) -and
                    $executablePath.StartsWith(
                        $MineruRoot,
                        [System.StringComparison]::OrdinalIgnoreCase
                    ) -and
                    $commandLine -match "--port\s+$MineruPort"
                )
            }
    )

    return $processes
}

function Stop-ProcessTree {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $process = Get-Process `
        -Id $ProcessId `
        -ErrorAction SilentlyContinue

    if ($null -eq $process) {
        return
    }

    Write-Host "Stopping process tree, PID: $ProcessId" -ForegroundColor Cyan

    & taskkill.exe /PID $ProcessId /T /F *> $null

    if (
        $LASTEXITCODE -ne 0 -and
        $null -ne (
            Get-Process `
                -Id $ProcessId `
                -ErrorAction SilentlyContinue
        )
    ) {
        throw "Failed to stop process tree, PID: $ProcessId"
    }
}

function Stop-Mineru {
    Write-Host ""
    Write-Host "Stopping MinerU..." -ForegroundColor Cyan

    $stoppedPids = @{}

    if (Test-Path $MineruPidFile) {
        try {
            $pidText = (
                Get-Content $MineruPidFile |
                    Select-Object -First 1
            ).Trim()

            $savedPid = 0

            if ([int]::TryParse($pidText, [ref]$savedPid)) {
                $processInfo = Get-CimInstance `
                    Win32_Process `
                    -Filter "ProcessId = $savedPid" `
                    -ErrorAction SilentlyContinue

                if ($null -ne $processInfo) {
                    $commandLine = [string]$processInfo.CommandLine
                    $executablePath = [string]$processInfo.ExecutablePath

                    $isMineruProcess =
                        $commandLine -match "mineru-api" -or
                        (
                            -not [string]::IsNullOrWhiteSpace($executablePath) -and
                            $executablePath.StartsWith(
                                $MineruRoot,
                                [System.StringComparison]::OrdinalIgnoreCase
                            )
                        )

                    if ($isMineruProcess) {
                        Stop-ProcessTree -ProcessId $savedPid
                        $stoppedPids[$savedPid] = $true
                    }
                    else {
                        Write-Warning "PID file points to a non-MinerU process. PID: $savedPid"
                    }
                }
            }
        }
        catch {
            Write-Warning "Failed to process MinerU PID file: $($_.Exception.Message)"
        }
        finally {
            Remove-Item $MineruPidFile -Force -ErrorAction SilentlyContinue
        }
    }

    $candidates = @(Get-MineruProcessCandidates)

    foreach ($candidate in $candidates) {
        $candidatePid = [int]$candidate.ProcessId

        if (-not $stoppedPids.ContainsKey($candidatePid)) {
            Stop-ProcessTree -ProcessId $candidatePid
            $stoppedPids[$candidatePid] = $true
        }
    }

    $deadline = (Get-Date).AddSeconds(20)

    while ((Get-Date) -lt $deadline) {
        $listeningPids = @(Get-ListeningProcessIds -Port $MineruPort)

        if ($listeningPids.Count -eq 0) {
            Write-Host "MinerU has been stopped." -ForegroundColor Green
            return
        }

        Start-Sleep -Seconds 1
    }

    $remainingPids = @(Get-ListeningProcessIds -Port $MineruPort)

    if ($remainingPids.Count -gt 0) {
        throw "Port $MineruPort is still occupied by PID(s): $($remainingPids -join ', ')"
    }

    Write-Host "MinerU has been stopped." -ForegroundColor Green
}

Stop-Compose `
    -ProjectName "equipment-rag-attu" `
    -ComposeFile "$Root\deploy\attu\compose.yaml"

Stop-Compose `
    -ProjectName "equipment-rag" `
    -ComposeFile "$Root\compose.yaml"

Stop-Mineru

Stop-Compose `
    -ProjectName "equipment-rag-langfuse" `
    -ComposeFile "$Root\deploy\langfuse\docker-compose.yml"

Write-Host ""
Write-Host "All services have been stopped. Docker volumes were preserved." -ForegroundColor Green