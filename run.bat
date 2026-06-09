@echo off
REM clocket-league — Windows launcher. Edit the IP below to your clock, then run
REM this (double-click), or add it to Task Scheduler -> "At log on" to autostart.
REM Default mode (RL socket -> HTTP) needs no extra Python packages.

set CLOCK_HOST=192.168.1.50

python "%~dp0clocket_league.py" --source rl --transport http --clock-host %CLOCK_HOST%
pause
