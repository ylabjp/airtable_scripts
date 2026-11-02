@echo off
rem Open the mating simulation screen in the browser (close this window to stop)
chcp 65001 > nul
cd /d "%~dp0"
python webapp.py
pause
