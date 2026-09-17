param(
  [switch]$OneFile,
  [switch]$SkipBundledTools
)

$ErrorActionPreference = "Stop"
$AppName = "ERNI Live Clipper Updated"
$AppZipName = "ERNI Live Clipper Updated Windows.zip"

Set-Location $PSScriptRoot
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-PythonCommand {
  try {
    python --version | Out-Null
    return "python"
  } catch {
    py --version | Out-Null
    return "py"
  }
}

function Download-File {
  param(
    [Parameter(Mandatory=$true)][string]$Url,
    [Parameter(Mandatory=$true)][string]$OutFile
  )
  Write-Host "Downloading $Url"
  $tempFile = "$OutFile.tmp"
  if (Test-Path $tempFile) {
    Remove-Item -Force $tempFile
  }

  $errors = New-Object System.Collections.Generic.List[string]
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
      Invoke-WebRequest `
        -Uri $Url `
        -OutFile $tempFile `
        -UseBasicParsing `
        -Headers @{ "User-Agent" = "Mozilla/5.0 ERNI-Live-Clipper" } `
        -TimeoutSec 120
      if ((Test-Path $tempFile) -and ((Get-Item $tempFile).Length -gt 0)) {
        Move-Item -Force $tempFile $OutFile
        return
      }
    } catch {
      $errors.Add("Invoke-WebRequest attempt ${attempt}: $($_.Exception.Message)")
      Start-Sleep -Seconds 2
    }
  }

  $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
  if ($curl) {
    try {
      & $curl.Source -L --fail --retry 5 --retry-delay 2 --connect-timeout 30 -o $tempFile $Url
      if ($LASTEXITCODE -eq 0 -and (Test-Path $tempFile) -and ((Get-Item $tempFile).Length -gt 0)) {
        Move-Item -Force $tempFile $OutFile
        return
      }
      $errors.Add("curl.exe exit code: $LASTEXITCODE")
    } catch {
      $errors.Add("curl.exe: $($_.Exception.Message)")
    }
  } else {
    $errors.Add("curl.exe not found")
  }

  try {
    Start-BitsTransfer -Source $Url -Destination $tempFile -ErrorAction Stop
    if ((Test-Path $tempFile) -and ((Get-Item $tempFile).Length -gt 0)) {
      Move-Item -Force $tempFile $OutFile
      return
    }
  } catch {
    $errors.Add("Start-BitsTransfer: $($_.Exception.Message)")
  }

  if (Test-Path $tempFile) {
    Remove-Item -Force $tempFile
  }
  throw "Could not download $Url. Try again later, check VPN/firewall, or run build_windows.ps1 -SkipBundledTools if yt-dlp/ffmpeg are already installed. Details: $($errors -join ' | ')"
}

function Test-UsableFile {
  param([Parameter(Mandatory=$true)][string]$Path)
  return (Test-Path $Path) -and ((Get-Item $Path).Length -gt 0)
}

function Ensure-WindowsTools {
  $vendor = Join-Path $PSScriptRoot "vendor\windows"
  New-Item -ItemType Directory -Force -Path $vendor | Out-Null

  $ytDlp = Join-Path $vendor "yt-dlp.exe"
  if (!(Test-UsableFile $ytDlp)) {
    Download-File `
      -Url "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
      -OutFile $ytDlp
  }

  $ffmpeg = Join-Path $vendor "ffmpeg.exe"
  $ffprobe = Join-Path $vendor "ffprobe.exe"
  $deno = Join-Path $vendor "deno.exe"
  $aria2c = Join-Path $vendor "aria2c.exe"
  if (!(Test-UsableFile $ffmpeg) -or !(Test-UsableFile $ffprobe)) {
    $zipPath = Join-Path $vendor "ffmpeg-release-essentials.zip"
    $extractPath = Join-Path $vendor "ffmpeg-extract"
    if (Test-Path $extractPath) {
      Remove-Item -Recurse -Force $extractPath
    }
    Download-File `
      -Url "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" `
      -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
    $foundFfmpeg = Get-ChildItem -Path $extractPath -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1
    $foundFfprobe = Get-ChildItem -Path $extractPath -Recurse -Filter "ffprobe.exe" | Select-Object -First 1
    if (!$foundFfmpeg -or !$foundFfprobe) {
      throw "Could not find ffmpeg.exe or ffprobe.exe inside downloaded archive."
    }
    Copy-Item $foundFfmpeg.FullName $ffmpeg -Force
    Copy-Item $foundFfprobe.FullName $ffprobe -Force
    Remove-Item -Recurse -Force $extractPath
  }

  if (!(Test-UsableFile $deno)) {
    $denoZipPath = Join-Path $vendor "deno-x86_64-pc-windows-msvc.zip"
    $denoExtractPath = Join-Path $vendor "deno-extract"
    if (Test-Path $denoExtractPath) {
      Remove-Item -Recurse -Force $denoExtractPath
    }
    Download-File `
      -Url "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip" `
      -OutFile $denoZipPath
    Expand-Archive -Path $denoZipPath -DestinationPath $denoExtractPath -Force
    $foundDeno = Get-ChildItem -Path $denoExtractPath -Recurse -Filter "deno.exe" | Select-Object -First 1
    if (!$foundDeno) {
      throw "Could not find deno.exe inside downloaded archive."
    }
    Copy-Item $foundDeno.FullName $deno -Force
    Remove-Item -Recurse -Force $denoExtractPath
  }

  if (!(Test-UsableFile $aria2c)) {
    $ariaZipPath = Join-Path $vendor "aria2-win-64bit.zip"
    $ariaExtractPath = Join-Path $vendor "aria2-extract"
    if (Test-Path $ariaExtractPath) {
      Remove-Item -Recurse -Force $ariaExtractPath
    }
    Download-File `
      -Url "https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip" `
      -OutFile $ariaZipPath
    Expand-Archive -Path $ariaZipPath -DestinationPath $ariaExtractPath -Force
    $foundAria = Get-ChildItem -Path $ariaExtractPath -Recurse -Filter "aria2c.exe" | Select-Object -First 1
    if (!$foundAria) {
      throw "Could not find aria2c.exe inside downloaded archive."
    }
    Copy-Item $foundAria.FullName $aria2c -Force
    Remove-Item -Recurse -Force $ariaExtractPath
  }

  return @{
    YtDlp = $ytDlp
    Ffmpeg = $ffmpeg
    Ffprobe = $ffprobe
    Deno = $deno
    Aria2c = $aria2c
  }
}

$python = Get-PythonCommand
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt

$tools = $null
if (!$SkipBundledTools) {
  $tools = Ensure-WindowsTools
}

$oldBuild = "build\$AppName"
$oldAppDir = "dist\$AppName"
$oldOneFile = "dist\$AppName.exe"
$oldZip = "dist\$AppZipName"
foreach ($path in @($oldBuild, $oldAppDir, $oldOneFile, $oldZip)) {
  if (Test-Path $path) {
    Remove-Item -Recurse -Force $path
  }
}

$buildMode = if ($OneFile) { "--onefile" } else { "--onedir" }
$buildArgs = @(
  "-m", "PyInstaller",
  "-y",
  "--name", $AppName,
  "--windowed",
  $buildMode,
  "--add-data", "HOTKEYS.md;.",
  "--add-data", "WINDOWS_BUILD.md;.",
  "app.py"
)

& $python @buildArgs

if ($OneFile) {
  if ($tools) {
    $toolsDir = "dist\tools"
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    Copy-Item $tools.YtDlp (Join-Path $toolsDir "yt-dlp.exe") -Force
    Copy-Item $tools.Ffmpeg (Join-Path $toolsDir "ffmpeg.exe") -Force
    Copy-Item $tools.Ffprobe (Join-Path $toolsDir "ffprobe.exe") -Force
    Copy-Item $tools.Deno (Join-Path $toolsDir "deno.exe") -Force
    Copy-Item $tools.Aria2c (Join-Path $toolsDir "aria2c.exe") -Force
  }
  Write-Host "Built: dist\$AppName.exe"
} else {
  $appDir = "dist\$AppName"
  $exePath = "$appDir\$AppName.exe"
  $zipPath = "dist\$AppZipName"
  if ($tools) {
    $toolsDir = Join-Path $appDir "tools"
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    Copy-Item $tools.YtDlp (Join-Path $toolsDir "yt-dlp.exe") -Force
    Copy-Item $tools.Ffmpeg (Join-Path $toolsDir "ffmpeg.exe") -Force
    Copy-Item $tools.Ffprobe (Join-Path $toolsDir "ffprobe.exe") -Force
    Copy-Item $tools.Deno (Join-Path $toolsDir "deno.exe") -Force
    Copy-Item $tools.Aria2c (Join-Path $toolsDir "aria2c.exe") -Force
  }
  Compress-Archive -Path "$appDir\*" -DestinationPath $zipPath
  Write-Host "Built: $exePath"
  Write-Host "Portable ZIP: $zipPath"
}

Write-Host ""
Write-Host "Recommended build for Windows is the default onedir mode because it starts faster."
Write-Host "For a single slower EXE, run: .\build_windows.ps1 -OneFile"
