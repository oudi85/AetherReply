param(
    [string]$InstallDir = '',
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProductId = 'auto_reply.windows.v1'
if (-not $InstallDir) { $InstallDir = Join-Path $PSScriptRoot '..' }
$Target = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$Root = [IO.Path]::GetPathRoot($Target).TrimEnd('\')
$MarkerPath = Join-Path $Target '.auto_reply_install.json'

function Test-SafeTarget {
    if (-not $Target -or $Target -eq $Root -or -not (Test-Path -LiteralPath $MarkerPath)) {
        throw '未找到有效的本程序安装标记，停止卸载。'
    }
    for ($part = $Target; $part; $part = Split-Path -Parent $part) {
        if (Test-Path -LiteralPath $part) {
            if ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "卸载路径包含链接或重定向目录：$part"
            }
        }
        if ($part -eq [IO.Path]::GetPathRoot($part).TrimEnd('\')) { break }
    }
    $marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($marker.product_id -ne $ProductId -or $marker.install_dir -ine $Target) {
        throw '安装标记与目标路径不一致，停止卸载。'
    }
    return $marker
}

function Remove-OwnedShortcuts($marker) {
    $shell = New-Object -ComObject WScript.Shell
    $pythonw = Join-Path $Target '.venv\Scripts\pythonw.exe'
    $uninstaller = Join-Path $Target 'uninstall.cmd'
    foreach ($path in @($marker.shortcuts)) {
        if (-not $path -or -not (Test-Path -LiteralPath $path)) { continue }
        if ([IO.Path]::GetExtension($path) -ine '.lnk') { continue }
        $shortcut = $shell.CreateShortcut($path)
        if ($shortcut.TargetPath -ieq $pythonw -or $shortcut.TargetPath -ieq $uninstaller) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}

function Stop-OwnedProcesses {
    $worker = Join-Path $Target 'tools\run_watch_auto.py'
    $dashboard = Join-Path $Target 'tools\run_dashboard.py'
    $venv = Join-Path $Target '.venv\Scripts'
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" | ForEach-Object {
        $cmd = [string]$_.CommandLine
        $exe = [string]$_.ExecutablePath
        if ($exe.StartsWith($venv + '\', [StringComparison]::OrdinalIgnoreCase) -and
            ($cmd.Contains($worker) -or $cmd.Contains($dashboard))) {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

try {
    $marker = Test-SafeTarget
    if (-not $Yes) {
        Add-Type -AssemblyName System.Windows.Forms
        $answer = [System.Windows.Forms.MessageBox]::Show(
            "卸载 AetherReply？`n`n将删除程序、计划任务、快捷方式，以及本机镜像聊天记录、配置和密钥。`n安装路径：$Target",
            '确认完整卸载',
            [System.Windows.Forms.MessageBoxButtons]::OKCancel,
            [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }
    }
    $task = Get-ScheduledTask -TaskName $marker.task_name -ErrorAction SilentlyContinue
    if ($task) {
        $actions = @($task.Actions)
        $pythonw = Join-Path $Target '.venv\Scripts\pythonw.exe'
        $worker = Join-Path $Target 'tools\run_watch_auto.py'
        if ($actions.Count -eq 1 -and
            [IO.Path]::GetFullPath($actions[0].Execute) -ieq [IO.Path]::GetFullPath($pythonw) -and
            $actions[0].Arguments.Contains($worker)) {
            Stop-ScheduledTask -TaskName $marker.task_name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $marker.task_name -Confirm:$false
        }
    }
    Stop-OwnedProcesses
    Remove-OwnedShortcuts $marker
    $registry = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WeChatAutoReply'
    if (Test-Path -LiteralPath $registry) {
        $entry = Get-ItemProperty -LiteralPath $registry
        if ($entry.InstallLocation -ieq $Target) {
            Remove-Item -LiteralPath $registry -Recurse -Force
        }
    }
    $tempScript = Join-Path $env:TEMP ("auto_reply_cleanup_" + [guid]::NewGuid().ToString('N') + '.ps1')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'cleanup.ps1') -Destination $tempScript
    Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @(
        '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $tempScript + '"'),
        '-InstallDir', ('"' + $Target + '"')) -WindowStyle Hidden
    Write-Host '卸载已启动，程序文件和本地数据将在几秒内全部删除。'
} catch {
    Write-Host "卸载失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
