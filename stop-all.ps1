$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

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

    Write-Host "`nStopping project: $ProjectName" -ForegroundColor Cyan

    & docker compose `
        --project-name $ProjectName `
        -f $ComposeFile `
        down `
        --remove-orphans

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stop project: $ProjectName"
    }
}

Stop-Compose `
    -ProjectName "equipment-rag-attu" `
    -ComposeFile "$Root\deploy\attu\compose.yaml"

Stop-Compose `
    -ProjectName "equipment-rag" `
    -ComposeFile "$Root\compose.yaml"

Stop-Compose `
    -ProjectName "equipment-rag-langfuse" `
    -ComposeFile "$Root\deploy\langfuse\docker-compose.yml"

Write-Host "`nAll services have been stopped. Docker volumes were preserved." -ForegroundColor Green