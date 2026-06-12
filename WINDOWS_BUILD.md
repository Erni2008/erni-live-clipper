# Windows Build

Эта инструкция собирает рабочую Windows-версию `ERNI Live Clipper`.

## Что получится

По умолчанию сборка делает быстрый portable-вариант:

```text
dist\ERNI Live Clipper\ERNI Live Clipper.exe
dist\ERNI Live Clipper Windows.zip
```

Рекомендуемый вариант - папка или ZIP. Он запускается быстрее, чем один огромный `.exe`.

## 1. Установить Python

Скачай Python 3.11+:

```text
https://www.python.org/downloads/windows/
```

Во время установки включи:

```text
Add python.exe to PATH
```

## 2. Открыть PowerShell

Открой папку проекта в PowerShell. Если Windows блокирует запуск скриптов, один раз выполни:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 3. Собрать portable-версию

Запусти:

```powershell
.\build_windows.ps1
```

Скрипт сам:

- поставит Python-зависимости;
- скачает `yt-dlp.exe`;
- скачает `ffmpeg.exe` и `ffprobe.exe`;
- добавит эти инструменты внутрь сборки;
- соберет приложение;
- создаст portable ZIP.

Готовый файл:

```text
dist\ERNI Live Clipper\ERNI Live Clipper.exe
```

Готовый архив для переноса:

```text
dist\ERNI Live Clipper Windows.zip
```

## 4. Один .exe, если очень нужно

Один `.exe` открывается медленнее, но его можно собрать так:

```powershell
.\build_windows.ps1 -OneFile
```

Результат:

```text
dist\ERNI Live Clipper.exe
```

## 5. Если уже установлены yt-dlp и ffmpeg

Можно собрать без встроенных инструментов:

```powershell
.\build_windows.ps1 -SkipBundledTools
```

Тогда на Windows должны работать команды:

```powershell
yt-dlp --version
ffmpeg -version
ffprobe -version
```

## Хоткеи на Windows

- `F8` - последние 30 секунд.
- `F9` - последние 60 секунд.
- `F10` - последние 3 минуты.
- `F11` - Mark moment.
- `Ctrl + Enter` - скачать ручной диапазон.
- `Ctrl + L` - перейти в поле ссылки.
- `Ctrl + O` - открыть папку сохранения.
- `Esc` - отменить текущий экспорт.

## Если Windows ругается на неизвестного разработчика

Это нормально для локальной unsigned-сборки:

```text
More info -> Run anyway
```

Для публичной раздачи нужен code signing certificate.
