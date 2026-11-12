# Build an MSIX package for the Microsoft Store from the PyInstaller output.
#   powershell -File packaging\windows\build_msix.ps1 -Dist dist\LPTPro -Out out
# The Store signs the package itself when it is published, so no certificate is needed.
# The identity values come from Partner Center (Product identity page); until the name
# is reserved, test placeholders are used.
param(
  [Parameter(Mandatory = $true)][string]$Dist,
  [Parameter(Mandatory = $true)][string]$Out,
  [string]$IdentityName = $(if ($env:MSIX_IDENTITY_NAME) { $env:MSIX_IDENTITY_NAME } else { "LPTPro.Test" }),
  [string]$Publisher = $(if ($env:MSIX_PUBLISHER) { $env:MSIX_PUBLISHER } else { "CN=LPTProTest" }),
  [string]$PublisherDisplayName = $(if ($env:MSIX_PUBLISHER_DISPLAY_NAME) { $env:MSIX_PUBLISHER_DISPLAY_NAME } else { "LPT Pro contributors" })
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Resolve-Path (Join-Path $here "..\..")

$init = Get-Content (Join-Path $root "lptpro\__init__.py") -Raw
if ($init -notmatch '__version__ = "(\d+)\.(\d+)\.(\d+)"') { throw "version not found" }
$version = "$($Matches[1]).$($Matches[2]).$($Matches[3]).0"

$stage = Join-Path $env:TEMP "lptpro-msix"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory $stage | Out-Null
Copy-Item $Dist (Join-Path $stage "LPTPro") -Recurse
Copy-Item (Join-Path $here "Assets") (Join-Path $stage "Assets") -Recurse

$manifest = Get-Content (Join-Path $here "AppxManifest.template.xml") -Raw
$manifest = $manifest.Replace("{{IDENTITY_NAME}}", $IdentityName).Replace("{{PUBLISHER}}", $Publisher)
$manifest = $manifest.Replace("{{PUBLISHER_DISPLAY_NAME}}", $PublisherDisplayName).Replace("{{VERSION}}", $version)
Set-Content (Join-Path $stage "AppxManifest.xml") $manifest -Encoding UTF8

$makeappx = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\makeappx.exe" |
            Sort-Object FullName -Descending | Select-Object -First 1
if (-not $makeappx) { throw "makeappx.exe not found (install the Windows SDK)" }

New-Item -ItemType Directory $Out -Force | Out-Null
$msix = Join-Path $Out "LPTPro_$($version)_x64.msix"
& $makeappx.FullName pack /d $stage /p $msix /o
if ($LASTEXITCODE -ne 0) { throw "makeappx failed" }
Write-Host "Built $msix"
