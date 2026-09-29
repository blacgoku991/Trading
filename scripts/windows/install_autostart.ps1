# Installe la tache planifiee qui lance le bot a chaque ouverture de session Windows (VPS, docs/RUNBOOK.md).
# La tache lance scripts\run_forever.py (le gardien), qui relance le bot apres un plantage ou une coupure.
# Texte sans accents : Windows PowerShell 5.1 lit un script sans BOM dans l'encodage ANSI.
#
# Depuis le dossier du depot :
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -Remove
#   (option -Bot live : bot principal run_live.py au lieu de l'experience de scalping)
param(
    [ValidateSet("scalp", "live")]
    [string] $Bot = "scalp",
    [switch] $Remove
)
$ErrorActionPreference = "Stop"
$TaskName = "goldbot-$Bot"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Tache $TaskName retiree. Le bot ne demarrera plus tout seul."
    exit 0
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Keeper = Join-Path $Root "scripts\run_forever.py"
if (-not (Test-Path $Python)) {
    throw "Python du bot introuvable : $Python (README, section Windows, etape 2)"
}
if (-not (Test-Path (Join-Path $Root ".env"))) {
    throw "Fichier .env introuvable dans $Root (README, section Windows, etape 4)"
}

$User = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Keeper`" --bot $Bot" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Trigger.Delay = "PT1M"  # une minute apres l'ouverture de session : reseau et bureau prets
# Aucune limite de duree (PT0S), une seule instance, priorite normale (la valeur par defaut 7 est basse).
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Priority 5
# Session ouverte (Interactive) : le terminal MT5 et Python doivent tourner dans la meme session Windows.
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description "goldbot ($Bot) : lance scripts\run_forever.py a l'ouverture de session" `
    -Force | Out-Null

Write-Host "Tache $TaskName installee : le bot demarre 1 minute apres chaque ouverture de session de $User."
Write-Host "Pour la lancer tout de suite : Start-ScheduledTask -TaskName $TaskName"
