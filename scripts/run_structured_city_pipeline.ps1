param(
    [string]$RepoRoot = "V:\CupidAi\hyderabad-city-knowledge-collector",
    [string]$DataRoot = "data",
    [string]$DatabaseUrl = "postgresql://admin:123@localhost:5432/cupidaidb",
    [int]$StructuredBulkLimit = 5000,
    [int]$StructuredWikimediaMaxEntities = 1200,
    [string]$StructuredCategories = "",
    [string]$LogLevel = "INFO",
    [string]$MobileIndexOutput = "mobile_cards.json",
    [switch]$SkipPostgresExport,
    [switch]$SkipMobileIndex
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-Collector {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    Write-Host "collector.cli $($Arguments -join ' ')" -ForegroundColor DarkGray
    & python -m collector.cli --data-root $DataRoot --log-level $LogLevel @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "collector.cli failed: $($Arguments -join ' ')"
    }
}

Push-Location $RepoRoot
try {
    Write-Step "Structured crawl: OpenStreetMap bulk + Wikidata/Wikipedia/Commons enrichment"
    if ([string]::IsNullOrWhiteSpace($StructuredCategories)) {
        Invoke-Collector structured-bulk --limit $StructuredBulkLimit --max-wikimedia $StructuredWikimediaMaxEntities
    } else {
        Invoke-Collector structured-bulk --limit $StructuredBulkLimit --max-wikimedia $StructuredWikimediaMaxEntities --categories $StructuredCategories
    }

    Write-Step "Refine: canonical names, intent tags, cards, quality scores"
    Invoke-Collector refine --skip-open-image

    Write-Step "Validate production readiness"
    Invoke-Collector validate-production

    if (-not $SkipPostgresExport) {
        Write-Step "Export: production-ready entities to local Postgres"
        Invoke-Collector postgres-export --database-url $DatabaseUrl
    }

    if (-not $SkipMobileIndex) {
        Write-Step "Mobile index: card-ready sample for app search"
        Invoke-Collector mobile-index --query "quiet places to walk near Begumpet" --limit 100 --output $MobileIndexOutput
    }

    Write-Step "Coverage report"
    Invoke-Collector coverage
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Done. Structured city pipeline completed." -ForegroundColor Green
Write-Host "Filesystem data: $RepoRoot\$DataRoot\city" -ForegroundColor Green
if (-not $SkipPostgresExport) {
    Write-Host "Postgres target: $DatabaseUrl" -ForegroundColor Green
}
