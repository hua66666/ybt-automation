$ErrorActionPreference = "Stop"
$OutputDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $OutputDir
$ConfigDir = Join-Path $env:LOCALAPPDATA "ybt_submit"
$SolverConfigPath = Join-Path $ConfigDir "solver_config.json"

function Set-SecretEnvironmentVariable {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ProtectedValue
    )
    $secure = ConvertTo-SecureString -String $ProtectedValue
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        Set-Item -Path "Env:$Name" -Value (
            [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        )
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Import-SolverConfig {
    if (-not (Test-Path -LiteralPath $SolverConfigPath)) {
        return
    }
    try {
        $config = Get-Content -LiteralPath $SolverConfigPath -Raw | ConvertFrom-Json
        if ($config.model) {
            $env:SOLVER_MODEL = [string]$config.model
        }
        if ($config.base_url) {
            $env:SOLVER_BASE_URL = [string]$config.base_url
        }
        if ($config.api_key_protected) {
            Set-SecretEnvironmentVariable -Name "SOLVER_API_KEY" `
                -ProtectedValue ([string]$config.api_key_protected)
        }
    }
    catch {
        Write-Host "Could not load saved solver configuration: $($_.Exception.Message)" `
            -ForegroundColor Yellow
        Start-Sleep -Seconds 2
    }
}

function Configure-Solver {
    Clear-Host
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host " Configure AI Solver" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host "Press Enter to keep the value shown in brackets."
    Write-Host ""

    $currentModel = if ($env:SOLVER_MODEL) { $env:SOLVER_MODEL } else { "" }
    $modelPrompt = "Model name"
    if ($currentModel) {
        $modelPrompt = "Model name [$currentModel]"
    }
    do {
        $enteredModel = (Read-Host $modelPrompt).Trim()
        if ($enteredModel) {
            $currentModel = $enteredModel
        }
        if (-not $currentModel) {
            Write-Host "Model name is required." -ForegroundColor Yellow
        }
    } while (-not $currentModel)

    $currentBaseUrl = if ($env:SOLVER_BASE_URL) {
        $env:SOLVER_BASE_URL
    } else {
        "https://api.openai.com/v1"
    }
    $enteredBaseUrl = (Read-Host "API base URL [$currentBaseUrl]").Trim()
    if ($enteredBaseUrl) {
        $currentBaseUrl = $enteredBaseUrl
    }

    $existingProtected = ""
    if (Test-Path -LiteralPath $SolverConfigPath) {
        try {
            $existing = Get-Content -LiteralPath $SolverConfigPath -Raw | ConvertFrom-Json
            $existingProtected = [string]$existing.api_key_protected
        }
        catch {
            $existingProtected = ""
        }
    }

    $secureKey = Read-Host "API Key (hidden; blank keeps existing)" -AsSecureString
    $keyProtected = $existingProtected
    if ($secureKey.Length -gt 0) {
        $keyProtected = $secureKey | ConvertFrom-SecureString
    }
    if ($currentBaseUrl -match "api\.openai\.com" -and -not $keyProtected) {
        Write-Host "An API key is required for api.openai.com." -ForegroundColor Red
        Pause-Menu
        return
    }

    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    [ordered]@{
        version = 1
        model = $currentModel
        base_url = $currentBaseUrl
        api_key_protected = $keyProtected
    } | ConvertTo-Json | Set-Content -LiteralPath $SolverConfigPath -Encoding UTF8

    $env:SOLVER_MODEL = $currentModel
    $env:SOLVER_BASE_URL = $currentBaseUrl
    if ($keyProtected) {
        Set-SecretEnvironmentVariable -Name "SOLVER_API_KEY" -ProtectedValue $keyProtected
    }
    else {
        Remove-Item Env:SOLVER_API_KEY -ErrorAction SilentlyContinue
    }
    Write-Host ""
    Write-Host "Solver configuration saved securely for this Windows user." `
        -ForegroundColor Green
    Pause-Menu
}

function Ensure-SolverConfigured {
    if ($env:SOLVER_MODEL) {
        return $true
    }
    Write-Host ""
    Write-Host "No solver model is configured." -ForegroundColor Yellow
    $answer = (Read-Host "Configure it now? [Y/n]").Trim().ToLowerInvariant()
    if ($answer -in @("", "y", "yes")) {
        Configure-Solver
    }
    return [bool]$env:SOLVER_MODEL
}

function Pause-Menu {
    Read-Host "Press Enter to return to the menu" | Out-Null
}

function Read-ProblemId {
    while ($true) {
        $value = (Read-Host "Problem ID").Trim()
        $number = 0
        if ([int]::TryParse($value, [ref]$number) -and $number -gt 0) {
            return $number
        }
        Write-Host "Please enter a positive problem ID." -ForegroundColor Yellow
    }
}

function Show-PrerequisiteHelp {
    Write-Host ""
    Write-Host "Required environment variables:" -ForegroundColor Cyan
    Write-Host '  $env:SOLVER_MODEL = "model-name"'
    Write-Host '  $env:SOLVER_API_KEY = "api-key"'
    Write-Host '  $env:SOLVER_BASE_URL = "http://127.0.0.1:11434/v1"  # optional'
    Write-Host ""
    Write-Host "YBT credentials are requested by the Python tool only when submitting."
    Write-Host "Browser submission requires Microsoft Edge and the Python Playwright package."
    Write-Host ""
}

Import-SolverConfig

while ($true) {
    Clear-Host
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host " YBT Single-Problem Tool" -ForegroundColor Cyan
    Write-Host "==========================================" -ForegroundColor Cyan
    Write-Host "1. Fetch, generate and validate only"
    Write-Host "2. Fetch, generate, validate and confirm browser submit"
    Write-Host "3. Fetch, generate, validate and AUTO browser-submit"
    Write-Host "4. Configure AI solver"
    Write-Host "5. Show setup instructions"
    Write-Host "6. Open generated-code folder"
    Write-Host "7. Batch browser AUTO-submit a problem range"
    Write-Host "8. Diagnose submit form (no submission)"
    Write-Host "0. Exit"
    Write-Host ""
    Write-Host "Language: C source (submitted as G++14)" -ForegroundColor Cyan
    Write-Host ""
    if ($env:SOLVER_MODEL) {
        Write-Host "Solver: $env:SOLVER_MODEL" -ForegroundColor Green
    }
    else {
        Write-Host "Solver: NOT CONFIGURED" -ForegroundColor Yellow
    }
    Write-Host ""

    $choice = (Read-Host "Select").Trim()
    try {
        switch ($choice) {
            "1" {
                if (-not (Ensure-SolverConfigured)) {
                    Pause-Menu
                    continue
                }
                $problem = Read-ProblemId
                $arguments = @(".\ybt_auto.py", "--problem", "$problem", "--language", "c", "--show-code")
                & python @arguments
                Pause-Menu
            }
            "2" {
                if (-not (Ensure-SolverConfigured)) {
                    Pause-Menu
                    continue
                }
                $problem = Read-ProblemId
                $arguments = @(".\ybt_auto.py", "--problem", "$problem", "--language", "c", "--show-code", "--submit", "--ui-submit")
                & python @arguments
                Pause-Menu
            }
            "3" {
                if (-not (Ensure-SolverConfigured)) {
                    Pause-Menu
                    continue
                }
                $problem = Read-ProblemId
                Write-Host ""
                Write-Host "BROWSER AUTO-SUBMIT MODE: once the local sample passes, Edge" -ForegroundColor Yellow
                Write-Host "will open, fill the form and click the real submit button." -ForegroundColor Yellow
                Write-Host "The 40s submit interval and YBT login are still enforced." -ForegroundColor Yellow
                Write-Host ""
                $arguments = @(
                    ".\ybt_auto.py", "--problem", "$problem",
                    "--language", "c", "--show-code",
                    "--submit", "--yes", "--ui-submit"
                )
                & python @arguments
                Pause-Menu
            }
            "4" {
                Configure-Solver
            }
            "5" {
                Clear-Host
                Show-PrerequisiteHelp
                Write-Host "Opening README.md..."
                Start-Process notepad.exe -ArgumentList (Join-Path $OutputDir "README.md")
                Pause-Menu
            }
            "6" {
                $generated = Join-Path $OutputDir "generated"
                if (-not (Test-Path -LiteralPath $generated)) {
                    New-Item -ItemType Directory -Path $generated | Out-Null
                }
                Start-Process explorer.exe -ArgumentList $generated
            }
            "7" {
                if (-not (Ensure-SolverConfigured)) {
                    Pause-Menu
                    continue
                }
                Write-Host "Batch mode: process a problem range in order." -ForegroundColor Cyan
                Write-Host "Already-accepted problems are remembered and skipped on rerun."
                Write-Host ""
                $start = 0
                while ($true) {
                    $raw = (Read-Host "First problem ID").Trim()
                    if ([int]::TryParse($raw, [ref]$start) -and $start -gt 0) { break }
                    Write-Host "Please enter a positive problem ID." -ForegroundColor Yellow
                }
                $end = 0
                while ($true) {
                    $raw = (Read-Host "Last problem ID").Trim()
                    if ([int]::TryParse($raw, [ref]$end) -and $end -ge $start) { break }
                    Write-Host "Please enter a problem ID >= $start." -ForegroundColor Yellow
                }
                $count = $end - $start + 1
                Write-Host ""
                Write-Host "About to process $count problem(s): $start .. $end." -ForegroundColor Yellow
                Write-Host "Each submission waits the 40s interval, so this may take a while." -ForegroundColor Yellow
                Write-Host "Each submission is clicked through the visible YBT browser submit panel." -ForegroundColor Yellow
                Write-Host "Problems that fail the local sample are NOT submitted." -ForegroundColor Yellow
                $confirm = (Read-Host "Continue? [y/N]").Trim().ToLowerInvariant()
                if ($confirm -notin @("y", "yes")) {
                    Write-Host "Cancelled."
                    Pause-Menu
                    continue
                }
                $arguments = @(
                    ".\ybt_batch.py",
                    "--start", "$start", "--end", "$end",
                    "--language", "c", "--show-code",
                    "--submit", "--yes"
                )
                & python @arguments
                Pause-Menu
            }
            "8" {
                $problem = Read-ProblemId
                $arguments = @(".\diagnose_form.py", "$problem")
                & python @arguments
                Pause-Menu
            }
            "0" {
                exit 0
            }
            default {
                Write-Host "Unknown option." -ForegroundColor Yellow
                Start-Sleep -Milliseconds 700
            }
        }
    }
    catch {
        Write-Host ""
        Write-Host "Launch failed: $($_.Exception.Message)" -ForegroundColor Red
        Pause-Menu
    }
}
