@echo off
rem FPL Team Optimizer launcher wrapper (cmd).
rem
rem This machine has Software Restriction Policies (HKLM Safer\CodeIdentifiers)
rem that refuse unsigned .ps1 files under every execution policy except Bypass,
rem so the PowerShell launcher is invoked through -ExecutionPolicy Bypass.
rem Flags pass straight through to start.ps1:
rem
rem   start.cmd                 auto: prod if frontend\dist exists, else dev
rem   start.cmd -Prod -Rebuild  prod: force frontend rebuild first
rem   start.cmd -Dev            dev: API :8000 + Vite :5173 (HMR)
rem   start.cmd -Prod -Port 9000
rem
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*