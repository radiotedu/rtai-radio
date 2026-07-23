@echo off
chcp 65001 >nul
title RadioTEDU AI Radio - Live Health
cd /d "C:\Users\tedu\Downloads\AI\RadioTEDU"
"C:\Users\tedu\Downloads\AI\RadioTEDU\.venv\Scripts\python.exe" "C:\Users\tedu\Downloads\AI\RadioTEDU\scripts\terminal_health_dashboard.py"
pause
