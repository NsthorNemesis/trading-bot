# deploy_vps.ps1 - Trading Bot v11
# Uso: .\deploy_vps.ps1
# Requiere OpenSSH (incluido en Windows 10/11)

param(
    [string]$IP       = "159.223.124.186",
    [string]$User     = "ubuntu",
    [string]$KeyPath  = "$HOME\.ssh\id_rsa",
    [string]$RemoteDir = "/home/ubuntu/trading_bot_v11"
)

$ErrorActionPreference = "Stop"

function SSH-Run([string]$cmd) {
    $r = & ssh -i "$KeyPath" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "${User}@${IP}" $cmd 2>&1
    return $r
}
function SSH-Exec([string]$cmd) {
    & ssh -i "$KeyPath" -o StrictHostKeyChecking=no "${User}@${IP}" $cmd
}
function SCP-File([string]$local, [string]$remote) {
    & scp -i "$KeyPath" -o StrictHostKeyChecking=no "$local" "${User}@${IP}:$remote"
}
function SCP-Dir([string]$local, [string]$remote) {
    & scp -i "$KeyPath" -o StrictHostKeyChecking=no -r "$local" "${User}@${IP}:$remote"
}

$LOCAL = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  DEPLOY - Trading Bot v11 - DigitalOcean"                  -ForegroundColor Cyan
Write-Host "  Servidor : ${User}@${IP}"                                  -ForegroundColor Cyan
Write-Host "  SSH Key  : $KeyPath"                                       -ForegroundColor Cyan
Write-Host "  Proyecto : $LOCAL"                                         -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Verificar SSH key
Write-Host "[1/7] Verificando SSH key..." -ForegroundColor Yellow
if (-not (Test-Path $KeyPath)) {
    $alt = "$HOME\.ssh\id_ed25519"
    if (Test-Path $alt) {
        $KeyPath = $alt
        Write-Host "  Usando: $KeyPath" -ForegroundColor Yellow
    } else {
        Write-Host "  ERROR: SSH key no encontrada en $KeyPath" -ForegroundColor Red
        Write-Host "  Usa: .\deploy_vps.ps1 -KeyPath 'C:\ruta\tu_key'" -ForegroundColor Red
        exit 1
    }
}
Write-Host "  OK: $KeyPath" -ForegroundColor Green

# 2. Verificar conexion
Write-Host ""
Write-Host "[2/7] Verificando conexion SSH..." -ForegroundColor Yellow
$test = SSH-Run "echo CONEXION_OK"
if ($test -notmatch "CONEXION_OK") {
    Write-Host "  ERROR: No se pudo conectar a ${User}@${IP}" -ForegroundColor Red
    exit 1
}
$osInfo = SSH-Run "lsb_release -d 2>/dev/null | cut -f2"
Write-Host "  OK | $osInfo" -ForegroundColor Green

# 3. Limpiar servidor
Write-Host ""
Write-Host "[3/7] Limpiando servidor..." -ForegroundColor Yellow
SSH-Exec "sudo systemctl stop trading_bot 2>/dev/null; sudo systemctl disable trading_bot 2>/dev/null; sudo rm -f /etc/systemd/system/trading_bot.service; sudo systemctl daemon-reload 2>/dev/null; pkill -f 'python.*main.py' 2>/dev/null; rm -rf $RemoteDir; echo LIMPIO"
Write-Host "  Servidor limpio" -ForegroundColor Green

# 4. Crear directorios
Write-Host ""
Write-Host "[4/7] Creando directorios en servidor..." -ForegroundColor Yellow
SSH-Exec "mkdir -p $RemoteDir/logs $RemoteDir/data/backtesting $RemoteDir/data/calibration $RemoteDir/data/trades $RemoteDir/data/historical $RemoteDir/agents $RemoteDir/utils $RemoteDir/config"
Write-Host "  Directorios OK" -ForegroundColor Green

# 5. Subir archivos
Write-Host ""
Write-Host "[5/7] Subiendo archivos del proyecto..." -ForegroundColor Yellow

Write-Host "  Archivos Python principales..."
$pyFiles = Get-ChildItem "$LOCAL\*.py" | Where-Object { $_.Name -notmatch "^(fast_backtest|run_backtest)" }
foreach ($f in $pyFiles) { SCP-File $f.FullName "$RemoteDir/" | Out-Null }

Write-Host "  Archivos de configuracion..."
foreach ($f in @("requirements.txt", "trading_bot.service", "setup_server.sh", "bot_control.sh")) {
    $fp = "$LOCAL\$f"
    if (Test-Path $fp) { SCP-File $fp "$RemoteDir/" | Out-Null }
}

Write-Host "  Agentes..."
if (Test-Path "$LOCAL\agents") { SCP-Dir "$LOCAL\agents" "$RemoteDir/" | Out-Null }

Write-Host "  Utilidades..."
if (Test-Path "$LOCAL\utils")  { SCP-Dir "$LOCAL\utils"  "$RemoteDir/" | Out-Null }

Write-Host "  Config..."
if (Test-Path "$LOCAL\config") { SCP-Dir "$LOCAL\config" "$RemoteDir/" | Out-Null }

Write-Host "  Parametros de calibracion..."
if (Test-Path "$LOCAL\data\calibration") { SCP-Dir "$LOCAL\data\calibration" "$RemoteDir/data/" | Out-Null }

Write-Host "  Archivos subidos OK" -ForegroundColor Green

# 6. Subir .env
Write-Host ""
Write-Host "[6/7] Subiendo .env (credenciales)..." -ForegroundColor Yellow
$envFile = "$LOCAL\.env"
if (Test-Path $envFile) {
    SCP-File $envFile "$RemoteDir/.env"
    SSH-Exec "chmod 600 $RemoteDir/.env"
    Write-Host "  .env subido (permisos 600)" -ForegroundColor Green
} else {
    Write-Host "  ERROR: .env no encontrado en $LOCAL" -ForegroundColor Red
    exit 1
}

# 7. Setup en servidor
Write-Host ""
Write-Host "[7/7] Ejecutando setup en servidor (2-3 minutos)..." -ForegroundColor Yellow
SSH-Exec "cd $RemoteDir && chmod +x setup_server.sh bot_control.sh && bash setup_server.sh"

# Resultado
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  DEPLOY COMPLETADO"                                          -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Ver estado:"    -ForegroundColor Cyan
Write-Host "  ssh -i `"$KeyPath`" ${User}@${IP} 'sudo systemctl status trading_bot'" -ForegroundColor White
Write-Host ""
Write-Host "  Ver logs vivo:" -ForegroundColor Cyan
Write-Host "  ssh -i `"$KeyPath`" ${User}@${IP} 'tail -f $RemoteDir/logs/trading_bot.log'" -ForegroundColor White
Write-Host ""
Write-Host "  Entrar al servidor:" -ForegroundColor Cyan
Write-Host "  ssh -i `"$KeyPath`" ${User}@${IP}" -ForegroundColor White
Write-Host ""
