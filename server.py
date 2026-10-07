"""
Alexa - Alcove Realty browser voice calling bot (OpenAI Realtime over WebRTC).

Run:  python server.py   then open http://localhost:8000
Needs only the Python standard library. API key is read from .env, so it
survives restarts.
"""
import base64
import csv
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
LEADS_CSV = BASE / "leads.csv"
TRANSCRIPTS = BASE / "transcripts"
ALCOVE_AUTH_FILE = BASE / "alcove_oauth.json"  # OAuth client + tokens; keep private
IST = timezone(timedelta(hours=5, minutes=30))  # no tz database needed on Windows

# ---------------------------------------------------------------- config ---
BOT_NAME = "Alexa"
COMPANY = "Alcove Realty"
MODEL = "gpt-realtime"
VOICE = "marin"  # female voices: marin, shimmer, coral
# Hosting: locally it runs on 127.0.0.1:8000. A deploy platform sets PORT (and
# PUBLIC_URL, e.g. https://alexa.example.in) and it then listens on 0.0.0.0.
PORT = int(os.environ.get("PORT") or 8000)
HOST = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")

# TogetherWecan MCP server (Alcove data). Alexa calls its tools through OpenAI.
ALCOVE_BASE = "https://togetherwecan.alcoverealty.in"
ALCOVE_MCP_URL = ALCOVE_BASE + "/mcp"
ALCOVE_SCOPE = "alcove:read"


def redirect_uri():
    """Where TogetherWecan sends the browser back after login (PUBLIC_URL when deployed)."""
    base = (os.environ.get("PUBLIC_URL") or f"http://127.0.0.1:{PORT}").rstrip("/")
    return base + "/oauth/callback"

LEAD_FIELDS = [
    "name", "phone", "language", "location_preference", "configuration",
    "budget", "purpose", "timeline", "site_visit_datetime", "notes",
]


def load_env():
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                # .env wins over any old/placeholder key set in Windows environment
                os.environ[k.strip()] = v.strip().strip('"').strip("'")


def greeting_now():
    h = datetime.now(IST).hour
    if 5 <= h < 12:
        return "Good morning", "Good morning"
    if 12 <= h < 17:
        return "Good afternoon", "Good afternoon"
    if 17 <= h < 21:
        return "Good evening", "Good evening"
    return "নমস্কার", "Good evening"  # late night: "Good night" is a farewell


def build_instructions():
    bn, en = greeting_now()
    now = datetime.now(IST).strftime("%A, %d %B %Y, %I:%M %p IST")
    user = current_user()
    return f"""You are {BOT_NAME}, a warm, polite female assistant from {COMPANY}. You answer questions using only the Alcove database (the alcove tools).
Current date/time: {now}.
You are talking to {user or "an Alcove employee"}{" (the signed-in user); address them by name" if user else ""}.

LANGUAGE
- Speak ONLY Bengali, Hindi or English. Never any other language.
- Open in Bengali. Once the user picks a language (or clearly speaks one), switch to it and stay in it.
- Use natural spoken Bengali/Hindi, not formal written style. Keep every reply to 1-2 short sentences.

OPENING (say exactly this, then stop and wait):
"{bn}! আমি {BOT_NAME}, {COMPANY} থেকে বলছি। আপনি কী জানতে চান?"

CALL FLOW:
1. Ask what they want to know, then wait.
2. Say "এক সেকেন্ড, দেখে নিচ্ছি" and search ALL the alcove databases for the answer (see SEARCH below), then answer briefly from the result.
3. Ask if they want to know anything else. Repeat from step 2 for each new question.
When they have nothing more to ask, thank them, say goodbye, and call end_call.

EMPLOYEES - fastest path, use it first:
- Whenever the user says an employee name or an employee code / ID (e.g. AR000623, "623"), call lookup_employee with exactly what they said. Do NOT use the alcove tools for this.
- It searches every database and table and shows the full result on the dashboard screen. Say only a 1-sentence summary (name, designation, department, and how many pending tasks they have, with how many are overdue), then add "বাকি সব ডিটেলস স্ক্রিনে দেখানো হয়েছে".
- If they ask about pending tasks, read out up to 3 of the most overdue ones from pending_top.
- PERSONAL DETAILS (date of birth, blood group, address, personal email/phone, WhatsApp, emergency contact, nominee, PAN, PF/ESIC etc.): call lookup_employee with personal=true. These come from alcovedb_2024.employee_master; answer only the field that was asked. If the field is empty or missing, reply "Not found".
- If it returns several matches, read out the names with designations and ask which one; then call lookup_employee again with that employee's code.
- If it returns found=false, reply exactly: "Not found"

SEARCH - for every other question:
- Always search every alcove tool / database that could hold the answer, not just the first one. Do not stop after one empty result.
- If a search returns nothing, try again with other words: spelling variants, Bengali/English names, partial names, related fields.
- Combine what you find across databases into one short answer.
- Only when every database has been searched and nothing matches, reply exactly: "Not found"

RULES
- Answer ONLY from what the alcove tools return. Never answer from your own knowledge, and never invent prices, offers, dates or any other facts.
- If the answer is not in any database (including questions not about Alcove data), reply exactly: "Not found"
- Never ask two questions in one turn.
- Always finish your current answer. If the user asked something new meanwhile, answer it next, one question at a time.
"""


TOOLS = [
    {
        "type": "function",
        "name": "save_lead",
        "description": "Save the qualified lead details collected during the call.",
        "parameters": {
            "type": "object",
            "properties": {f: {"type": "string"} for f in LEAD_FIELDS},
            "required": ["name"],
        },
    },
    {
        "type": "function",
        "name": "end_call",
        "description": "Hang up after saying goodbye.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "lookup_employee",
        "description": "Find an employee by name or employee code (e.g. AR000623) across every Alcove database and table, and show the result on the dashboard.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Employee name or code, as the user said it."},
                "personal": {"type": "boolean", "description": "true when the user asks for personal details (DOB, address, blood group, personal contact, emergency contact, nominee, PAN...)."},
            },
            "required": ["query"],
        },
    },
]


# ------------------------------------------------------ employee lookup ---
# Searched directly by the backend (not by the model) so it is fast: the
# profile comes from employee_master, then every table that has an employee
# code column is counted, and pending work is collected, in batched UNION
# queries run in parallel.
EMP_TABLES_FILE = BASE / "employee_tables.json"  # cache of where employee codes live
EMP_CODE_PATTERNS = ["emp_code", "emp_id", "employee_code"]
EMP_SKIP = re.compile(r"backup|archive|restore|zz_migration|password|_formate$|audit_log|activity_log", re.I)
MAX_SQL = 7800  # server limit is 8000 characters per query
PROFILE_FIELDS = [  # work details, always shown
    "Emp_Code", "Person_Accountable", "Designation", "Department", "Location", "Company",
    "Reporting_Manager", "Reporting_Manager_ID", "HOD", "HOD_ID", "DOJ", "Tenure",
    "Employment_Type", "STATUS", "Email_ID_Official", "Contact_number",
]
CHECKLIST_START = "2026-04-01"  # the app never scores checklist rows before this date
_emp_tables = None
FMS_COLUMNS = ["task_id", "task_name", "current_stage", "planned_end_time", "status"]
_db_slots = threading.Semaphore(6)  # the server allows 8 DB connections per user


def mcp_call(tool, **args):
    with _db_slots:
        return _mcp_call(tool, **args)


def _mcp_call(tool, **args):
    token = alcove_token()
    if not token:
        raise RuntimeError("TogetherWecan not connected. Open /oauth/login first.")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    for attempt in range(4):
        req = urllib.request.Request(
            ALCOVE_MCP_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
        except (ConnectionError, urllib.error.URLError):
            if attempt == 3:
                raise
            time.sleep(1)
            continue
        for line in raw.splitlines():  # the reply may come as a server-sent event
            if line.startswith("data:"):
                raw = line[5:]
        res = json.loads(raw).get("result", {})
        text = "\n".join(c.get("text", "") for c in res.get("content", []))
        wait = re.search(r"Rate limit reached.*?(\d+)s", text)
        if wait and attempt < 3:  # 60 requests/minute on the server
            time.sleep(min(int(wait.group(1)), 20) + 0.5)
            continue
        if "max_user_connections" in text and attempt < 3:
            time.sleep(0.7)
            continue
        if "max_questions" in text:
            raise RuntimeError("The database's hourly query limit (2000/hour) is used up. Try again later.")
        if res.get("isError") or text.startswith("Error"):
            raise RuntimeError(text[:300])
        return json.loads(text)


def sql_rows(sql, limit=1000):
    return mcp_call("run_query", database="alcovedb_2024", sql=sql, limit=limit)["rows"]


def batched(parts):
    """Group UNION parts into queries under the server's size limit."""
    batches, cur = [], []
    for p in parts:
        if cur and len(" UNION ALL ".join(cur + [p])) > MAX_SQL:
            batches.append(cur)
            cur = []
        cur.append(p)
    return batches + [cur] if cur else batches


def _schema_index():
    """Where employee codes live, and which FMS task tables have the common shape. Cached for a day."""
    global _emp_tables
    if _emp_tables is None and EMP_TABLES_FILE.exists() and time.time() - EMP_TABLES_FILE.stat().st_mtime < 86400:
        cached = json.loads(EMP_TABLES_FILE.read_text(encoding="utf-8"))
        if "fms" in cached:
            _emp_tables = cached
    if _emp_tables is None:
        found = {}
        for pattern in EMP_CODE_PATTERNS:
            for m in mcp_call("search_objects", pattern=pattern).get("matches", []):
                if m.get("type") == "BASE TABLE" and not EMP_SKIP.search(m["table"]):
                    cols = found.setdefault(f"{m['database']}.{m['table']}", [])
                    cols += [c for c in m["matched_columns"] if c not in cols]
        # FMS tables whose rows can be read as pending stages: one search per required column
        shaped = None
        for col in FMS_COLUMNS:
            have = {f"{m['database']}.{m['table']}" for m in mcp_call("search_objects", pattern=col).get("matches", [])
                    if col in [c.lower() for c in m["matched_columns"]]}
            shaped = have if shaped is None else shaped & have
        fms = {}
        for src, cols in found.items():
            doer = next((c for c in ("actual_emp_code", "actual_emp_id") if c in cols), None)
            if src.endswith(".tasks") and doer and src in shaped:
                fms[src] = doer
        _emp_tables = {"tables": dict(sorted(found.items())), "fms": dict(sorted(fms.items()))}
        EMP_TABLES_FILE.write_text(json.dumps(_emp_tables, indent=1), encoding="utf-8")
    return _emp_tables


def employee_tables():
    """{"db.table": [code columns]} for every table holding employee codes."""
    return _schema_index()["tables"]


def normalize_code(q):
    """'ar 623' / '623' / 'AR000623' -> 'AR000623'; None if it is not a code."""
    s = re.sub(r"[\s\-]", "", q).upper()
    if re.fullmatch(r"\d{1,6}", s):
        return "AR" + s.zfill(6)
    m = re.fullmatch(r"([A-Z]{2,4})(\d{1,8})", s)
    if m:
        return m.group(1) + m.group(2).zfill(6)
    return None


def table_counts(code):
    parts = [
        f"SELECT '{src}' s,COUNT(*) n FROM {src} WHERE " + " OR ".join(f"{c}='{code}'" for c in cols)
        for src, cols in employee_tables().items()
    ]
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda b: sql_rows(" UNION ALL ".join(b)), batched(parts)))
    hits = [{"source": r["s"], "rows": int(r["n"])} for rows in results for r in rows if int(r["n"])]
    return sorted(hits, key=lambda h: -h["rows"])


def _u(col):  # tables use different collations; UNION needs them to match
    return f"CONVERT({col} USING utf8mb4)"


def _core_pending(code):
    """Delegation (latest row per task), support tickets and overdue checklist items."""
    sql = (
        f"SELECT 'Delegation' kind,'alcovedb_2024.delegation_tasks' src,{_u('task_id')} id,{_u('task_name')} task,"
        f"{_u('status')} stage,GREATEST(COALESCE(first_complete_date,0),COALESCE(second_complete_date,0),"
        f"COALESCE(third_complete_date,0)) due FROM alcovedb_2024.delegation_tasks d WHERE emp_code='{code}' "
        "AND status IN ('Pending','Revision') AND id=(SELECT MAX(id) FROM alcovedb_2024.delegation_tasks x "
        "WHERE x.task_id=d.task_id) "
        f"UNION ALL SELECT 'Ticket','alcovedb_2024.support_tickets',{_u('id')},{_u('task_name')},{_u('status')},"
        f"COALESCE(new_complete_date,complete_date) FROM alcovedb_2024.support_tickets "
        f"WHERE solver_emp_code='{code}' AND status IN ('Pending','Revision') "
        f"UNION ALL SELECT 'Checklist','alcove_checklist.checklist_data',{_u('id')},{_u('task_name')},"
        f"{_u('frequency')},dates FROM alcove_checklist.checklist_data WHERE EMP_ID='{code}' AND Action IS NULL "
        f"AND dates>='{CHECKLIST_START}' AND dates<=NOW()"
    )
    return sql_rows(sql, limit=1000)


def _fms_part(src, col, code):
    return (f"SELECT 'FMS' kind,'{src}' src,{_u('task_id')} id,{_u('task_name')} task,{_u('current_stage')} stage,"
            f"planned_end_time due FROM {src} WHERE {col}='{code}' AND status IN ('Pending','Returned')")


def _fms_pending(code):
    """FMS stages waiting on this person: the current doer is actual_emp_code / actual_emp_id."""
    parts = [_fms_part(src, doer, code) for src, doer in _schema_index()["fms"].items()]
    with ThreadPoolExecutor(4) as ex:
        results = ex.map(lambda b: sql_rows(" UNION ALL ".join(b), limit=1000), batched(parts))
        return [r for rows in results for r in rows]


def pending_tasks(code):
    with ThreadPoolExecutor(2) as ex:
        core, fms = ex.submit(_core_pending, code), ex.submit(_fms_pending, code)
        rows = core.result() + fms.result()
    now = datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S")
    for r in rows:
        r["due"] = str(r["due"] or "").replace(" ", "T")
        if r["due"].startswith("0000"):
            r["due"] = ""
        r["overdue"] = bool(r["due"]) and r["due"] < now
    rows.sort(key=lambda r: (not r["overdue"], r["due"] or "9999"))
    return rows


_lookup_cache = {}  # (code, personal) -> (time, result); saves the server's hourly query quota
LOOKUP_TTL = 300


def lookup_employee(query, personal=False):
    t0 = time.time()
    query = (query or "").strip()
    code = normalize_code(query)
    hit = _lookup_cache.get((code, personal)) if code else None
    if hit and t0 - hit[0] < LOOKUP_TTL:
        return dict(hit[1], query=query, seconds=0.0, cached=True)
    cols = "*" if personal else ",".join(PROFILE_FIELDS)
    if code:
        people = sql_rows(f"SELECT {cols} FROM alcovedb_2024.employee_master WHERE Emp_Code='{code}'")
    else:
        name = re.sub(r"[^A-Za-z .]", "", query).strip()
        if len(name) < 2:
            return {"found": False, "query": query}
        like = "%" + "%".join(name.split()) + "%"
        people = sql_rows(
            f"SELECT {cols} FROM alcovedb_2024.employee_master WHERE Person_Accountable LIKE '{like}' "
            "ORDER BY STATUS='INACTIVE', Person_Accountable", limit=25)
    if not people:
        return {"found": False, "query": query}
    if len(people) > 1:
        return {"found": True, "multiple": True, "query": query, "matches": [
            {k: p.get(k) for k in ("Emp_Code", "Person_Accountable", "Designation", "Department", "STATUS")}
            for p in people]}
    row = people[0]
    profile = {k: row.get(k) for k in PROFILE_FIELDS}
    with ThreadPoolExecutor(2) as ex:
        sources, pending = ex.submit(table_counts, row["Emp_Code"]), ex.submit(pending_tasks, row["Emp_Code"])
        sources, pending = sources.result(), pending.result()
    result = {
        "found": True, "query": query, "profile": profile, "sources": sources, "pending": pending,
        "tables_searched": len(employee_tables()),
        "databases_searched": len({s.split(".")[0] for s in employee_tables()}),
        "seconds": round(time.time() - t0, 1),
    }
    if personal:  # everything else employee_master returns (the server redacts KYC/pay columns itself)
        result["personal"] = {k: v for k, v in row.items() if k not in PROFILE_FIELDS}
    _lookup_cache[(row["Emp_Code"], personal)] = (time.time(), result)
    return result


def employee_rows(code, source):
    code = normalize_code(code or "")
    cols = employee_tables().get(source)
    if not code or not cols:
        raise ValueError("unknown employee or table")
    where = " OR ".join(f"{c}='{code}'" for c in cols)
    return mcp_call("run_query", database="alcovedb_2024", sql=f"SELECT * FROM {source} WHERE {where}", limit=200)


# ------------------------------------------------- TogetherWecan OAuth ---
# Same flow the Claude connector uses: dynamic client registration, then
# authorization code + PKCE, then refresh tokens.
_auth_lock = threading.Lock()
_pending = {}  # state -> code_verifier


def _load_auth():
    if ALCOVE_AUTH_FILE.exists():
        return json.loads(ALCOVE_AUTH_FILE.read_text(encoding="utf-8"))
    return {}


def _save_auth(a):
    ALCOVE_AUTH_FILE.write_text(json.dumps(a, indent=2), encoding="utf-8")


def _post(url, data, ctype):
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": ctype, "Accept": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TogetherWecan {e.code}: {e.read().decode(errors='replace')}")


def _token_request(fields):
    return _post(ALCOVE_BASE + "/token", urllib.parse.urlencode(fields).encode(),
                 "application/x-www-form-urlencoded")


def _store_tokens(a, tok):
    a["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        a["refresh_token"] = tok["refresh_token"]
    a["expires_at"] = time.time() + int(tok.get("expires_in") or 3600)
    _save_auth(a)


def alcove_login_url():
    with _auth_lock:
        a = _load_auth()
        # a client is registered per callback URL, so moving to a server registers a new one
        if not a.get("client_id") or a.get("client_redirect", "http://127.0.0.1:8000/oauth/callback") != redirect_uri():
            c = _post(ALCOVE_BASE + "/register", json.dumps({
                "client_name": f"{BOT_NAME} Call Bot",
                "redirect_uris": [redirect_uri()],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
                "scope": ALCOVE_SCOPE,
            }).encode(), "application/json")
            a.update(client_id=c["client_id"], client_secret=c.get("client_secret", ""), client_redirect=redirect_uri())
            _save_auth(a)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _pending[state] = verifier
    return ALCOVE_BASE + "/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": a["client_id"],
        "redirect_uri": redirect_uri(),
        "scope": ALCOVE_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": ALCOVE_MCP_URL,
    })


def alcove_finish_login(code, state):
    verifier = _pending.pop(state, None)
    if not verifier:
        raise RuntimeError("Login expired or invalid. Open /oauth/login again.")
    with _auth_lock:
        a = _load_auth()
        _store_tokens(a, _token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": a["client_id"],
            "client_secret": a.get("client_secret", ""),
            "code_verifier": verifier,
            "resource": ALCOVE_MCP_URL,
        }))


def alcove_token():
    """A valid access token, refreshed when close to expiry. None if not connected."""
    with _auth_lock:
        a = _load_auth()
        if not a.get("access_token"):
            return None
        if time.time() < a.get("expires_at", 0) - 120:
            return a["access_token"]
        if not a.get("refresh_token"):
            return None
        try:
            _store_tokens(a, _token_request({
                "grant_type": "refresh_token",
                "refresh_token": a["refresh_token"],
                "client_id": a["client_id"],
                "client_secret": a.get("client_secret", ""),
                "resource": ALCOVE_MCP_URL,
            }))
        except Exception as e:
            print(f"[{BOT_NAME}] TogetherWecan refresh failed, log in again at /oauth/login: {e}")
            return None
        return a["access_token"]


def session_tools():
    token = alcove_token()
    if not token:
        return TOOLS
    return TOOLS + [{
        "type": "mcp",
        "server_label": "alcove",
        "server_url": ALCOVE_MCP_URL,
        "authorization": token,
        "require_approval": "never",
    }]


def session_config():
    return {
        "session": {
            "type": "realtime",
            "model": MODEL,
            "instructions": build_instructions(),
            "audio": {
                "input": {
                    "noise_reduction": {"type": "far_field"},
                    "transcription": {
                        "model": "gpt-4o-mini-transcribe",
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.35,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 700,
                        "create_response": True,
                        "interrupt_response": False,  # finish the current reply, then answer
                    },
                },
                "output": {"voice": VOICE},
            },
            "tools": session_tools(),
            "tool_choice": "auto",
        }
    }


def mint_client_secret():
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY missing. Put it in the .env file next to server.py.")
    req = urllib.request.Request(
        "https://api.openai.com/v1/realtime/client_secrets",
        data=json.dumps(session_config()).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenAI error {e.code}: {e.read().decode(errors='replace')}")


def save_lead(data):
    new = not LEADS_CSV.exists()
    with LEADS_CSV.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["saved_at"] + LEAD_FIELDS)
        w.writerow([datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")] + [str(data.get(k, "")) for k in LEAD_FIELDS])


def save_transcript(data):
    TRANSCRIPTS.mkdir(exist_ok=True)
    who = re.sub(r"[^A-Za-z0-9_-]+", "_", str(data.get("name") or "unknown"))[:40]
    path = TRANSCRIPTS / f"{datetime.now(IST).strftime('%Y%m%d_%H%M%S')}_{who}.txt"
    lines = [f"{m.get('role', '?').upper()}: {m.get('text', '')}" for m in data.get("messages", [])]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path.name


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _authorized(self):
        """When APP_PASSWORD is set (do this on any shared server), ask the browser for it."""
        pw = os.environ.get("APP_PASSWORD")
        if not pw:
            return True
        try:
            sent = base64.b64decode((self.headers.get("Authorization") or "")[6:]).decode().split(":", 1)[1]
        except Exception:
            sent = ""
        if secrets.compare_digest(sent, pw):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{BOT_NAME}"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._authorized():
            return
        if self.path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/health":
            key = os.environ.get("OPENAI_API_KEY", "")
            ok = key.startswith("sk-") and "your-key" not in key
            return self._send(200, {"key_ok": ok, "bot_name": BOT_NAME, "alcove_connected": bool(alcove_token()),
                                    "user_name": current_user()})
        if self.path == "/oauth/login":
            try:
                return self._redirect(alcove_login_url())
            except Exception as e:
                return self._send(502, f"Could not reach TogetherWecan: {e}", "text/plain; charset=utf-8")
        if self.path.startswith("/oauth/callback"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "error" in q:
                return self._send(400, f"Login cancelled: {q['error'][0]}", "text/plain; charset=utf-8")
            try:
                alcove_finish_login(q.get("code", [""])[0], q.get("state", [""])[0])
                print(f"[{BOT_NAME}] TogetherWecan connected")
                return self._redirect("/")
            except Exception as e:
                return self._send(400, f"Login failed: {e}", "text/plain; charset=utf-8")
        if self.path.startswith("/api/employee"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            try:
                if self.path.startswith("/api/employee/rows"):
                    return self._send(200, employee_rows(q.get("code"), q.get("source")))
                result = lookup_employee(q.get("q", ""), personal=q.get("personal") == "1")
                print(f"[{BOT_NAME}] employee lookup '{q.get('q')}': {len(result.get('sources', []))} tables, {len(result.get('pending', []))} pending, {result.get('seconds')}s")
                return self._send(200, result)
            except Exception as e:
                print(f"[{BOT_NAME}] employee lookup error: {e}")
                return self._send(500, {"error": str(e)})
        if self.path == "/api/session":
            try:
                s = mint_client_secret()
                print(f"[{BOT_NAME}] session started")
                return self._send(200, {"client_secret": s["value"], "bot_name": BOT_NAME})
            except Exception as e:
                print(f"[{BOT_NAME}] session error: {e}")
                return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        try:
            data = self._json_body()
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/leads":
            save_lead(data)
            print(f"[{BOT_NAME}] lead saved: {data.get('name')}")
            return self._send(200, {"ok": True})
        if self.path == "/api/transcript":
            name = save_transcript(data)
            print(f"[{BOT_NAME}] transcript saved: {name}")
            return self._send(200, {"ok": True, "file": name})
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass  # keep console clean; important events are printed above


def main():
    load_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("WARNING: OPENAI_API_KEY not set. Copy .env.example to .env and add your key.")
    if not alcove_token():
        print(f"TogetherWecan not connected. Open {redirect_uri().replace('/callback', '/login')} once to log in.")
    if HOST != "127.0.0.1" and not os.environ.get("APP_PASSWORD"):
        print("WARNING: reachable from the network without APP_PASSWORD - anyone with the URL can read employee data.")
    print(f"{BOT_NAME} running -> http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
