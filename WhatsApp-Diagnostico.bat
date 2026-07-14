@echo off
title WhatsApp - Diagnostico de navegador (headed)
rem Este .bat fica DENTRO da pasta Projeto (versionado no Git). %~dp0 = a propria Projeto.
cd /d "%~dp0"
echo Testando qual navegador consegue acessar a rede em modo visivel (headed).
echo Varias janelas vao abrir e fechar rapidamente. Aguarde o resultado abaixo.
echo.
".venv\Scripts\python.exe" -m ingestao.whatsapp_client diag
echo.
echo Copie as linhas OK/FALHA acima e cole para o assistente.
pause
