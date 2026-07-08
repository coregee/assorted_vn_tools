# Build and deploy the VWF text hook (32-bit ddraw.dll).
# Usage:  powershell -ExecutionPolicy Bypass -File libraries\vwfhook\build.ps1 [-Game <folder>]
param([string]$Game)
$ErrorActionPreference = 'Stop'

function Find-VcVars {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path $vswhere) {
        $path = & $vswhere -latest -prerelease -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath 2>$null | Select-Object -First 1
        if ($path) {
            $vc = Join-Path $path 'VC\Auxiliary\Build\vcvars32.bat'
            if (Test-Path $vc) { return $vc }
        }
    }
    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($year in '2022', '2019', '2017') {
        foreach ($edition in 'BuildTools', 'Community', 'Professional', 'Enterprise') {
            foreach ($root in $roots) {
                $vc = Join-Path $root "Microsoft Visual Studio\$year\$edition\VC\Auxiliary\Build\vcvars32.bat"
                if (Test-Path $vc) { return $vc }
            }
        }
    }
    return $null
}

Push-Location $PSScriptRoot
try {
    if (-not $Game) { $Game = (Join-Path $PSScriptRoot '..\..\game') }
    $vc = Find-VcVars
    if (-not $vc) {
        throw "Could not find vcvars32.bat (VS C++ x86 build tools). Install 'Desktop development with C++' or the standalone VS Build Tools."
    }
    Write-Host "Using $vc"
    cmd /c "call `"$vc`" >nul 2>nul && cl /nologo /LD /O2 /MT /EHsc ddraw_vwf.cpp /Fe:ddraw.dll /link /DEF:ddraw_vwf.def user32.lib gdi32.lib kernel32.lib"
    if (-not (Test-Path ddraw.dll)) { throw 'build failed: ddraw.dll not produced' }
    # clean build intermediates
    Remove-Item ddraw.exp, ddraw.lib, ddraw_vwf.obj -ErrorAction SilentlyContinue
    Copy-Item ddraw.dll (Join-Path $Game 'ddraw.dll') -Force
    Write-Host "Built and deployed -> $(Join-Path $Game 'ddraw.dll')"
}
finally { Pop-Location }
