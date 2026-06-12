# ERNI Live Clipper for Windows

Эта папка - отдельный набор для Windows.

## Быстрая сборка

1. Скачай эту папку на Windows.
2. Установи Python 3.11+ с галочкой `Add python.exe to PATH`.
3. Открой PowerShell в этой папке.
4. Если PowerShell блокирует скрипты, один раз выполни:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

5. Собери portable-версию:

```powershell
.\build_windows.ps1
```

## Что получится

```text
dist\ERNI Live Clipper\ERNI Live Clipper.exe
dist\ERNI Live Clipper Windows.zip
```

Рекомендуется использовать ZIP или папку `dist\ERNI Live Clipper`, потому что так приложение открывается быстрее.

## Горячая клавиша TXT-метки

Для клипации без сворачивания:

```text
Ctrl + Alt + 7
```

Перед этим в приложении нажми `Start Tracking` на вкладке `Stream Markers`.

