# Benchmark release runbook

Run these commands only after workflow, provider, context, graph, and benchmark sources are frozen. Do not compare a result made from one source tree with a package made from another.

## Preflight

```powershell
Set-Location '<path-to-Our-Harness>'
$env:PYTHONPATH = (Resolve-Path '.\src').Path
py -B -m unittest tests.test_benchmark tests.test_external_benchmark -q
py -B -m compileall -q src tests scripts
git status --short
git rev-parse HEAD
```

Archive any prior named result before replacing it. Keep the old result and report beside its source revision.

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$archive = Join-Path 'benchmark-archive' $stamp
New-Item -ItemType Directory -Path $archive -Force | Out-Null
foreach ($name in 'benchmark-current.json','benchmark-local-qwen3b.json') {
    if (Test-Path -LiteralPath $name) { Move-Item -LiteralPath $name -Destination $archive }
}
```

## Provider-free v3 result

```powershell
py -B -m our_harness benchmark `
  --seed 20260814 `
  --repetitions 1 `
  --format json `
  --output benchmark-current.json

$current = Get-Content -Raw -LiteralPath '.\benchmark-current.json' | ConvertFrom-Json
if ($current.schema_version -ne 3 -or $current.deterministic_score -ne 100 -or $current.agentic_score -ne 'not_run') {
    throw 'Provider-free v3 result failed its release contract.'
}
```

## Local Qwen v3 result

The checked profile names `hf.co/Qwen/Qwen2.5-Coder-3B-Instruct-GGUF:Q4_K_M` at `http://127.0.0.1:11434`. Set `OLLAMA_MODELS` to the model store that contains this tag. Set `OLLAMA_EXE` to the Ollama executable when `ollama` is not on `PATH`. Confirm the tag through the running server before inference.

The separate strict 7B profile is `benchmark-local-qwen7b-profile.json`. Its official Ollama tag is `qwen2.5-coder:7b-instruct-q4_K_M`. The tag pulled on 2026-08-15 had manifest digest `dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364`, size `4683087561` bytes, parameter size `7.6B`, and quantization `Q4_K_M`. Re-read `/api/tags` and record the digest for every published run; do not assume a mutable tag still resolves to this digest.

The commands below reuse a matching server if one already owns the endpoint. Otherwise they start one process and stop only that exact process. They never kill Ollama by name.

```powershell
$ollamaExe = if ($env:OLLAMA_EXE) {
    (Resolve-Path -LiteralPath $env:OLLAMA_EXE -ErrorAction Stop).Path
} else {
    (Get-Command 'ollama' -ErrorAction Stop).Source
}
$ollamaModels = $env:OLLAMA_MODELS
if (-not $ollamaModels -or -not (Test-Path -LiteralPath $ollamaModels -PathType Container)) {
    throw 'Set OLLAMA_MODELS to the local model-store directory before this run.'
}
$expectedModel = 'hf.co/Qwen/Qwen2.5-Coder-3B-Instruct-GGUF:Q4_K_M'
$ollamaOwned = $null

try {
    try {
        $null = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
    } catch {
        $env:OLLAMA_HOST = '127.0.0.1:11434'
        $env:OLLAMA_MODELS = $ollamaModels
        New-Item -ItemType Directory -Path '.\benchmark-logs' -Force | Out-Null
        $ollamaOwned = Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -PassThru -WindowStyle Hidden `
          -RedirectStandardOutput '.\benchmark-logs\ollama-qwen.stdout.log' `
          -RedirectStandardError '.\benchmark-logs\ollama-qwen.stderr.log'
        $deadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Milliseconds 500
            try { $version = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2 } catch { $version = $null }
        } until ($version -or (Get-Date) -ge $deadline -or $ollamaOwned.HasExited)
        if (-not $version) { throw 'The owned Ollama server did not become ready.' }
    }

    $tags = (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5).models.name
    if ($expectedModel -notin $tags) { throw "The endpoint does not expose $expectedModel. Stop and fix the server model root." }

    py -B -m our_harness benchmark `
      --seed 20260814 `
      --repetitions 1 `
      --provider-profile '.\benchmark-local-qwen-profile.json' `
      --format json `
      --output benchmark-local-qwen3b.json
    if ($LASTEXITCODE -ne 0) { throw 'Local Qwen benchmark failed.' }
} finally {
    if ($null -ne $ollamaOwned -and -not $ollamaOwned.HasExited) {
        Stop-Process -Id $ollamaOwned.Id
        $ollamaOwned.WaitForExit()
    }
}

$qwen = Get-Content -Raw -LiteralPath '.\benchmark-local-qwen3b.json' | ConvertFrom-Json
if ($qwen.schema_version -ne 3 -or $qwen.agentic.status -ne 'completed') {
    throw 'Local Qwen v3 result failed its release contract.'
}
```

Record the source revision, profile hash from the result, Ollama version, model manifest digest, result SHA-256, wall time, and hardware beside the result. Do not rebuild `dist` until both results and all source checks pass.

## ChatGPT subscription comparison

`benchmark-codex-subscription-profile.json` selects the named `codex-cli` route. It uses `command: ["codex"]`; keep the executable location in `PATH` for the current shell and never write that machine path or authentication data into the profile.

```powershell
codex --version
codex login status
py -B -m our_harness benchmark `
  --seed 20260814 `
  --repetitions 1 `
  --provider-profile '.\benchmark-codex-subscription-profile.json' `
  --format json `
  --output benchmark-codex-subscription.json
if ($LASTEXITCODE -ne 0) { throw 'Codex subscription benchmark failed.' }
```

Keep this result separate from API-priced and local-model results. Its usage status is `subscription-unpriced`; do not infer a zero monetary cost.
