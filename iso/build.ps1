#Requires -Version 5.1
<#
.SYNOPSIS
  Construit l'ISO appliance Rackloom sous Windows via Docker Desktop.

.DESCRIPTION
  Aucune distro WSL Ubuntu + apt n'est requise. Prerequis :
    - Docker Desktop en mode Linux containers
    - Acces reseau a releases.ubuntu.com (ou cache local)

  Les images :appliance sont construites/sauvegardees sur l'hote (demon Desktop),
  puis le reconditionnement (gpg, xorriso, .deb) tourne dans un conteneur Linux
  (iso/Dockerfile.builder) qui reutilise iso/build.sh.

.PARAMETER TestSeed
  Graine autoinstall non interactive (equivalent --test-seed).

.PARAMETER SkipImages
  N'embarque pas les images Docker (equivalent --skip-images).

.PARAMETER Help
  Affiche cette aide.
#>
param(
  [switch]$TestSeed,
  [switch]$SkipImages,
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
  Write-Host ""
  Write-Host "== $Message"
}

# PowerShell 5.1 + ErrorAction Stop : le stderr d'un exe natif devient terminant.
# On abaisse temporairement ErrorAction pour lire $LASTEXITCODE sans planter.
function Invoke-Docker {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$DockerArgs,
    [switch]$AllowFailure
  )
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & docker @DockerArgs 1>$null 2>$null
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  if (-not $AllowFailure -and $code -ne 0) {
    throw ("echec docker {0} (code {1})" -f ($DockerArgs -join " "), $code)
  }
  return $code
}

function Test-DockerImage([string]$Ref) {
  return ((Invoke-Docker -DockerArgs @("image", "inspect", $Ref) -AllowFailure) -eq 0)
}

function Invoke-DockerChecked {
  param(
    [Parameter(Mandatory = $true)]
    [string[]]$DockerArgs
  )
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & docker @DockerArgs
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $prev
  }
  if ($code -ne 0) {
    throw ("echec docker {0} (code {1})" -f ($DockerArgs -join " "), $code)
  }
}

if ($Help) {
  Write-Host @"
Usage: .\iso\build.ps1 [-TestSeed] [-SkipImages] [-Help]

Construit l'ISO appliance sous Windows via Docker Desktop (Linux containers).
Aucune distro WSL ni installation locale de xorriso/gpg/apt.

  -TestSeed     graine autoinstall non interactive
  -SkipImages   n'embarque pas les images Docker

Sortie : dist\rackloom-appliance-*.iso (+ .sha256 + .manifest.txt)
D3 : ne pas publier hors laboratoire.
"@
  exit 0
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$IsoDir = Join-Path $Root "iso"
$OutDir = if ($env:RACKLOOM_ISO_OUT) { $env:RACKLOOM_ISO_OUT } else { Join-Path $Root "dist" }
$CacheDir = if ($env:RACKLOOM_ISO_CACHE) { $env:RACKLOOM_ISO_CACHE } else {
  Join-Path $env:LOCALAPPDATA "rackloom-iso-cache"
}
$WorkDir = if ($env:RACKLOOM_ISO_WORK) { $env:RACKLOOM_ISO_WORK } else {
  Join-Path $env:TEMP "rackloom-iso-work"
}
$HostImages = Join-Path $WorkDir "host-images"
$BuilderTag = "rackloom/iso-builder:appliance"
$ImageTag = "appliance"

New-Item -ItemType Directory -Force -Path $OutDir, $CacheDir, $WorkDir, $HostImages | Out-Null

function ConvertTo-DockerPath([string]$Path) {
  # Docker Desktop accepte C:\foo ou C:/foo ; normalise en slash.
  return ($Path -replace "\\", "/")
}

function Assert-DockerLinux {
  Write-Step "verification Docker Desktop (Linux containers)"
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & docker version 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
      throw "docker introuvable ou demon inaccessible. Installez Docker Desktop et demarrez-le."
    }
    $serverOs = (& docker version --format "{{.Server.Os}}" 2>$null)
  } finally {
    $ErrorActionPreference = $prev
  }
  if (-not $serverOs) {
    throw "impossible de lire docker version. Verifiez que Docker Desktop est demarre."
  }
  if ($serverOs -ne "linux") {
    throw "Docker Desktop doit etre en mode Linux containers (actuel: $serverOs). Menu : Switch to Linux containers."
  }
  Write-Host "-- Docker OK (Server.Os=$serverOs)"
}

function Get-GitShortSha {
  if ($env:RACKLOOM_ISO_GIT_SHA) { return $env:RACKLOOM_ISO_GIT_SHA }
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $sha = & git -C $Root rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $sha) { return $sha.Trim() }
  } finally {
    $ErrorActionPreference = $prev
  }
  return "unknown"
}

function Build-ApplianceImages {
  Write-Step "construction / sauvegarde des images :$ImageTag"
  $env:DOCKER_BUILDKIT = "1"
  $pairs = @(
    @{ Ref = "rackloom/backend:$ImageTag"; Dockerfile = "backend/Dockerfile" },
    @{ Ref = "rackloom/launcher:$ImageTag"; Dockerfile = "launcher/Dockerfile" },
    @{ Ref = "rackloom/runner-ansible:$ImageTag"; Dockerfile = "runners/ansible/Dockerfile" },
    @{ Ref = "rackloom/web:$ImageTag"; Dockerfile = "compose/caddy/Dockerfile" },
    @{ Ref = "rackloom/runner-terraform:$ImageTag"; Dockerfile = "runners/terraform/Dockerfile" }
  )
  foreach ($p in $pairs) {
    if (-not (Test-DockerImage $p.Ref)) {
      Write-Host "-- docker build $($p.Ref)"
      Invoke-DockerChecked -DockerArgs @(
        "build",
        "-f", (Join-Path $Root $p.Dockerfile),
        "-t", $p.Ref,
        $Root
      )
    } else {
      Write-Host "-- image deja presente : $($p.Ref)"
    }
  }
  # D3 (ouverte) : runner Terraform = binaire HashiCorp BSL 1.1.
  Write-Host "-- docker pull postgres:16.11-bookworm"
  Invoke-DockerChecked -DockerArgs @("pull", "postgres:16.11-bookworm")

  if (Test-Path $HostImages) {
    Get-ChildItem -Path $HostImages -Force | Remove-Item -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $HostImages | Out-Null

  $manifest = Join-Path $HostImages "manifest.sha256"
  $ids = Join-Path $HostImages "image-ids.txt"
  if (Test-Path $manifest) { Remove-Item $manifest -Force }
  if (Test-Path $ids) { Remove-Item $ids -Force }
  New-Item -ItemType File -Path $manifest -Force | Out-Null
  New-Item -ItemType File -Path $ids -Force | Out-Null

  foreach ($item in @(
      @{ Ref = "rackloom/backend:$ImageTag"; Name = "backend" },
      @{ Ref = "rackloom/launcher:$ImageTag"; Name = "launcher" },
      @{ Ref = "rackloom/web:$ImageTag"; Name = "web" },
      @{ Ref = "rackloom/runner-ansible:$ImageTag"; Name = "runner-ansible" },
      @{ Ref = "rackloom/runner-terraform:$ImageTag"; Name = "runner-terraform" },
      @{ Ref = "postgres:16.11-bookworm"; Name = "postgres" }
    )) {
    $dest = Join-Path $HostImages "$($item.Name).tar"
    Write-Host "-- docker save $($item.Ref) -> $($item.Name).tar"
    Invoke-DockerChecked -DockerArgs @("save", "-o", $dest, $item.Ref)
    $hash = (Get-FileHash -Algorithm SHA256 -Path $dest).Hash.ToLowerInvariant()
    Add-Content -Path $manifest -Value "$hash  $($item.Name).tar"

    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
      $idLine = & docker image inspect --format "{{.Id}} {{index .RepoTags 0}}" $item.Ref
      if ($LASTEXITCODE -ne 0) {
        throw "echec docker image inspect $($item.Ref) apres save"
      }
    } finally {
      $ErrorActionPreference = $prev
    }
    Add-Content -Path $ids -Value $idLine
  }
}

function Build-IsoBuilder {
  Write-Step "image builder ISO ($BuilderTag)"
  Invoke-DockerChecked -DockerArgs @(
    "build",
    "-f", (Join-Path $IsoDir "Dockerfile.builder"),
    "-t", $BuilderTag,
    $Root
  )
}

# Checkout Windows peut livrer CRLF malgre .gitattributes ; le conteneur bash exige LF.
function Convert-IsoTextToLf {
  Write-Step "normalisation LF sous iso/ (hote Windows)"
  $patterns = @(
    "*.sh", "*.env", "*.ps1", "*.yaml", "*.yml", "*.template",
    "user-data", "user-data.test", "meta-data", "Dockerfile*",
    "VERSION", "README.md", "*.service", "rackloom-firstboot",
    "docker-entrypoint.sh"
  )
  $files = @(Get-ChildItem -Path $IsoDir -Recurse -File -Force | Where-Object {
      $name = $_.Name
      ($patterns | Where-Object { $name -like $_ } | Select-Object -First 1)
    })
  $utf8 = New-Object System.Text.UTF8Encoding $false
  $utf8Bom = New-Object System.Text.UTF8Encoding $true
  foreach ($f in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if ($hasBom) {
      $text = $utf8.GetString($bytes, 3, $bytes.Length - 3)
    } else {
      $text = $utf8.GetString($bytes)
    }
    $normalized = $text -replace "`r`n", "`n" -replace "`r", "`n"
    $needsBom = ($f.Extension -eq ".ps1")
    if (($normalized -cne $text) -or ($needsBom -and -not $hasBom)) {
      $enc = if ($needsBom) { $utf8Bom } else { $utf8 }
      [System.IO.File]::WriteAllText($f.FullName, $normalized, $enc)
      $rel = $f.FullName.Substring($Root.Length) -replace '^[\\/]+', ''
      Write-Host "-- LF : $rel"
    }
  }
}

function Invoke-Remaster {
  Write-Step "reconditionnement ISO dans le conteneur Linux (xorriso/gpg)"
  Convert-IsoTextToLf
  $gitSha = Get-GitShortSha
  $bashArgs = @()
  if ($TestSeed) { $bashArgs += "--test-seed" }
  if ($SkipImages) { $bashArgs += "--skip-images" }

  $envArgs = @(
    "-e", "RACKLOOM_ISO_CACHE=/cache",
    "-e", "RACKLOOM_ISO_WORK=/work",
    "-e", "RACKLOOM_ISO_OUT=/out",
    "-e", "RACKLOOM_ISO_GIT_SHA=$gitSha"
  )
  if (-not $SkipImages) {
    $envArgs += @("-e", "RACKLOOM_ISO_IMAGE_DIR=/work/host-images")
  } else {
    $envArgs += @("-e", "RACKLOOM_ISO_SKIP_IMAGES=1")
  }
  if ($env:RACKLOOM_ISO_DEB_DIR) {
    $envArgs += @("-e", "RACKLOOM_ISO_DEB_DIR=/debs")
  }

  $volArgs = @(
    "-v", "$(ConvertTo-DockerPath $Root):/src",
    "-v", "$(ConvertTo-DockerPath $CacheDir):/cache",
    "-v", "$(ConvertTo-DockerPath $WorkDir):/work",
    "-v", "$(ConvertTo-DockerPath $OutDir):/out"
  )
  if ($env:RACKLOOM_ISO_DEB_DIR) {
    $volArgs += @("-v", "$(ConvertTo-DockerPath $env:RACKLOOM_ISO_DEB_DIR):/debs:ro")
  }

  # ENTRYPOINT = rackloom-iso-entrypoint (normalise CRLF puis build.sh)
  $cmd = @(
    "run", "--rm",
    "-w", "/src"
  ) + $volArgs + $envArgs + @(
    $BuilderTag
  ) + $bashArgs

  Invoke-DockerChecked -DockerArgs $cmd
}

Assert-DockerLinux
Build-IsoBuilder
if (-not $SkipImages) {
  Build-ApplianceImages
} else {
  Write-Step "images Docker non embarquees (-SkipImages)"
}
Invoke-Remaster

Write-Step "termine"
Get-ChildItem -Path $OutDir -Filter "rackloom-appliance-*.iso*" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 6 |
  ForEach-Object { Write-Host ("{0}`t{1:N0} octets" -f $_.Name, $_.Length) }
Write-Host "Sortie : $OutDir"
Write-Host "D3 ouverte : ne pas publier l'ISO hors laboratoire."
