# Offline container recreation check. Requires Docker and a locally built project image.
$ErrorActionPreference = 'Stop'
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
$network = "team26-test-$suffix"
$volume = "team26-data-$suffix"
$fake = "team26-simulator-$suffix"
$backend = "team26-backend-$suffix"
$testDir = (Resolve-Path $PSScriptRoot).Path
$image = 'sain-hack-2026-team26-app'
$admin = @{ 'X-Admin-Key' = 'temporary-integration-key' }

function Start-Backend([string]$portMapping) {
    $id = docker run -d --rm --name $backend --network $network -p $portMapping `
        -e HACK_API_URL=http://simulator:18888 -e HACK_TEAM_TOKEN=fake-team-token `
        -e FRONTEND_ADMIN_KEY=temporary-integration-key -e ROOM_DB_PATH=/app/data/rooms.sqlite3 `
        -e SESSION_CLEANUP_INTERVAL_SECONDS=60 -v "${volume}:/app/data" `
        -v "${testDir}:/app/tests:ro" --entrypoint python $image `
        -m uvicorn tests.fake_llm_app:app --host 0.0.0.0 --port 8000
    if ($LASTEXITCODE -ne 0) { throw 'Backend container failed to start' }
    return $id
}

function Wait-ForBackend([string]$base) {
    for ($i = 0; $i -lt 40; $i++) {
        try { if ((Invoke-WebRequest -UseBasicParsing -Uri "$base/health" -TimeoutSec 2).StatusCode -eq 200) { return } } catch { }
        Start-Sleep -Milliseconds 250
    }
    throw 'Backend did not become healthy'
}

try {
    docker network create $network | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create test network' }
    docker volume create $volume | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create test volume' }
    docker run -d --rm --name $fake --network $network --network-alias simulator `
        -v "${testDir}:/tmp/tests:ro" python:3.11-slim python /tmp/tests/fake_simulator.py | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Simulator container failed to start' }
    Start-Backend '127.0.0.1::8000' | Out-Null
    $mapped = docker port $backend '8000/tcp'
    if ($mapped -notmatch ':(\d+)$') { throw 'Cannot determine published port' }
    $portNumber = $Matches[1]
    $base = "http://127.0.0.1:$portNumber"
    Wait-ForBackend $base

    $a = Invoke-RestMethod -Method Post -Uri "$base/api/v1/sessions" -Headers $admin -ContentType 'application/json' -Body '{}'
    $b = Invoke-RestMethod -Method Post -Uri "$base/api/v1/sessions" -Headers $admin -ContentType 'application/json' -Body '{}'
    $ha = @{ Authorization = "Bearer $($a.access_token)" }
    $hb = @{ Authorization = "Bearer $($b.access_token)" }
    if ($a.access_token -eq $b.access_token) { throw 'Session tokens are not isolated' }
    $catalog = Invoke-RestMethod -Uri "$base/api/v1/catalog" -Headers $ha
    if ($catalog.devices.Count -ne 1) { throw 'Catalog lookup failed' }
    $time = Invoke-RestMethod -Method Post -Uri "$base/api/v1/time" -Headers $ha -ContentType 'application/json' -Body '{"at":"2026-11-21T23:00:00+03:00"}'
    $action = Invoke-RestMethod -Method Post -Uri "$base/api/v1/actions" -Headers $ha -ContentType 'application/json' -Body '{"device_id":"light","values":{"brightness":13}}'
    $message = Invoke-RestMethod -Method Post -Uri "$base/api/v1/messages" -Headers $ha -ContentType 'application/json' -Body '{"text":"test"}'
    $other = Invoke-RestMethod -Uri "$base/api/v1/room" -Headers $hb
    $otherHistory = Invoke-RestMethod -Uri "$base/api/v1/messages" -Headers $hb
    if ($action.state.light.brightness -ne 13 -or $message.outcome -ne 'done' -or $other.state.light.brightness -ne 50 -or $otherHistory.messages.Count -ne 0) { throw 'Session isolation failed' }

    docker stop $backend | Out-Null
    Start-Backend "127.0.0.1:${portNumber}:8000" | Out-Null
    Wait-ForBackend $base
    $restoredA = Invoke-RestMethod -Uri "$base/api/v1/room" -Headers $ha
    $restoredB = Invoke-RestMethod -Uri "$base/api/v1/room" -Headers $hb
    $history = Invoke-RestMethod -Uri "$base/api/v1/messages" -Headers $ha
    $auditRaw = docker exec $fake python -c "import json,urllib.request; print(urllib.request.urlopen('http://localhost:18888/_audit').read().decode())"
    $audit = $auditRaw | ConvertFrom-Json
    if ($restoredA.at -ne $time.at -or $restoredA.state.light.brightness -ne 30 -or $restoredB.state.light.brightness -ne 50 -or $history.messages.Count -ne 2 -or $audit.run_count -ne 2) { throw 'Container recreation check failed' }
    Write-Output 'OK: two isolated sessions, catalog, actions, chat and time; container recreated with same volume, no new simulator runs.'
} finally {
    docker stop $backend 2>$null | Out-Null
    docker stop $fake 2>$null | Out-Null
    docker volume rm $volume 2>$null | Out-Null
    docker network rm $network 2>$null | Out-Null
}
