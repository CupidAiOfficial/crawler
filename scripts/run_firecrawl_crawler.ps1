param(
    [string]$Query = "places to visit after midnight in hyderabad",
    [string]$QueryListPath = "",
    [string]$ProgressPath = "",
    [int]$MaxQueries = 0,
    [int]$MaxCandidates = 20,
    [int]$Workers = 4,
    [int]$ProductionEnrichmentPasses = 2,
    [int]$ProductionEnrichmentMaxEntities = 200,
    [int]$ProductionWebEnrichMaxEntities = 80,
    [int]$StructuredBulkLimit = 5000,
    [int]$StructuredWikimediaMaxEntities = 1200,
    [string]$StructuredCategories = "",
    [string]$FirecrawlDir = "V:\CupidAi\firecrawl",
    [string]$FirecrawlUrl = "http://localhost:3002",
    [string]$DataRoot = "data",
    [string]$DatabaseUrl = "postgresql://admin:123@localhost:5432/cupidaidb",
    [string]$LogLevel = "INFO",
    [string]$MobileIndexOutput = "mobile_cards.json",
    [switch]$Build,
    [switch]$StopAfterRun,
    [switch]$SkipPostgresExport,
    [switch]$SkipProductionWebEnrich,
    [switch]$SkipStructuredBulk,
    [switch]$EnableRefineOpenImage,
    [switch]$ResetProgress,
    [switch]$ContinueOnError,
    [switch]$EnableStickyStatus,
    [switch]$DisableStickyStatus,
    [int]$StartupTimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$script:TrackerStage = "starting"
$script:TrackerIndex = 0
$script:TrackerTotal = 0
$script:TrackerQuery = ""
$script:TrackerDataRoot = "data"
$script:TrackerDatabaseUrl = ""
$script:TrackerLastRender = [DateTime]::MinValue
$script:TrackerCachedDb = $null
$script:TrackerLines = 4

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
    Update-GlobalTracker -Stage $Message -Force
}

function Invoke-Collector {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $display = ($Arguments -join " ")
    Write-Host "collector.cli $display" -ForegroundColor DarkGray
    Update-GlobalTracker -Force
    & python -m collector.cli --data-root $DataRoot --log-level $LogLevel @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "collector.cli failed: $display"
    }
    Update-GlobalTracker -Force
}

function Invoke-RefineStep {
    param([switch]$ProductionWebEnrich)
    $args = @("refine")
    if (-not $EnableRefineOpenImage) {
        $args += "--skip-open-image"
    }
    if ($ProductionWebEnrich) {
        $args += @("--production-web-enrich", "--max-web-enrich", "$ProductionWebEnrichMaxEntities")
    }
    Invoke-Collector @args
}

function Quote-ProcessArgument {
    param([string]$Value)
    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    $escaped = $Value -replace '\\', '\\' -replace '"', '\"'
    return '"' + $escaped + '"'
}

function Invoke-ProcessWithLiveLogs {
    param([string]$FilePath, [string[]]$Arguments)
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    $psi.Arguments = ($Arguments | ForEach-Object { Quote-ProcessArgument $_ }) -join " "
    $psi.WorkingDirectory = (Get-Location).Path
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    $outputHandler = [System.Diagnostics.DataReceivedEventHandler]{
        param($sender, $eventArgs)
        if ($eventArgs.Data) {
            Write-LiveLog $eventArgs.Data
        }
    }
    $errorHandler = [System.Diagnostics.DataReceivedEventHandler]{
        param($sender, $eventArgs)
        if ($eventArgs.Data) {
            Write-LiveLog $eventArgs.Data "DarkYellow"
        }
    }
    $process.add_OutputDataReceived($outputHandler)
    $process.add_ErrorDataReceived($errorHandler)
    [void]$process.Start()
    $process.BeginOutputReadLine()
    $process.BeginErrorReadLine()
    while (-not $process.WaitForExit(500)) {
        Update-GlobalTracker
    }
    $process.WaitForExit()
    $process.remove_OutputDataReceived($outputHandler)
    $process.remove_ErrorDataReceived($errorHandler)
    return $process.ExitCode
}

function Write-LiveLog {
    param([string]$Message, [string]$Color = "")
    if ($EnableStickyStatus -and -not $DisableStickyStatus -and -not [Console]::IsOutputRedirected) {
        try {
            $target = [Math]::Max(0, [Console]::WindowTop + [Console]::WindowHeight - $script:TrackerLines - 1)
            [Console]::SetCursorPosition(0, $target)
        } catch {
        }
    }
    if ($Color) {
        Write-Host $Message -ForegroundColor $Color
    } else {
        Write-Host $Message
    }
    Update-GlobalTracker
}

function Set-GlobalTrackerContext {
    param(
        [string]$Stage,
        [int]$Index,
        [int]$Total,
        [string]$Query
    )
    $script:TrackerStage = $Stage
    $script:TrackerIndex = $Index
    $script:TrackerTotal = $Total
    $script:TrackerQuery = $Query
    Update-GlobalTracker -Force
}

function Update-GlobalTracker {
    param([string]$Stage = "", [switch]$Force)
    if ($Stage) {
        $script:TrackerStage = $Stage
    }
    if ($DisableStickyStatus -or -not $EnableStickyStatus) {
        return
    }
    $now = Get-Date
    if (-not $Force -and ($now - $script:TrackerLastRender).TotalSeconds -lt 2) {
        return
    }
    $script:TrackerLastRender = $now
    if (-not [string]::IsNullOrWhiteSpace($script:TrackerDatabaseUrl)) {
        $script:TrackerCachedDb = Get-DbCounts -Url $script:TrackerDatabaseUrl
    }
    $local = Get-LocalCounts -Root $script:TrackerDataRoot
    Render-StickyTracker -Db $script:TrackerCachedDb -Local $local
}

function Get-DbCounts {
    param([string]$Url)
    if ([string]::IsNullOrWhiteSpace($Url) -or $SkipPostgresExport) {
        return $null
    }
    $code = @'
import json, sys
try:
    import psycopg
    tables = [
        "crawler_entities_raw", "city_entities", "entity_intents",
        "entity_sources", "entity_reviews", "entity_media",
        "entity_relationships", "crawler_source_pages", "crawler_entity_quality",
    ]
    out = {}
    with psycopg.connect(sys.argv[1], connect_timeout=3) as conn:
        with conn.cursor() as cur:
            for table in tables:
                cur.execute(f"select count(*) from {table}")
                out[table] = cur.fetchone()[0]
    print(json.dumps(out))
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
'@
    try {
        $json = $code | python - $Url 2>$null
        if ($json) {
            return ($json | ConvertFrom-Json)
        }
    } catch {
        return $null
    }
    return $null
}

function Get-LocalCounts {
    param([string]$Root)
    $entityRoot = Join-Path $Root "city\entities"
    $frontierPath = Join-Path $Root "checkpoints\frontier.json"
    $progressPath = Join-Path $Root "checkpoints\query_batch_progress.json"
    $entities = 0
    $frontier = 0
    $completed = 0
    $failed = 0
    if (Test-Path -LiteralPath $entityRoot) {
        $entities = @(Get-ChildItem -LiteralPath $entityRoot -Directory -ErrorAction SilentlyContinue).Count
    }
    if (Test-Path -LiteralPath $frontierPath) {
        try {
            $payload = Get-Content -Raw -LiteralPath $frontierPath | ConvertFrom-Json
            $frontier = @($payload).Count
        } catch {
            $frontier = -1
        }
    }
    if (Test-Path -LiteralPath $progressPath) {
        try {
            $progress = Get-Content -Raw -LiteralPath $progressPath | ConvertFrom-Json
            $completed = @($progress.completed_queries).Count
            $failed = @($progress.failed_queries).Count
        } catch {
        }
    }
    return [pscustomobject]@{
        entities = $entities
        frontier = $frontier
        completed = $completed
        failed = $failed
    }
}

function New-ProgressBar {
    param([int]$Current, [int]$Total, [int]$Width = 28)
    if ($Total -le 0) {
        return "[" + ("-" * $Width) + "]"
    }
    $done = [Math]::Min($Width, [Math]::Floor(($Current / [double]$Total) * $Width))
    return "[" + ("#" * $done) + ("-" * ($Width - $done)) + "]"
}

function Fit-Line {
    param([string]$Text)
    try {
        $width = [Math]::Max(40, [Console]::WindowWidth)
        if ($Text.Length -gt $width) {
            return $Text.Substring(0, $width - 1)
        }
        return $Text.PadRight($width)
    } catch {
        return $Text
    }
}

function Render-StickyTracker {
    param([object]$Db, [object]$Local)
    $bar = New-ProgressBar -Current $script:TrackerIndex -Total $script:TrackerTotal
    $query = if ($script:TrackerQuery.Length -gt 80) { $script:TrackerQuery.Substring(0, 80) } else { $script:TrackerQuery }
    if ($Db -and $Db.error) {
        $dbLine = "DB unavailable: $($Db.error)"
    } elseif ($Db) {
        $dbLine = "DB raw=$($Db.crawler_entities_raw) city=$($Db.city_entities) intents=$($Db.entity_intents) sources=$($Db.entity_sources) reviews=$($Db.entity_reviews) media=$($Db.entity_media) rel=$($Db.entity_relationships) pages=$($Db.crawler_source_pages) quality=$($Db.crawler_entity_quality)"
    } else {
        $dbLine = "DB skipped"
    }
    $line1 = "Progress $bar $($script:TrackerIndex)/$($script:TrackerTotal) stage=$($script:TrackerStage)"
    $line2 = "Query: $query"
    $line3 = $dbLine
    $line4 = "Files entities=$($Local.entities) frontier=$($Local.frontier) completed=$($Local.completed) failed=$($Local.failed) updated=$(Get-Date -Format HH:mm:ss)"
    if ([Console]::IsOutputRedirected) {
        Write-Host $line1
        Write-Host $line3
        return
    }
    try {
        $top = [Console]::WindowTop + [Console]::WindowHeight - $script:TrackerLines
        $left = 0
        [Console]::SetCursorPosition($left, $top)
        $bg = [Console]::BackgroundColor
        $fg = [Console]::ForegroundColor
        [Console]::BackgroundColor = "DarkBlue"
        [Console]::ForegroundColor = "White"
        foreach ($line in @($line1, $line2, $line3, $line4)) {
            [Console]::Write((Fit-Line $line))
        }
        [Console]::BackgroundColor = $bg
        [Console]::ForegroundColor = $fg
        [Console]::SetCursorPosition(0, [Math]::Max(0, $top - 1))
    } catch {
        Write-Host $line1
        Write-Host $line3
    }
}

function Invoke-CheckpointExport {
    param([string]$Label)
    if ($SkipPostgresExport) {
        return
    }
    Write-Step "Checkpoint export: $Label"
    Invoke-Collector postgres-export --database-url $DatabaseUrl
}

function Get-DefaultProgressPath {
    param([string]$Root)
    $checkpointDir = Join-Path $Root "checkpoints"
    New-Item -ItemType Directory -Force -Path $checkpointDir | Out-Null
    return (Join-Path $checkpointDir "query_batch_progress.json")
}

function Read-QueryList {
    param([string]$Path, [string]$FallbackQuery)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return @($FallbackQuery)
    }
    $resolved = Resolve-Path -LiteralPath $Path
    $queries = Get-Content -LiteralPath $resolved.Path |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.StartsWith("#") }
    $deduped = New-Object System.Collections.Generic.List[string]
    $seen = @{}
    foreach ($item in $queries) {
        $key = $item.ToLowerInvariant()
        if (-not $seen.ContainsKey($key)) {
            $deduped.Add($item)
            $seen[$key] = $true
        }
    }
    return $deduped.ToArray()
}

function New-ProgressState {
    param([string]$ListPath)
    return [ordered]@{
        query_list_path = $ListPath
        completed_queries = @()
        failed_queries = @()
        current_query = $null
        completed_count = 0
        paused = $false
        updated_at = (Get-Date).ToString("o")
    }
}

function Load-ProgressState {
    param([string]$Path, [string]$ListPath, [switch]$Reset)
    if ($Reset -or -not (Test-Path -LiteralPath $Path)) {
        return (New-ProgressState -ListPath $ListPath)
    }
    try {
        $state = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
        if ($null -eq $state.completed_queries) {
            $state | Add-Member -NotePropertyName completed_queries -NotePropertyValue @()
        }
        if ($null -eq $state.failed_queries) {
            $state | Add-Member -NotePropertyName failed_queries -NotePropertyValue @()
        }
        return $state
    } catch {
        Write-Warning "Could not read progress file $Path. Starting fresh. Error: $($_.Exception.Message)"
        return (New-ProgressState -ListPath $ListPath)
    }
}

function Save-ProgressState {
    param([object]$State, [string]$Path)
    $State.updated_at = (Get-Date).ToString("o")
    $State.completed_count = @($State.completed_queries).Count
    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $State | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Add-UniqueProgressValue {
    param([object]$State, [string]$Property, [string]$Value)
    $items = @($State.$Property)
    if ($items -notcontains $Value) {
        $State.$Property = @($items + $Value)
    }
}

function Remove-ProgressValue {
    param([object]$State, [string]$Property, [string]$Value)
    $State.$Property = @(@($State.$Property) | Where-Object { $_ -ne $Value })
}

function Wait-IfPaused {
    param([object]$State, [string]$Path)
    try {
        while ([Console]::KeyAvailable) {
            $key = [Console]::ReadKey($true)
            if (($key.Modifiers -band [ConsoleModifiers]::Control) -and $key.Key -eq [ConsoleKey]::S) {
                $State.paused = $true
                Save-ProgressState -State $State -Path $Path
                Write-Host ""
                Write-Host "Paused by Ctrl+S. Press Ctrl+R to resume." -ForegroundColor Yellow
            }
        }
        while ($State.paused) {
            Start-Sleep -Milliseconds 250
            if ([Console]::KeyAvailable) {
                $key = [Console]::ReadKey($true)
                if (($key.Modifiers -band [ConsoleModifiers]::Control) -and $key.Key -eq [ConsoleKey]::R) {
                    $State.paused = $false
                    Save-ProgressState -State $State -Path $Path
                    Write-Host "Resumed by Ctrl+R." -ForegroundColor Green
                    break
                }
            }
        }
    } catch {
        return
    }
}

function Invoke-CrawlFlowForQuery {
    param(
        [string]$CurrentQuery,
        [int]$Index,
        [int]$Total,
        [object]$State,
        [string]$StatePath
    )

    $State.current_query = $CurrentQuery
    Save-ProgressState -State $State -Path $StatePath
    Set-GlobalTrackerContext -Stage "query_start" -Index $Index -Total $Total -Query $CurrentQuery
    Write-Step "Query $Index/$Total`: $CurrentQuery"
    Write-Host "Hotkeys: Ctrl+S pause after current step, Ctrl+R resume." -ForegroundColor DarkYellow

    Wait-IfPaused -State $State -Path $StatePath
    Set-GlobalTrackerContext -Stage "seed" -Index $Index -Total $Total -Query $CurrentQuery
    Write-Step "Search: seeding Firecrawl query '$CurrentQuery'"
    Invoke-Collector seed firecrawl_search $CurrentQuery

    Wait-IfPaused -State $State -Path $StatePath
    Set-GlobalTrackerContext -Stage "extract" -Index $Index -Total $Total -Query $CurrentQuery
    Write-Step "Extract: running crawler batch"
    Invoke-Collector run --max-candidates $MaxCandidates --workers $Workers

    Wait-IfPaused -State $State -Path $StatePath
    Set-GlobalTrackerContext -Stage "refine" -Index $Index -Total $Total -Query $CurrentQuery
    Write-Step "Refine: recomputing canonical intent, card, evidence and relationship fields"
    if ($SkipProductionWebEnrich) {
        Invoke-RefineStep
    } else {
        Invoke-RefineStep -ProductionWebEnrich
    }
    Invoke-CheckpointExport -Label "after initial extract/refine"

    for ($pass = 1; $pass -le $ProductionEnrichmentPasses; $pass++) {
        Wait-IfPaused -State $State -Path $StatePath
        Set-GlobalTrackerContext -Stage "validate_pass_$pass" -Index $Index -Total $Total -Query $CurrentQuery
        Write-Step "Production validation pass $pass/$ProductionEnrichmentPasses`: enqueue missing geo/address enrichment"
        Invoke-Collector validate-production --enqueue --max-entities $ProductionEnrichmentMaxEntities
        Invoke-CheckpointExport -Label "after validation pass $pass"

        Wait-IfPaused -State $State -Path $StatePath
        Set-GlobalTrackerContext -Stage "enrich_crawl_pass_$pass" -Index $Index -Total $Total -Query $CurrentQuery
        Write-Step "Production enrichment crawl pass $pass/$ProductionEnrichmentPasses"
        Invoke-Collector run --max-candidates $MaxCandidates --workers $Workers

        Wait-IfPaused -State $State -Path $StatePath
        Set-GlobalTrackerContext -Stage "refine_pass_$pass" -Index $Index -Total $Total -Query $CurrentQuery
        Write-Step "Refine after production enrichment pass $pass/$ProductionEnrichmentPasses"
        if ($SkipProductionWebEnrich) {
            Invoke-RefineStep
        } else {
            Invoke-RefineStep -ProductionWebEnrich
        }
        Invoke-CheckpointExport -Label "after enrichment/refine pass $pass"
    }

    Wait-IfPaused -State $State -Path $StatePath
    Set-GlobalTrackerContext -Stage "final_validate" -Index $Index -Total $Total -Query $CurrentQuery
    Write-Step "Final production validation"
    Invoke-Collector validate-production

    Wait-IfPaused -State $State -Path $StatePath
    Set-GlobalTrackerContext -Stage "final_postgres_export" -Index $Index -Total $Total -Query $CurrentQuery
    if (-not $SkipPostgresExport) {
        Write-Step "Export: writing production-ready crawler data to local Postgres"
        Invoke-Collector postgres-export --database-url $DatabaseUrl
    } else {
        Write-Step "Export skipped by -SkipPostgresExport"
    }

    Wait-IfPaused -State $State -Path $StatePath
    Set-GlobalTrackerContext -Stage "mobile_index" -Index $Index -Total $Total -Query $CurrentQuery
    Write-Step "Export: writing mobile card index"
    Invoke-Collector mobile-index --query $CurrentQuery --limit 100 --output $MobileIndexOutput

    Wait-IfPaused -State $State -Path $StatePath
    Set-GlobalTrackerContext -Stage "coverage" -Index $Index -Total $Total -Query $CurrentQuery
    Write-Step "Coverage"
    Invoke-Collector coverage
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

function Test-Postgres {
    param([string]$Url)

    $probe = New-TemporaryFile
    $code = @'
import sys
try:
    import psycopg
except Exception as exc:
    print(f"psycopg unavailable: {exc}")
    sys.exit(2)
try:
    with psycopg.connect(sys.argv[1], connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("select 1")
            cur.fetchone()
    print("postgres ready")
except Exception as exc:
    print(f"postgres unavailable: {exc}")
    sys.exit(1)
'@
    try {
        Set-Content -LiteralPath $probe.FullName -Value $code -Encoding UTF8
        $output = & python $probe.FullName $Url 2>&1
        $exit = $LASTEXITCODE
        if ($output) {
            $output | ForEach-Object { Write-Host $_ }
        }
        return $exit -eq 0
    } finally {
        Remove-Item -LiteralPath $probe.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-PostgresPythonDependency {
    $code = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('psycopg') else 1)"
    & python -c $code *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Step "Installing Python Postgres dependency psycopg"
    & python -m pip install "psycopg[binary]>=3.2.0"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install psycopg. Run: python -m pip install 'psycopg[binary]>=3.2.0'"
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
    $resolvedDataRoot = Join-Path $RepoRoot $DataRoot
    $script:TrackerDataRoot = $resolvedDataRoot
    $script:TrackerDatabaseUrl = $DatabaseUrl
    if ([string]::IsNullOrWhiteSpace($ProgressPath)) {
        $ProgressPath = Get-DefaultProgressPath -Root $resolvedDataRoot
    }
    $queries = @(Read-QueryList -Path $QueryListPath -FallbackQuery $Query)
    if ($MaxQueries -gt 0) {
        $queries = @($queries | Select-Object -First $MaxQueries)
    }
    if ($queries.Count -eq 0) {
        throw "No queries found. Check Query or QueryListPath."
    }
    $progress = Load-ProgressState -Path $ProgressPath -ListPath $QueryListPath -Reset:$ResetProgress
    Save-ProgressState -State $progress -Path $ProgressPath

    if (-not $SkipPostgresExport) {
        Write-Step "Checking local Postgres for export"
        Ensure-PostgresPythonDependency
        if (-not (Test-Postgres -Url $DatabaseUrl)) {
            throw "Local Postgres is not ready or psycopg is missing. Expected backend DB URL: $DatabaseUrl"
        }
    }

    Write-Step "Running query batch"
    Write-Host "Queries loaded: $($queries.Count)" -ForegroundColor Green
    Write-Host "Progress file: $ProgressPath" -ForegroundColor Green
    Set-GlobalTrackerContext -Stage "batch_start" -Index 0 -Total $queries.Count -Query ""

    if (-not $SkipStructuredBulk) {
        Wait-IfPaused -State $progress -Path $ProgressPath
        Set-GlobalTrackerContext -Stage "structured_bulk" -Index 0 -Total $queries.Count -Query "OpenStreetMap + Wikidata/Wikipedia bulk"
        Write-Step "Structured acquisition: OpenStreetMap bulk with Wikidata/Wikipedia image enrichment"
        if ([string]::IsNullOrWhiteSpace($StructuredCategories)) {
            Invoke-Collector structured-bulk --limit $StructuredBulkLimit --max-wikimedia $StructuredWikimediaMaxEntities
        } else {
            Invoke-Collector structured-bulk --limit $StructuredBulkLimit --max-wikimedia $StructuredWikimediaMaxEntities --categories $StructuredCategories
        }
        Invoke-CheckpointExport -Label "after structured bulk acquisition"
    }

    $completedSet = @{}
    foreach ($done in @($progress.completed_queries)) {
        if ($done) {
            $completedSet[$done.ToLowerInvariant()] = $true
        }
    }

    for ($i = 0; $i -lt $queries.Count; $i++) {
        $current = $queries[$i]
        $key = $current.ToLowerInvariant()
        if ($completedSet.ContainsKey($key)) {
            Write-Host "Skipping completed query $($i + 1)/$($queries.Count): $current" -ForegroundColor DarkGray
            Set-GlobalTrackerContext -Stage "skip_completed" -Index ($i + 1) -Total $queries.Count -Query $current
            continue
        }
        try {
            Invoke-CrawlFlowForQuery -CurrentQuery $current -Index ($i + 1) -Total $queries.Count -State $progress -StatePath $ProgressPath
            Add-UniqueProgressValue -State $progress -Property "completed_queries" -Value $current
            Remove-ProgressValue -State $progress -Property "failed_queries" -Value $current
            $progress.current_query = $null
            $progress.paused = $false
            Save-ProgressState -State $progress -Path $ProgressPath
            $completedSet[$key] = $true
            Set-GlobalTrackerContext -Stage "query_complete" -Index ($i + 1) -Total $queries.Count -Query $current
            Write-Host "Completed query $($i + 1)/$($queries.Count): $current" -ForegroundColor Green
        } catch {
            Add-UniqueProgressValue -State $progress -Property "failed_queries" -Value $current
            $progress.current_query = $current
            Save-ProgressState -State $progress -Path $ProgressPath
            Write-Warning "Query failed $($i + 1)/$($queries.Count): $current. Error: $($_.Exception.Message)"
            if (-not $ContinueOnError) {
                throw
            }
            $progress.current_query = $null
            Save-ProgressState -State $progress -Path $ProgressPath
            Set-GlobalTrackerContext -Stage "query_failed_continue" -Index ($i + 1) -Total $queries.Count -Query $current
            Write-Host "Continuing because -ContinueOnError is enabled." -ForegroundColor Yellow
        }
    }
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
Write-Host "Done. Search -> extract -> refine -> validate -> enrich -> export completed." -ForegroundColor Green
Write-Host "Filesystem entities: $RepoRoot\$DataRoot\city\entities" -ForegroundColor Green
Write-Host "Mobile index: $RepoRoot\$DataRoot\city\indexes\$MobileIndexOutput" -ForegroundColor Green
Write-Host "Progress file: $ProgressPath" -ForegroundColor Green
if (-not $SkipPostgresExport) {
    Write-Host "Postgres export target: $DatabaseUrl" -ForegroundColor Green
}
