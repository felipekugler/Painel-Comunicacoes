@echo off
rem ============================================================
rem  Painel DIVCOM — inicia o servidor (se precisar) e abre o browser
rem  Usar: dar duplo clique, ou criar atalho no desktop/barra de tarefas
rem ============================================================

setlocal

rem Caminho deste .bat = raiz do projeto
set ROOT=%~dp0
cd /d "%ROOT%"

set PYTHON=%ROOT%.venv\Scripts\python.exe
set URL=http://127.0.0.1:8002/

rem Confere se o venv existe
if not exist "%PYTHON%" (
    echo [ERRO] venv nao encontrado em %PYTHON%
    echo Crie com:  py -3 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt ^&^& .venv\Scripts\python -m playwright install chromium
    pause
    goto :fim
)

rem Checa se o servidor ja esta rodando (netstat na porta 8002)
netstat -ano -p TCP | findstr /R /C:":8002 .* LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo Servidor ja esta rodando. Abrindo browser...
    start "" "%URL%"
    goto :fim
)

rem Nao esta rodando — inicia em janela separada e abre o browser
echo Iniciando Painel DIVCOM...
start "Painel DIVCOM" /MIN "%PYTHON%" "%ROOT%app.py"

rem Aguarda 4s pro servidor subir
timeout /T 4 /NOBREAK >nul

echo Abrindo browser em %URL%
start "" "%URL%"

:fim
endlocal
