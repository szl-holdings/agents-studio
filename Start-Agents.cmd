@echo off
title SZL Agent Studio
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch-Studio.ps1"
if errorlevel 1 pause
