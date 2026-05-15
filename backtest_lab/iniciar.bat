@echo off
title Backtest Lab — Trading Bot v11
echo.
echo  ╔══════════════════════════════════════╗
echo  ║      BACKTEST LAB — iniciando...     ║
echo  ╚══════════════════════════════════════╝
echo.

cd /d "%~dp0"

:: Instalar dependencias si faltan
echo  Verificando dependencias...
pip install flask duckdb pandas numpy ta oandapyV20 -q --break-system-packages 2>nul
if errorlevel 1 (
    pip install flask duckdb pandas numpy ta oandapyV20 -q
)

echo  Iniciando servidor...
echo.
echo  Abriendo http://localhost:5050
echo  (Ctrl+C para detener)
echo.

:: Abrir el navegador con delay
start "" /b timeout /t 2 >nul
start http://localhost:5050

:: Lanzar Flask
python app.py

pause
