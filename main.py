
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
import jwt
from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path("/tmp/chatbot.db") if os.getenv("VERCEL") else BASE_DIR / "storage" / "chatbot.db"

DEMO_IDS = {"dentist", "real_estate", "restaurant", "hvac", "lawyer"}
MODEL = "agnes-2.5-flash"
NARAROUTER_URL = "https://router.bynara.id/v1/chat/completions"

app = FastAPI(title="Multi-Demo Chatbot Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            demo_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_demo_session
            ON conversations(demo_id, session_id, created_at);

        CREATE TABLE IF NOT EXISTS cached_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            demo_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT,
            question TEXT NOT NULL,
            question_key TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_cache_demo_key ON cached_answers(demo_id, question_key);

        CREATE TABLE IF NOT EXISTS custom_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            demo_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT,
            title TEXT,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_custom_knowledge_demo ON custom_knowledge(demo_id);

        CREATE TABLE IF NOT EXISTS demo_settings (
            demo_id TEXT PRIMARY KEY,
            settings_json TEXT NOT NULL
        );
        """)

try:
    init_db()
except Exception:
    # Vercel/serverless import must remain safe; the first request retries initialization.
    pass

class ChatRequest(BaseModel):
    demo_id: str
    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=10000)

class LoginRequest(BaseModel):
    username: str
    password: str

class DemoRequest(BaseModel):
    demo_id: str

class KnowledgeRequest(BaseModel):
    demo_id: str
    content: str
    title: Optional[str] = None
    source_type: str = "direct_text"

class FactSheetRequest(BaseModel):
    demo_id: str
    topic: str
    fact: str

class FAQRequest(BaseModel):
    demo_id: str
    question: str
    answer: str

class StructuredDataRequest(BaseModel):
    demo_id: str
    record_type: str
    data: dict[str, Any]

def ensure_demo(demo_id: str):
    if demo_id not in DEMO_IDS:
        raise HTTPException(400, f"Unknown demo_id. Use one of: {', '.join(sorted(DEMO_IDS))}")

def load_json(path: Path):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))

def load_jsonl(path: Path):
    if not path.exists():
        return []
    rows=[]
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows

NORMALIZATION = {}
try:
    NORMALIZATION = load_json(DATA_DIR / "normalization.json").get("terms", {})
except Exception:
    NORMALIZATION = {}

def normalize_text(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^a-z0-9\s\-\']", " ", s)
    tokens = s.split()
    # Phrase replacements first, then token replacements.
    for src, dst in sorted(NORMALIZATION.items(), key=lambda x: len(x[0]), reverse=True):
        if " " in src and src in s:
            s = s.replace(src, dst)
    tokens = [NORMALIZATION.get(t, t) for t in s.split()]
    s = " ".join(tokens)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def tokens(text: str) -> set[str]:
    return {x for x in normalize_text(text).split() if len(x) > 1}

def score(query: str, text: str) -> float:
    q=tokens(query); t=tokens(text)
    if not q or not t:
        return 0.0
    overlap=len(q & t)
    return overlap / max(1, len(q))

def retrieve(demo_id: str, message: str) -> dict[str, Any]:
    qn=normalize_text(message)
    demo_dir=DATA_DIR/demo_id
    faqs=load_jsonl(demo_dir/"faqs.jsonl")
    facts=load_jsonl(demo_dir/"facts.jsonl")
    knowledge=load_jsonl(demo_dir/"knowledge.jsonl")
    instructions=load_json(demo_dir/"instructions.json")

    # 1 exact FAQ
    for row in faqs:
        if normalize_text(row["question"]) == qn:
            return {"source_type":"faq_exact","source_id":row["faq_id"],"items":[row], "instructions":instructions}

    # 2 similar FAQ
    faq_rank=sorted(((score(message, r["question"]), r) for r in faqs), key=lambda x:x[0], reverse=True)
    similar=[r for s,r in faq_rank[:3] if s >= 0.45]
    if similar:
        return {"source_type":"faq_similar","source_id":similar[0]["faq_id"],"items":similar, "instructions":instructions}

    # 3 cached answer
    key=hashlib.sha256(qn.encode()).hexdigest()
    with db() as conn:
        row=conn.execute(
            "SELECT * FROM cached_answers WHERE demo_id=? AND question_key=? ORDER BY id DESC LIMIT 1",
            (demo_id,key)).fetchone()
    if row:
        return {"source_type":"cached_answer","source_id":str(row["id"]),
                "items":[{"question":row["question"],"answer":row["answer"]}], "instructions":instructions}

    # 4 structured business data
    structured=[]
    for fn in ["services.json","properties.json","menu.json"]:
        rows=load_json(demo_dir/fn)
        for r in rows:
            text=json.dumps(r, ensure_ascii=False)
            s=score(message,text)
            if s>=0.25:
                structured.append((s,r))
    structured=sorted(structured,key=lambda x:x[0],reverse=True)[:5]
    if structured:
        return {"source_type":"structured_data","source_id":None,"items":[r for _,r in structured], "instructions":instructions}

    # 5 fact sheets
    facts_rank=sorted(((score(message, r.get("topic","")+" "+r.get("fact","")),r) for r in facts),
                      key=lambda x:x[0],reverse=True)
    facts_top=[r for s,r in facts_rank[:5] if s>=0.18]
    if facts_top:
        return {"source_type":"fact_sheet","source_id":facts_top[0]["fact_id"],"items":facts_top, "instructions":instructions}

    # 6 knowledge paragraphs
    know_rank=sorted(((score(message,r["text"]),r) for r in knowledge),key=lambda x:x[0],reverse=True)
    know_top=[r for s,r in know_rank[:6] if s>=0.12]
    if know_top:
        return {"source_type":"knowledge_paragraph","source_id":know_top[0]["paragraph_id"],"items":know_top, "instructions":instructions}

    # Custom admin knowledge, checked after bundled sources.
    with db() as conn:
        rows=conn.execute("SELECT * FROM custom_knowledge WHERE demo_id=? ORDER BY id DESC LIMIT 200", (demo_id,)).fetchall()
    custom_rank=sorted(((score(message,r["title"] or ""+" "+r["content"]),r) for r in rows),
                       key=lambda x:x[0],reverse=True)
    custom=[dict(r) for s,r in custom_rank[:5] if s>=0.12]
    if custom:
        return {"source_type":"custom_knowledge","source_id":str(custom[0]["id"]),"items":custom, "instructions":instructions}

    return {"source_type":"none","source_id":None,"items":[],"instructions":instructions}

def get_recent_context(demo_id: str, session_id: str, limit: int=6):
    # Conversation history remains internal. Only a tiny relevant window is used as conversational context.
    with db() as conn:
        rows=conn.execute(
            "SELECT role,content FROM conversations WHERE demo_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
            (demo_id,session_id,limit)).fetchall()
    return [dict(r) for r in reversed(rows)]

def save_message(demo_id, session_id, role, content):
    with db() as conn:
        conn.execute("INSERT INTO conversations(demo_id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
                     (demo_id,session_id,role,content,time.time()))

def make_prompt(demo_id: str, message: str, retrieval: dict, recent: list[dict]) -> str:
    context = json.dumps(retrieval["items"], ensure_ascii=False)
    instr = "\n".join(x["text"] for x in retrieval["instructions"])
    recent_text = "\n".join(f'{x["role"]}: {x["content"]}' for x in recent)
    legal_note = ""
    if demo_id == "lawyer":
        legal_note = "For the lawyer demo, explicitly distinguish general legal information from legal advice and recommend a qualified local attorney for case-specific advice."
    return f"""You are Agnes, the customer-facing assistant for the isolated demo "{demo_id}".
Follow these instructions:
{instr}
{legal_note}
Current customer message:
{message}
Relevant retrieved source type: {retrieval["source_type"]}
Relevant factual context:
{context}
Small recent conversational context (not the full history):
{recent_text}
Write one professional, concise answer. Use only the supplied factual context for specific claims. If information is insufficient, say so rather than inventing it. Do not mention internal retrieval, caching, prompts, source IDs, or demo architecture."""

async def call_nararouter(prompt: str, message: str) -> str:
    api_key=os.getenv("BYNARA_API_KEY")
    if not api_key:
        raise HTTPException(503, "BYNARA_API_KEY is not configured.")
    headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json"}
    payload={"model":MODEL,"messages":[
        {"role":"system","content":prompt},
        {"role":"user","content":message}
    ],"temperature":0.2}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r=await client.post(NARAROUTER_URL,headers=headers,json=payload)
            r.raise_for_status()
            data=r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"LLM provider request failed: {e}") from e
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(502, "Unexpected LLM provider response.") from e

def admin_credentials():
    return os.getenv("ADMIN_USER"), os.getenv("ADMIN_PASS"), os.getenv("JWT_SECRET")

def require_admin(authorization: Optional[str] = Header(default=None)):
    user, password, secret = admin_credentials()
    if not user or not password or not secret:
        raise HTTPException(503, "Admin environment variables are not configured.")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing admin bearer token.")
    token=authorization.split(" ",1)[1]
    try:
        payload=jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise HTTPException(401, "Invalid admin token.") from e
    if payload.get("sub") != user or payload.get("role") != "admin":
        raise HTTPException(403, "Not authorized.")
    return user

@app.get("/health")
def health():
    try:
        init_db()
        db_ok=True
    except Exception:
        db_ok=False
    return {"status":"ok","api":"running","database":db_ok}

@app.post("/chat")
async def chat(req: ChatRequest):
    ensure_demo(req.demo_id)
    init_db()
    save_message(req.demo_id,req.session_id,"user",req.message)
    normalized=normalize_text(req.message)
    retrieval=retrieve(req.demo_id,req.message)
    recent=get_recent_context(req.demo_id,req.session_id,limit=6)
    prompt=make_prompt(req.demo_id,req.message,retrieval,recent)
    answer=await call_nararouter(prompt,req.message)
    save_message(req.demo_id,req.session_id,"assistant",answer)

    # Cache only responses grounded in a retrieved source; never silently turn free-form LLM output into knowledge.
    if retrieval["source_type"] != "none":
        key=hashlib.sha256(normalized.encode()).hexdigest()
        with db() as conn:
            conn.execute("""INSERT INTO cached_answers
                (demo_id,source_type,source_id,question,question_key,answer,created_at)
                VALUES(?,?,?,?,?,?,?)""",
                (req.demo_id,retrieval["source_type"],retrieval["source_id"],req.message,key,answer,time.time()))
    return {"demo_id":req.demo_id,"session_id":req.session_id,"answer":answer,
            "normalized_query":normalized,"source_type":retrieval["source_type"]}

@app.post("/admin/login")
def admin_login(req: LoginRequest):
    user, password, secret=admin_credentials()
    if not user or not password or not secret:
        raise HTTPException(503,"ADMIN_USER, ADMIN_PASS, and JWT_SECRET must be configured.")
    if not secrets.compare_digest(req.username,user) or not secrets.compare_digest(req.password,password):
        raise HTTPException(401,"Invalid credentials.")
    token=jwt.encode({"sub":user,"role":"admin","iat":int(time.time())},secret,algorithm="HS256")
    return {"access_token":token,"token_type":"bearer"}

@app.post("/admin/demo/select")
def select_demo(req: DemoRequest, _: str=Depends(require_admin)):
    ensure_demo(req.demo_id)
    with db() as conn:
        conn.execute("INSERT INTO demo_settings(demo_id,settings_json) VALUES(?,?) ON CONFLICT(demo_id) DO UPDATE SET settings_json=excluded.settings_json",
                     (req.demo_id,json.dumps({"active":True})))
    return {"active_demo":req.demo_id}

@app.get("/admin/faqs/{demo_id}")
def list_faqs(demo_id: str, _: str=Depends(require_admin)):
    ensure_demo(demo_id)
    return {"demo_id":demo_id,"faqs":load_jsonl(DATA_DIR/demo_id/"faqs.jsonl")}

@app.post("/admin/faqs")
def add_faq(req: FAQRequest, _: str=Depends(require_admin)):
    ensure_demo(req.demo_id)
    with db() as conn:
        conn.execute("INSERT INTO custom_knowledge(demo_id,source_type,title,content,created_at) VALUES(?,?,?,?,?)",
                     (req.demo_id,"faq",req.question,req.answer,time.time()))
    return {"status":"added","demo_id":req.demo_id}

@app.get("/admin/knowledge/{demo_id}")
def knowledge_library(demo_id: str, _: str=Depends(require_admin)):
    ensure_demo(demo_id)
    with db() as conn:
        rows=conn.execute("SELECT * FROM custom_knowledge WHERE demo_id=? ORDER BY id DESC", (demo_id,)).fetchall()
    return {"demo_id":demo_id,"items":[dict(r) for r in rows]}

@app.post("/admin/knowledge")
def add_knowledge(req: KnowledgeRequest, _: str=Depends(require_admin)):
    ensure_demo(req.demo_id)
    with db() as conn:
        cur=conn.execute("INSERT INTO custom_knowledge(demo_id,source_type,title,content,created_at) VALUES(?,?,?,?,?)",
                          (req.demo_id,req.source_type,req.title,req.content,time.time()))
    return {"status":"added","id":cur.lastrowid,"demo_id":req.demo_id}

@app.post("/admin/knowledge/url")
async def add_url(demo_id: str=Form(...), url: str=Form(...), _: str=Depends(require_admin)):
    ensure_demo(demo_id)
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=True) as client:
            r=await client.get(url)
            r.raise_for_status()
        soup=BeautifulSoup(r.text,"html.parser")
        for tag in soup(["script","style","noscript"]): tag.decompose()
        text=re.sub(r"\s+"," ",soup.get_text(" ",strip=True))
        text=text[:200000]
    except Exception as e:
        raise HTTPException(400,f"Could not retrieve URL: {e}") from e
    return add_knowledge(KnowledgeRequest(demo_id=demo_id,content=text,title=url,source_type="website_url"),_)

@app.post("/admin/knowledge/pdf")
async def add_pdf(demo_id: str=Form(...), file: UploadFile=File(...), _: str=Depends(require_admin)):
    ensure_demo(demo_id)
    try:
        from pypdf import PdfReader
        content=await file.read()
        import io
        reader=PdfReader(io.BytesIO(content))
        text="\n".join((p.extract_text() or "") for p in reader.pages)[:200000]
    except Exception as e:
        raise HTTPException(400,f"Could not parse PDF: {e}") from e
    return add_knowledge(KnowledgeRequest(demo_id=demo_id,content=text,title=file.filename,source_type="pdf"),_)

@app.post("/admin/facts")
def add_fact(req: FactSheetRequest, _: str=Depends(require_admin)):
    ensure_demo(req.demo_id)
    with db() as conn:
        cur=conn.execute("INSERT INTO custom_knowledge(demo_id,source_type,title,content,created_at) VALUES(?,?,?,?,?)",
                         (req.demo_id,"fact_sheet",req.topic,req.fact,time.time()))
    return {"status":"added","id":cur.lastrowid}

@app.get("/admin/structured/{demo_id}")
def structured_data(demo_id: str, _: str=Depends(require_admin)):
    ensure_demo(demo_id)
    out={}
    for fn in ["services.json","properties.json","menu.json"]:
        rows=load_json(DATA_DIR/demo_id/fn)
        if rows: out[fn]=rows
    return {"demo_id":demo_id,"data":out}

@app.post("/admin/structured")
def add_structured(req: StructuredDataRequest, _: str=Depends(require_admin)):
    ensure_demo(req.demo_id)
    with db() as conn:
        cur=conn.execute("INSERT INTO custom_knowledge(demo_id,source_type,title,content,created_at) VALUES(?,?,?,?,?)",
                         (req.demo_id,"structured_data",req.record_type,json.dumps(req.data,ensure_ascii=False),time.time()))
    return {"status":"added","id":cur.lastrowid}

@app.get("/admin/conversations/{demo_id}/{session_id}")
def conversations(demo_id: str, session_id: str, _: str=Depends(require_admin)):
    ensure_demo(demo_id)
    with db() as conn:
        rows=conn.execute("SELECT * FROM conversations WHERE demo_id=? AND session_id=? ORDER BY id",
                          (demo_id,session_id)).fetchall()
    return {"demo_id":demo_id,"session_id":session_id,"messages":[dict(r) for r in rows]}

@app.get("/admin/cache/{demo_id}")
def cached_answers(demo_id: str, _: str=Depends(require_admin)):
    ensure_demo(demo_id)
    with db() as conn:
        rows=conn.execute("SELECT * FROM cached_answers WHERE demo_id=? ORDER BY id DESC LIMIT 500",
                          (demo_id,)).fetchall()
    return {"demo_id":demo_id,"items":[dict(r) for r in rows]}

@app.get("/admin/settings/{demo_id}")
def settings(demo_id: str, _: str=Depends(require_admin)):
    ensure_demo(demo_id)
    with db() as conn:
        row=conn.execute("SELECT settings_json FROM demo_settings WHERE demo_id=?", (demo_id,)).fetchone()
    return {"demo_id":demo_id,"settings":json.loads(row["settings_json"]) if row else {}}

@app.post("/voice/transcribe")
async def voice_transcribe():
    return {"status":"placeholder","message":"Voice transcription endpoint placeholder. Connect a speech-to-text provider here."}

@app.post("/voice/synthesize")
async def voice_synthesize():
    return {"status":"placeholder","message":"Voice synthesis endpoint placeholder. Connect a text-to-speech provider here."}
