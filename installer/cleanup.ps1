param([Parameter(Mandatory=$true)][string]$InstallDir)

$ErrorActionPreference = 'Stop'
$Target = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$Root = [IO.Path]::GetPathRoot($Target).TrimEnd('\')
$MarkerPath = Join-Path $Target '.auto_reply_install.json'
Start-Sleep -Seconds 3
try {
    if (-not $Target -or $Target -eq $Root -or -not (Test-Path -LiteralPath $MarkerPath)) {
        throw '卸载目录标记不存在。'
    }
    for ($part = $Target; $part; $part = Split-Path -Parent $part) {
        if (Test-Path -LiteralPath $part) {
            if ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw '卸载目录包含重定向路径。'
            }
        }
        if ($part -eq [IO.Path]::GetPathRoot($part).TrimEnd('\')) { break }
    }
    $marker = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($marker.product_id -ne 'auto_reply.windows.v1' -or $marker.install_dir -ine $Target) {
        throw '卸载目录标记不匹配。'
    }
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        try {
            Remove-Item -LiteralPath $Target -Recurse -Force
            if (-not (Test-Path -LiteralPath $Target)) { break }
        } catch {
            if ($attempt -eq 11) { throw }
            Start-Sleep -Seconds 1
        }
    }
} catch {
    $log = Join-Path $env:TEMP 'auto_reply_uninstall_error.txt'
    $_.Exception.Message | Set-Content -LiteralPath $log -Encoding UTF8
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "卸载未能完整删除安装目录。请查看：$log",
        'AetherReply 卸载失败',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
} finally {
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
