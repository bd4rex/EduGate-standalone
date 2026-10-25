# Executed by the built-in PowerShell after the launcher supplies $job.
# The helper runs from local temporary storage, never from the removable drive.
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$guard = $null
$programRestored = $true
$outcome = @{ status = 'failed'; action = $job.action; run_id = $job.run_id }
function Write-JsonFile($path, $value) {
    $bytes = $utf8.GetBytes(($value | ConvertTo-Json -Depth 12))
    $stream = [IO.File]::Open($path + '.tmp', 'Create', 'Write', 'None')
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    Move-Item -LiteralPath ($path + '.tmp') -Destination $path -Force
}
function Within($base, $relative) {
    $basePath = [IO.Path]::GetFullPath($base).TrimEnd('\')
    $candidate = [IO.Path]::GetFullPath([IO.Path]::Combine($basePath, $relative))
    if (-not $candidate.StartsWith($basePath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Path escaped the portable folder' }
    return $candidate
}
function File-Digest($path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $inputFile = [IO.File]::OpenRead($path)
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($inputFile))).Replace('-', '').ToLowerInvariant() }
    finally { $inputFile.Dispose(); $algorithm.Dispose() }
}
try {
    $process = [Diagnostics.Process]::GetProcessById([int]$job.pid)
    [IO.File]::WriteAllText($job.ready, 'ready', $utf8)
    $process.WaitForExit()
    $stateFile = Within $job.data 'launcher-state.json'
    $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.run_id -ne $job.run_id -or $state.status -ne 'stopped' -or -not $state.saved) {
        throw 'Cannot confirm saved data, or another classroom has started. Do not unplug yet.'
    }
    $guard = [IO.File]::Open((Within $job.data '.edugate.lock'), 'Open', 'ReadWrite', 'ReadWrite')
    $guard.Lock(0, 1)
    if ($job.action -eq 'upgrade') {
        $pendingFile = Within $job.data 'pending-upgrade.json'
        $pending = Get-Content -LiteralPath $pendingFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $stage = Within $job.data $pending.staging
        $backup = Within $job.data ($pending.staging + '-previous')
        if (Test-Path -LiteralPath $backup) { throw 'Upgrade backup already exists' }
        $manifest = Get-Content -LiteralPath (Within $stage 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($entry in $manifest.files.PSObject.Properties) {
            $file = Within $stage $entry.Name
            if ((Get-Item -LiteralPath $file).Length -ne $entry.Value.size -or (File-Digest $file) -ne $entry.Value.sha256) {
                throw ('Upgrade file validation failed: ' + $entry.Name)
            }
        }
        $names = @('_internal', 'README.txt', 'package.json', 'EduGate-Standalone.exe')
        foreach ($name in $names) {
            $target = Within $job.root $name
            if ((Test-Path -LiteralPath $target) -and ((Get-Item -LiteralPath $target).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Program path is a directory link' }
        }
        New-Item -ItemType Directory -Path $backup | Out-Null
        $moved = New-Object 'System.Collections.Generic.List[string]'
        $installed = New-Object 'System.Collections.Generic.List[string]'
        try {
            foreach ($name in $names) {
                $target = Within $job.root $name
                if (Test-Path -LiteralPath $target) {
                    Move-Item -LiteralPath $target -Destination (Within $backup $name)
                    $moved.Add($name)
                }
                $source = Within $stage $name
                if (Test-Path -LiteralPath $source) {
                    Move-Item -LiteralPath $source -Destination $target
                    $installed.Add($name)
                }
            }
        } catch {
            $programRestored = $false
            # Only the four named program entries can be removed/restored.
            foreach ($name in $installed) {
                $target = Within $job.root $name
                if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
            }
            foreach ($name in $moved) { Move-Item -LiteralPath (Within $backup $name) -Destination (Within $job.root $name) }
            $programRestored = $true
            throw
        }
        Remove-Item -LiteralPath $pendingFile
        $outcome.status = 'upgraded'
        $outcome.version = $manifest.version
        $outcome.previous_program = $backup
        Write-JsonFile (Within $job.data 'last-upgrade.json') $outcome
        $guard.Dispose()
        $guard = $null
        $start = @{ FilePath = (Within $job.root 'EduGate-Standalone.exe'); WorkingDirectory = $job.root; WindowStyle = 'Hidden' }
        $arguments = @()
        if ($job.no_browser) { $arguments += '--no-browser' }
        if (-not $job.notice) { $arguments += '--no-exit-notice' }
        if ($arguments.Count) { $start.ArgumentList = $arguments }
        Start-Process @start | Out-Null
    } else {
        $outcome.status = 'saved_and_exited'
        Write-JsonFile (Within $job.data 'last-exit.json') $outcome
    }
} catch {
    $outcome.message = $_.Exception.Message
    if ($job.action -eq 'upgrade') {
        try { Write-JsonFile (Within $job.data 'last-upgrade.json') $outcome } catch {}
        if ($programRestored) {
            $pendingFile = Within $job.data 'pending-upgrade.json'
            if (Test-Path -LiteralPath $pendingFile) { Remove-Item -LiteralPath $pendingFile }
        }
    }
} finally {
    if ($null -ne $guard) { $guard.Dispose() }
    Write-JsonFile $job.result $outcome
}
if ($job.notice -and $outcome.status -ne 'upgraded') {
    Add-Type -AssemblyName System.Windows.Forms
    if ($outcome.status -eq 'saved_and_exited') {
        [Windows.Forms.MessageBox]::Show('课堂数据已保存，EduGate 已完全退出。现在可以使用 Windows 安全弹出 U 盘。', 'EduGate 已退出', 'OK', 'Information') | Out-Null
    } else {
        [Windows.Forms.MessageBox]::Show(('未能确认操作完成，请暂时不要拔出 U 盘。' + [Environment]::NewLine + $outcome.message), 'EduGate 需要检查', 'OK', 'Error') | Out-Null
    }
}
