@echo off
cd /d "%USERPROFILE%\Desktop\summarise-api"
call venv\Scripts\activate.bat

if "%ANTHROPIC_API_KEY%"=="" (
    echo.
    echo ERROR: Your API key is not set.
    echo.
    echo Get your key from: https://platform.anthropic.com
    echo Then run this command before clicking release.bat:
    echo.
    echo   set ANTHROPIC_API_KEY=your-key-here
    echo.
    pause
    exit /b 1
)

python release.py
pause
