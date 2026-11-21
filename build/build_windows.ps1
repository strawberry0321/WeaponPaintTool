$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
$output = Join-Path $PSScriptRoot 'out'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$env:TEMP = $output
$env:TMP = $output
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:winexe /platform:x64 /optimize+ /debug- /utf8output `
    '/out:WeaponPaintTool.exe' '/win32icon:app\web\favicon.ico' '/win32manifest:build\desktop\app.manifest' `
    /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll `
    '/r:app\desktop\Microsoft.Web.WebView2.Core.dll' '/r:app\desktop\Microsoft.Web.WebView2.WinForms.dll' `
    'build\desktop\WeaponPaintApp.cs'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
