param(
    [string]$Query = "places to visit after midnight in hyderabad",
    [int]$MaxCandidates = 20,
    [string]$FirecrawlDir = "V:\CupidAi\firecrawl",
    [string]$FirecrawlUrl = "http://localhost:3002",
    [switch]$Build,
    [switch]$StopAfterRun,
    [int]$StartupTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-FirecrawlApi {
    param([string]$BaseUrl)

    try {
        $body = @{ query = "Hyderabad"; limit = 1 } | ConvertTo-Json -Compress
        $null = Invoke-RestMethod "$BaseUrl/v2/search" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 20
        return $true
    } catch {
        return $false
    }
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")

if (-not (Test-Path $FirecrawlDir)) {
    throw "Firecrawl folder not found at $FirecrawlDir. Run .\scripts\setup_firecrawl_local.ps1 first."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required. Start Docker Desktop and retry."
}

try {
    docker info *> $null
} catch {
    throw "Docker Desktop is not running. Start Docker Desktop and retry."
}

Write-Step "Starting local Firecrawl"
Push-Location $FirecrawlDir
try {
    if ($Build) {
        docker compose up -d --build api
    } else {
        docker compose up -d api
    }
} finally {
    Pop-Location
}

Write-Step "Waiting for Firecrawl API at $FirecrawlUrl"
$deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    if (Test-FirecrawlApi -BaseUrl $FirecrawlUrl) {
        Write-Host "Firecrawl is ready." -ForegroundColor Green
        break
    }
    Start-Sleep -Seconds 3
}

if (-not (Test-FirecrawlApi -BaseUrl $FirecrawlUrl)) {
    Push-Location $FirecrawlDir
    try {
        docker compose ps
        docker compose logs --tail=80 api
    } finally {
        Pop-Location
    }
    throw "Firecrawl did not become ready within $StartupTimeoutSeconds seconds."
}

Write-Step "Seeding query"
Push-Location $RepoRoot
try {
    python -m collector.cli seed firecrawl_search $Query

    Write-Step "Running crawler batch"
    python -m collector.cli run --max-candidates $MaxCandidates

    Write-Step "Coverage"
    python -m collector.cli coverage
} finally {
    Pop-Location
}

if ($StopAfterRun) {
    Write-Step "Stopping Firecrawl"
    Push-Location $FirecrawlDir
    try {
        docker compose down
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "Done. Data is under $RepoRoot\data\city\entities" -ForegroundColor Green
