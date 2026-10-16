@echo off
rem Makes this machine the Heartcraft game server. Code and data live on the router share next to
rem this file; the code is copied here on every start, the data is read and written on the share.
rem Exit code 3 means another machine has become the server.
setlocal
set "SHARE=%~dp0"
set "HERE=%LOCALAPPDATA%\heartcraft"
if not exist "%HERE%" mkdir "%HERE%"
set "PY=py -3"
where py >nul 2>nul || set "PY=python"
:loop
copy /y "%SHARE%index.html" "%HERE%\" >nul || goto retry
copy /y "%SHARE%server.py" "%HERE%\" >nul || goto retry
%PY% "%HERE%\server.py" --port 8092 --page "%HERE%\index.html" --worlds "%SHARE%worlds" --admin "%SHARE%admin" --data "%SHARE%data" --claim "%SHARE%host"
if %errorlevel%==0 exit /b 0
if %errorlevel%==3 (
    echo Another machine is the game server now.
    pause
    exit /b 0
)
:retry
echo Server stopped, restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
