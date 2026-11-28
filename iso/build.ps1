#Requires -Version 5.1
<#
.SYNOPSIS
  Construit l'ISO appliance Rackloom sous Windows via Docker Desktop.

.DESCRIPTION
  Aucune distro WSL Ubuntu + apt n'est requise. Prérequis :
    - Docker Desktop en mode Linux containers
    - Accès réseau à releases.ubuntu.com (ou cache local)

  Les images :appliance sont construites/sauvegardées sur l'hôte (démon Desktop),
  puis le reconditionnement (gpg, xorriso, .deb) tourne dans un conteneur Linux
  (iso/Dockerfile.builder) qui réutilise iso/build.sh.

.PARAMETER TestSeed
  Graine autoinstall non interactive (équivalent --test-seed).

.PARAMETER SkipImages
  N'embarque pas les images Docker (équivalent --skip-images).

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
  Write-Step "vérification Docker Desktop (Linux containers)"
  try {
    docker version | Out-Null
  } catch {
    throw "docker introuvable ou démon inaccessible. Installez Docker Desktop et démarrez-le."
  }
  $serverOs = (docker version --format "{{.Server.Os}}" 2>$null)
  if (-not $serverOs) {
    throw "impossible de lire docker version. Vérifiez que Docker Desktop est démarré."
  }
  if ($serverOs -ne "linux") {
    throw "Docker Desktop doit être en mode Linux containers (actuel: $serverOs). Menu : Switch to Linux containers."
  }
  Write-Host "-- Docker OK (Server.Os=$serverOs)"
}

function Get-GitShortSha {
  if ($env:RACKLOOM_ISO_GIT_SHA) { return $env:RACKLOOM_ISO_GIT_SHA }
  try {
    $sha = & git -C $Root rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $sha) { return $sha.Trim() }
  } catch { }
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
    $null = docker image inspect $p.Ref 2>$null
    if ($LASTEXITCODE -ne 0) {
      Write-Host "-- docker build $($p.Ref)"
      docker build -f (Join-Path $Root $p.Dockerfile) -t $p.Ref $Root
      if ($LASTEXITCODE -ne 0) { throw "échec docker build $($p.Ref)" }
    } else {
      Write-Host "-- image déjà présente : $($p.Ref)"
    }
  }
  # D3 (ouverte) : runner Terraform = binaire HashiCorp BSL 1.1.
  docker pull postgres:16.11-bookworm
  if ($LASTEXITCODE -ne 0) { throw "échec docker pull postgres:16.11-bookworm" }

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
    Write-Host "-- docker save $($item.Ref) → $($item.Name).tar"
    docker save -o $dest $item.Ref
    if ($LASTEXITCODE -ne 0) { throw "échec docker save $($item.Ref)" }
    $hash = (Get-FileHash -Algorithm SHA256 -Path $dest).Hash.ToLowerInvariant()
    Add-Content -Path $manifest -Value "$hash  $($item.Name).tar"
    $idLine = docker image inspect --format "{{.Id}} {{index .RepoTags 0}}" $item.Ref
    Add-Content -Path $ids -Value $idLine
  }
}

function Build-IsoBuilder {
  Write-Step "image builder ISO ($BuilderTag)"
  docker build -f (Join-Path $IsoDir "Dockerfile.builder") -t $BuilderTag $Root
  if ($LASTEXITCODE -ne 0) { throw "échec construction $BuilderTag" }
}

function Invoke-Remaster {
  Write-Step "reconditionnement ISO dans le conteneur Linux (xorriso/gpg)"
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

  $cmd = @(
    "run", "--rm",
    "-w", "/src"
  ) + $volArgs + $envArgs + @(
    $BuilderTag,
    "bash", "iso/build.sh"
  ) + $bashArgs

  & docker @cmd
  if ($LASTEXITCODE -ne 0) { throw "échec du reconditionnement ISO (conteneur)" }
}

Assert-DockerLinux
Build-IsoBuilder
if (-not $SkipImages) {
  Build-ApplianceImages
} else {
  Write-Step "images Docker non embarquées (-SkipImages)"
}
Invoke-Remaster

Write-Step "terminé"
Get-ChildItem -Path $OutDir -Filter "rackloom-appliance-*.iso*" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 6 |
  ForEach-Object { Write-Host ("{0}`t{1:N0} octets" -f $_.Name, $_.Length) }
Write-Host "Sortie : $OutDir"
Write-Host "D3 ouverte : ne pas publier l'ISO hors laboratoire."
