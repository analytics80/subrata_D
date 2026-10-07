@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Add your OpenAI key in .env, save, close Notepad.
  notepad .env
)
start "" http://localhost:8000
python server.py
pause
