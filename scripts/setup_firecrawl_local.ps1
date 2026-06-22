param(
    [string]$InstallDir = "V:\CupidAi\firecrawl",
    [string]$BullAuthKey = "local-firecrawl-admin",
    [switch]$Start
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required. Install/start Docker Desktop first."
}

try {
    docker info *> $null
} catch {
    throw "Docker Desktop is installed but the Docker engine is not running. Start Docker Desktop and retry."
}

if (-not (Test-Path $InstallDir)) {
    git clone https://github.com/firecrawl/firecrawl.git $InstallDir
}

Push-Location $InstallDir
try {
    if (-not (Test-Path ".env")) {
        @"
PORT=3002
HOST=0.0.0.0
USE_DB_AUTHENTICATION=false
BULL_AUTH_KEY=$BullAuthKey
POSTGRES_USER=firecrawl
POSTGRES_PASSWORD=firecrawl_password
POSTGRES_DB=postgres
MAX_CPU=0.8
MAX_RAM=0.8
ALLOW_LOCAL_WEBHOOKS=true
PROXY_SERVER=
PROXY_USERNAME=
PROXY_PASSWORD=
BLOCK_MEDIA=
SEARXNG_ENDPOINT=
SEARXNG_ENGINES=
SEARXNG_CATEGORIES=
OPENAI_API_KEY=
OPENAI_BASE_URL=
OLLAMA_BASE_URL=
MODEL_NAME=
MODEL_EMBEDDING_NAME=
SLACK_WEBHOOK_URL=
SUPABASE_ANON_TOKEN=
SUPABASE_URL=
SUPABASE_SERVICE_TOKEN=
AUTUMN_SECRET_KEY=
SELF_HOSTED_WEBHOOK_URL=
LOGGING_LEVEL=
TEST_API_KEY=
NUQ_BACKEND=
"@ | Set-Content -Path ".env" -Encoding UTF8
    }

    if ($Start) {
        docker compose build
        docker compose up -d
        Write-Host "Firecrawl should be available at http://localhost:3002"
        Write-Host "Queue UI: http://localhost:3002/admin/$BullAuthKey/queues"
    } else {
        Write-Host "Firecrawl source is ready at $InstallDir"
        Write-Host "Start it with: docker compose up -d --build"
    }
}
finally {
    Pop-Location
}
