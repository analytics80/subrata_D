# Alexa — Alcove Realty voice calling bot

Alexa is a browser voice bot that answers questions from the Alcove database (TogetherWecan) in Bengali, Hindi or English.
It uses the OpenAI Realtime API (`gpt-realtime`) over WebRTC: your browser streams the mic straight to OpenAI, so replies come fast and Alexa stops talking as soon as you interrupt.

## Setup (one time)
1. Copy `.env.example` to `.env` and paste your OpenAI API key. The key is saved in that file, so you don't set it again after a restart.
2. Connect Alcove data (TogetherWecan): start the server, open http://127.0.0.1:8000/oauth/login and sign in. Alexa answers only from this database. The login is saved in `alcove_oauth.json` and refreshes on its own, so keep that file private and don't share it.

## Run
Double-click `start.bat`, or:

    python server.py

Then open http://localhost:8000 in Chrome or Edge, click **Start Call** and allow the microphone.

## What it does
- Greets by IST time in Bengali ("সুপ্রভাত / শুভ অপরাহ্ন / শুভ সন্ধ্যা"), introduces itself and asks what you want to know.
- Looks up each question in the Alcove database and answers only from it. Off-topic questions are politely declined.
- Say or type an employee name or code (e.g. AR000623): every table that holds employee codes is searched in ~1–2 s and the result appears on the **Employee dashboard** (click a table to see its records). The list of those tables is cached in `employee_tables.json` for a day.
- The dashboard always shows the employee's **pending tasks**: FMS stages waiting on them, pending/revision delegation tasks, pending support tickets and checklist items that are past due but not done. Overdue ones are marked red.
- Personal details (DOB, address, blood group, personal email, PF/ESIC…) come from `alcovedb_2024.employee_master`, only when asked or when you click **Show personal details**.
- Works on phones: the panels stack into one column.
- The database allows 2000 queries an hour; one employee search uses about 11, and repeat searches within 5 minutes are served from memory.
- Saves each call transcript to `transcripts/`.
- The "Your microphone" bar moves when Alexa can hear you. If it stays flat, check the browser's mic permission and the Windows input device.

## Settings (top of server.py)
`BOT_NAME`, `VOICE` (marin / shimmer / coral), `MODEL`, `PORT`, and `silence_duration_ms`, which controls how long Alexa waits after you stop talking before it replies.
