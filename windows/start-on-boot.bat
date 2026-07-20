@echo off
rem Runs the sleep listener silently in the background at Windows startup.
rem Place a shortcut to this .bat file in shell:startup

cd /d "%~dp0"
start "" /B pythonw.exe sleep-listener.py
