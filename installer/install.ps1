param(
    [string]$InstallDir = '',
    [switch]$SkipIntegration
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProductId = 'auto_reply.windows.v1'
$TaskName = 'WeChatAutoReply'
$Source = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')

function Full-Directory([string]$Path) {
    $full = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path)).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\')
    if (-not $full -or $full -eq $root) { throw '不能安装在磁盘根目录。' }
    for ($part = $full; $part; $part = Split-Path -Parent $part) {
        if (Test-Path -LiteralPath $part) {
            if ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "安装路径包含链接或重定向目录：$part"
            }
        }
        if ($part -eq [IO.Path]::GetPathRoot($part).TrimEnd('\')) { break }
    }
    return $full
}

function Select-InstallDirectory {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = '选择安装位置；将在所选目录下创建 AetherReply 文件夹。'
    $dialog.SelectedPath = [Environment]::GetFolderPath('LocalApplicationData')
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        throw '已取消安装。'
    }
    return (Join-Path $dialog.SelectedPath 'AetherReply')
}

function Find-Python {
    $candidates = @()
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { $candidates += [pscustomobject]@{ File = $launcher.Source; Prefix = @('-3') } }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { $candidates += [pscustomobject]@{ File = $python.Source; Prefix = @() } }
    foreach ($candidate in $candidates) {
        $prefix = @($candidate.Prefix)
        & $candidate.File @prefix -c 'import sys; assert sys.platform.startswith(chr(119)) and sys.version_info >= (3,11) and sys.maxsize > 2**32' 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    throw '需要 64 位 Python 3.11 或更新版本，并将 py 或 python 加入 PATH。'
}

function Copy-PythonTree([string]$From, [string]$To) {
    Get-ChildItem -LiteralPath $From -Recurse -File -Filter '*.py' | ForEach-Object {
        if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) { return }
        $relative = $_.FullName.Substring($From.Length).TrimStart('\')
        $destination = Join-Path $To $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }
}

function Test-OwnedTask([string]$Pythonw, [string]$Worker) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return $false }
    $actions = @($task.Actions)
    if ($actions.Count -ne 1 -or
        [IO.Path]::GetFullPath($actions[0].Execute) -ine [IO.Path]::GetFullPath($Pythonw) -or
        -not $actions[0].Arguments.Contains($Worker)) {
        throw "计划任务 $TaskName 已属于其他安装；未覆盖。"
    }
    return $true
}

function Create-Shortcut([string]$Path, [string]$Target, [string]$Arguments,
                         [string]$WorkingDirectory, [string]$Icon) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    if (Test-Path -LiteralPath $Path) {
        $existing = $shell.CreateShortcut($Path)
        if ($existing.TargetPath -ine $Target) { throw "快捷方式已被其他程序占用：$Path" }
    }
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    $shortcut.IconLocation = $Icon
    $shortcut.Save()
}

try {
    if (-not $InstallDir) { $InstallDir = Select-InstallDirectory }
    $Target = Full-Directory $InstallDir
    if ($Target -ieq $Source -or $Source.StartsWith($Target + '\', [StringComparison]::OrdinalIgnoreCase) -or
        $Target.StartsWith($Source + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw '安装位置不能与源码目录相同，也不能互相包含。'
    }
    $markerPath = Join-Path $Target '.auto_reply_install.json'
    $fresh = -not (Test-Path -LiteralPath $Target)
    $previousShortcuts = @()
    if (-not $fresh) {
        $existingFiles = @(Get-ChildItem -LiteralPath $Target -Force)
        if ($existingFiles.Count -gt 0) {
            if (-not (Test-Path -LiteralPath $markerPath)) {
                throw '目标文件夹非空且不是本程序的安装目录；请选择其他位置。'
            }
            $marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($marker.product_id -ne $ProductId -or $marker.install_dir -ine $Target) {
                throw '安装标记不匹配；未覆盖该文件夹。'
            }
            $previousShortcuts = @($marker.shortcuts)
        }
    }
    $venv = Join-Path $Target '.venv'
    $pythonw = Join-Path $venv 'Scripts\pythonw.exe'
    $worker = Join-Path $Target 'tools\run_watch_auto.py'
    if (-not $SkipIntegration) { $null = Test-OwnedTask $pythonw $worker }
    $interpreter = Find-Python
    Write-Host "安装到：$Target"
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    Copy-PythonTree (Join-Path $Source 'auto_reply') (Join-Path $Target 'auto_reply')
    $toolNames = @('brute_all_pids.py','debug_keys.py','probe_gate.py','probe_send_ui.py',
                   'probe_task_focus.py','probe_ui.py','run_dashboard.py',
                   'run_watch_auto.py','smoke_test.py','sticker_self_test.py')
    $toolsDir = Join-Path $Target 'tools'
    New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
    foreach ($name in $toolNames) {
        Copy-Item -LiteralPath (Join-Path $Source (Join-Path 'tools' $name)) `
            -Destination (Join-Path $toolsDir $name) -Force
    }
    New-Item -ItemType Directory -Path (Join-Path $Target 'assets') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Source 'assets\auto_reply.ico') `
        -Destination (Join-Path $Target 'assets\auto_reply.ico') -Force
    Copy-Item -LiteralPath (Join-Path $Source 'assets\aetherreply-icon.png') `
        -Destination (Join-Path $Target 'assets\aetherreply-icon.png') -Force
    New-Item -ItemType Directory -Path (Join-Path $Target 'LICENSES') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Source 'LICENSES\Apache-2.0.txt') `
        -Destination (Join-Path $Target 'LICENSES\Apache-2.0.txt') -Force
    foreach ($name in @('README.md','LICENSE','NOTICE','ideas.md','requirements.txt','config.example.toml')) {
        Copy-Item -LiteralPath (Join-Path $Source $name) -Destination (Join-Path $Target $name) -Force
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Target 'config.toml'))) {
        Copy-Item -LiteralPath (Join-Path $Target 'config.example.toml') `
            -Destination (Join-Path $Target 'config.toml')
    }
    New-Item -ItemType Directory -Path (Join-Path $Target 'data') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $Target 'installer') -Force | Out-Null
    foreach ($name in @('uninstall.ps1','cleanup.ps1')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) `
            -Destination (Join-Path $Target (Join-Path 'installer' $name)) -Force
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'uninstall.cmd') `
        -Destination (Join-Path $Target 'uninstall.cmd') -Force

    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $dashboard = Join-Path $Target 'tools\run_dashboard.py'
    $icon = Join-Path $Target 'assets\auto_reply.ico'
    $shortcuts = @(
        (Join-Path $desktop 'AetherReply.lnk'),
        (Join-Path $startMenu 'AetherReply.lnk'),
        (Join-Path $startMenu '卸载 AetherReply.lnk')
    )
    $marker = [ordered]@{
        product_id = $ProductId
        install_dir = $Target
        task_name = $TaskName
        integrated = $false
        shortcuts = $shortcuts
    }
    $marker | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $markerPath -Encoding UTF8

    $prefix = @($interpreter.Prefix)
    & $interpreter.File @prefix -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 虚拟环境失败。' }
    $installedPython = Join-Path $venv 'Scripts\python.exe'
    & $installedPython -m pip install --disable-pip-version-check -r (Join-Path $Target 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '安装 Python 依赖失败。' }
    Push-Location $Target
    try {
        & $installedPython -c 'import auto_reply, PIL, psutil, uiautomation, Crypto, zstandard'
    } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw '安装后的模块检查失败。' }
    if (-not $SkipIntegration) {
        $action = New-ScheduledTaskAction -Execute $pythonw `
            -Argument ('-X utf8 "' + $worker + '"') -WorkingDirectory $Target
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
        $principal = New-ScheduledTaskPrincipal -UserId $identity `
            -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Principal $principal -Force | Out-Null
        Create-Shortcut $shortcuts[0] $pythonw ('-X utf8 "' + $dashboard + '"') $Target $icon
        Create-Shortcut $shortcuts[1] $pythonw ('-X utf8 "' + $dashboard + '"') $Target $icon
        Create-Shortcut $shortcuts[2] (Join-Path $Target 'uninstall.cmd') '' $Target $icon
        $shell = New-Object -ComObject WScript.Shell
        foreach ($oldPath in $previousShortcuts) {
            if (-not $oldPath -or $shortcuts -contains $oldPath -or
                -not (Test-Path -LiteralPath $oldPath)) { continue }
            $oldShortcut = $shell.CreateShortcut($oldPath)
            if ($oldShortcut.TargetPath -ieq $pythonw -or
                $oldShortcut.TargetPath -ieq (Join-Path $Target 'uninstall.cmd')) {
                Remove-Item -LiteralPath $oldPath -Force
            }
        }
        $registry = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\WeChatAutoReply'
        New-Item -Path $registry -Force | Out-Null
        New-ItemProperty -Path $registry -Name DisplayName -Value 'AetherReply' -Force | Out-Null
        New-ItemProperty -Path $registry -Name DisplayVersion -Value '0.1.0' -Force | Out-Null
        New-ItemProperty -Path $registry -Name InstallLocation -Value $Target -Force | Out-Null
        New-ItemProperty -Path $registry -Name DisplayIcon -Value $icon -Force | Out-Null
        New-ItemProperty -Path $registry -Name UninstallString `
            -Value ('"' + (Join-Path $PSHOME 'powershell.exe') + '" -NoProfile -STA -ExecutionPolicy Bypass -File "' +
                    (Join-Path $Target 'installer\uninstall.ps1') + '"') -Force | Out-Null
    }
    $marker.integrated = [bool](-not $SkipIntegration)
    $marker | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $markerPath -Encoding UTF8
    Write-Host '安装完成。自动发送保持关闭；请先在 config.toml 配置模型并按 README 完成微信数据初始化。'
    Write-Host "控制台入口：$dashboard"
} catch {
    Write-Host "安装失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
