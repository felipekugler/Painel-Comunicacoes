@echo off
title WhatsApp Web - Login (numero dedicado)
rem Este .bat fica DENTRO da pasta Projeto (versionado no Git). %~dp0 = a propria Projeto.
cd /d "%~dp0"
echo Abrindo o WhatsApp Web para login (numero DEDICADO)...
echo Escaneie o QR com o telefone do numero dedicado quando a janela abrir.
echo.
".venv\Scripts\python.exe" -m ingestao.whatsapp_client login
echo.
echo (Pode fechar esta janela.)
pause
