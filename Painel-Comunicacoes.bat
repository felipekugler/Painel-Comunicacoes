@echo off
rem ============================================================
rem  Painel de Comunicações — inicia o servidor (se precisar) e abre o browser
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
    call :abrirbrowser
    goto :fim
)

rem Nao esta rodando — inicia em janela separada e abre o browser
echo Iniciando Painel de Comunicações...
start "Painel de Comunicações" /MIN "%PYTHON%" "%ROOT%app.py"

rem Aguarda o servidor ficar pronto checando a porta 8002 (ate 30s), em vez de
rem uma espera fixa — em maquinas mais lentas (1a execucao, antivirus, HD lento)
rem o servidor pode demorar mais que alguns segundos pra subir, e abrir o
rem browser cedo demais da ERR_CONNECTION_REFUSED.
echo Aguardando o servidor iniciar...
set /A _tentativas=0
:esperaservidor
netstat -ano -p TCP | findstr /R /C:":8002 .* LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 goto :servidorpronto
set /A _tentativas+=1
if %_tentativas% GEQ 30 (
    echo [AVISO] O servidor esta demorando mais que o normal. Abrindo o navegador mesmo assim...
    goto :servidorpronto
)
rem "ping" como espera de ~1s: ao contrario do "timeout", nao exige um console
rem interativo (handle de entrada) — funciona tambem se o .bat for chamado de
rem forma nao-interativa (outro script, tarefa agendada etc.), onde "timeout"
rem falha de cara com "nao ha suporte para o redirecionamento de entrada" e o
rem loop deixaria de esperar de verdade.
ping -n 2 127.0.0.1 >nul
goto :esperaservidor

:servidorpronto
echo Abrindo browser em %URL%
call :abrirbrowser

goto :fim

rem Abre o painel respeitando a preferencia "abrir_tela_cheia" (Configuracoes).
rem O abrir_painel.py le a config e abre em tela cheia (Chrome --start-fullscreen)
rem ou como aba do navegador padrao. Se algo falhar, cai no navegador padrao.
:abrirbrowser
"%PYTHON%" "%ROOT%abrir_painel.py"
if %ERRORLEVEL% NEQ 0 start "" "%URL%"
exit /b

:fim
endlocal
