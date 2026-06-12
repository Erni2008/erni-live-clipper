param(
  [switch]$OneFile,
  [switch]$SkipBundledTools
)

$ErrorActionPreference = "Stop"

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
  Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
}

function Ensure-WindowsTools {
  $vendor = Join-Path $PSScriptRoot "vendor\windows"
  New-Item -ItemType Directory -Force -Path $vendor | Out-Null

  $ytDlp = Join-Path $vendor "yt-dlp.exe"
  if (!(Test-Path $ytDlp)) {
    Download-File `
      -Url "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
      -OutFile $ytDlp
  }

  $ffmpeg = Join-Path $vendor "ffmpeg.exe"
  $ffprobe = Join-Path $vendor "ffprobe.exe"
  if (!(Test-Path $ffmpeg) -or !(Test-Path $ffprobe)) {
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

  return @{
    YtDlp = $ytDlp
    Ffmpeg = $ffmpeg
    Ffprobe = $ffprobe
  }
}

$python = Get-PythonCommand
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt

$tools = $null
if (!$SkipBundledTools) {
  $tools = Ensure-WindowsTools
}

$buildMode = if ($OneFile) { "--onefile" } else { "--onedir" }
$buildArgs = @(
  "-m", "PyInstaller",
  "-y",
  "--name", "ERNI Live Clipper",
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
  }
  Write-Host "Built: dist\ERNI Live Clipper.exe"
} else {
  $appDir = "dist\ERNI Live Clipper"
  $exePath = "$appDir\ERNI Live Clipper.exe"
  $zipPath = "dist\ERNI Live Clipper Windows.zip"
  if ($tools) {
    $toolsDir = Join-Path $appDir "tools"
    New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
    Copy-Item $tools.YtDlp (Join-Path $toolsDir "yt-dlp.exe") -Force
    Copy-Item $tools.Ffmpeg (Join-Path $toolsDir "ffmpeg.exe") -Force
    Copy-Item $tools.Ffprobe (Join-Path $toolsDir "ffprobe.exe") -Force
  }
  if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
  }
  Compress-Archive -Path "$appDir\*" -DestinationPath $zipPath
  Write-Host "Built: $exePath"
  Write-Host "Portable ZIP: $zipPath"
}

Write-Host ""
Write-Host "Recommended build for Windows is the default onedir mode because it starts faster."
Write-Host "For a single slower EXE, run: .\build_windows.ps1 -OneFile"
