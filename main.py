import os
import sys
import httpx
from typing import Optional, Dict, Any
from dotenv import load_dotenv

# Load environment variables from the API project
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "jobito-api", ".env"))
print(f"🔍 Loading environment from: {env_path}")
if os.path.exists(env_path):
    load_dotenv(env_path)
    print("✅ .env file found and loaded.")
else:
    print("⚠️ .env file NOT found in jobito-api folder.")


# Fix Arabic printing on Windows (cp1252 -> utf-8)
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import psycopg2
import psycopg2.extras
from fastapi.responses import StreamingResponse
import json
import base64
import io
from PIL import Image
import fitz # PyMuPDF
import docx

app = FastAPI(title="Jobito AI Chatbot (Local Generative AI)")

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    print("⚠️ DATABASE_URL NOT FOUND IN .ENV!")
else:
    # Fix for psycopg2: remove pgbouncer=true from URI
    if "pgbouncer=true" in DB_URL:
        DB_URL = DB_URL.replace("pgbouncer=true", "").replace("?&", "?").replace("&&", "&").strip("?&")
    print("📡 Database URL loaded successfully.")


NESTJS_MONITORING_URL = os.getenv("NESTJS_MONITORING_URL", "http://localhost:3000/monitoring/log")


async def report_to_bam(message: str, metadata: Dict = None):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(NESTJS_MONITORING_URL, json={
                "message": f"[ChatBot-LocalAI] {message}",
                "metadata": metadata or {}
            }, timeout=1.0)
    except:
        pass

def get_connection():
    return psycopg2.connect(DB_URL, options="-c search_path=ptj,public")


GROQ_API_KEY = "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"
print("✅ تم إعداد Groq API للرد بدلاً من النموذج المحلي.")



def test_connections():
    # PostgreSQL Test
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                print("✅ تم الاتصال بـ PostgreSQL بنجاح.")
    except Exception as e:
        print(f"❌ فشل الاتصال بـ PostgreSQL: {e}")

test_connections()

# ═══════════════════════════════════════════════════════════════════════════════
# CONTEXT RETRIEVAL (RAG) & MEMORY
# ═══════════════════════════════════════════════════════════════════════════════


def get_db_context(user_msg: str) -> str:
    """Intelligently decides what to fetch from DB based on intent."""
    context_parts = []
    
    # 1. Job search intent
    if any(k in user_msg for k in ["وظيفة", "وظائف", "شغل", "اعمل", "job", "work", "career"]):
        context_parts.append(fetch_jobs_context(user_msg))
    
    # 2. Company search intent
    if any(k in user_msg for k in ["شركة", "شركات", "company", "info about"]):
        context_parts.append(fetch_company_context(user_msg))

    # 3. Help/FAQ intent
    if any(k in user_msg for k in ["كيف", "مساعدة", "help", "how to", "مشكلة"]):
        context_parts.append(fetch_help_context(user_msg))

    return "\n".join([p for p in context_parts if p])

def fetch_jobs_context(query: str):
    # Extract potential keywords (handle Arabic better: 3+ chars)
    # Removing common stop words manually for better accuracy
    stop_words = ["اريد", "ابحث", "عن", "ما", "هي", "ممكن", "أين", "في", "على"]
    keywords = [w for w in query.split() if len(w) >= 3 and w not in stop_words]
    
    if not keywords:
        # If no keywords but they asked about jobs generally
        sql = "SELECT title, salary_min, salary_max FROM ptj.jobs WHERE is_active = TRUE ORDER BY created_at DESC LIMIT 3"
        params = ()
    else:
        search_term = f"%{keywords[0]}%"
        sql = """
            SELECT title, salary_min, salary_max 
            FROM ptj.jobs 
            WHERE (title ILIKE %s OR description ILIKE %s) AND is_active = TRUE 
            ORDER BY created_at DESC
            LIMIT 3
        """
        params = (search_term, search_term)
        
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                jobs = cur.fetchall()
                if not jobs: 
                    return "لا توجد وظائف مطابقة تماماً حالياً، لكن يمكنك تصفح الموقع للمزيد."
                res = "الوظائف المتاحة حالياً: "
                res += " | ".join([f"{j['title']} (راتب متوقع: {int(j['salary_min'] or 0)}-{int(j['salary_max'] or 0)})" for j in jobs])
                return res
    except Exception as e: 
        print(f"DB Error (jobs): {e}")
        return ""

def fetch_company_context(query: str):
    # Clean query to find company name
    words = [w for w in query.split() if len(w) >= 3]
    search_term = f"%{words[-1]}%" if words else "%"

    sql = "SELECT name, industry, description FROM ptj.companies WHERE name ILIKE %s OR description ILIKE %s LIMIT 1"
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (search_term, search_term))
                c = cur.fetchone()
                if not c: return ""
                return f"معلومات عن شركة {c['name']} ({c['industry'] or 'غير محدد'}): {c['description'][:150]}..."
    except Exception as e: 
        print(f"DB Error (company): {e}")
        return ""

def fetch_help_context(query: str):
    words = [w for w in query.split() if len(w) >= 3]
    search_term = f"%{words[-1]}%" if words else "%"
    
    sql = "SELECT title, content FROM ptj.help_articles WHERE title ILIKE %s OR content ILIKE %s LIMIT 1"
    try:
        with get_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, (search_term, search_term))
                h = cur.fetchone()
                if not h: return ""
                return f"إليك المساعدة بخصوص {h['title']}: {h['content'][:200]}..."
    except Exception as e: 
        print(f"DB Error (help): {e}")
        return ""

class ChatRequest(BaseModel):
    message: str = None 
    user_id: str = None
    history: list = None
    image: str = None # Base64 image or File content
    file_type: str = "image" # "image", "pdf", "docx"

@app.post("/chat")
async def chat(request: ChatRequest):
    if not request.message and not request.image:
        raise HTTPException(status_code=400, detail="Empty request")

    user_id = request.user_id or "guest"
    user_msg = request.message
    history = request.history or []
    image_data = request.image
    f_type = request.file_type

    # 1. Handle File Processing (PDF/DOCX)
    extracted_text = ""
    pil_image = None

    if image_data:
        try:
            if "base64," in image_data:
                image_data = image_data.split("base64,")[1]
            raw_bytes = base64.b64decode(image_data)
            
            if f_type == "pdf":
                doc = fitz.open(stream=raw_bytes, filetype="pdf")
                for page in doc:
                    extracted_text += page.get_text()
                doc.close()
            elif f_type == "docx":
                doc = docx.Document(io.BytesIO(raw_bytes))
                extracted_text = "\n".join([p.text for p in doc.paragraphs])
            else:
                # Default to Image
                pil_image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        except Exception as e:
            print(f"File process error: {e}")

    # 2. Build Prompt Context
    db_context = ""
    if user_msg:
        db_context = get_db_context(user_msg.lower())

    sys_prompt = "أنت مساعد ذكي لمنصة Jobito. أجِب بأسلوب عربي ودود ومختصر.\n" \
                 "يمكنك تغيير ألوان الواجهة إذا طلب المستخدم ذلك عن طريق كتابة [THEME: color_name] في نهاية ردك.\n" \
                 "الألوان المتاحة: (dark, blue, purple, green, gold)."

    if db_context:
        sys_prompt += f"\n\nمعلومات إضافية للمساعدة في الإجابة:\n{db_context}"

    async def generate_chunks():
        messages = [{"role": "system", "content": sys_prompt}]
        for msg in history[-5:]:
            role = "assistant" if msg.get("role") == "assistant" else "user"
            messages.append({"role": role, "content": msg.get("content", "")})
        
        user_content = user_msg or ""
        if extracted_text:
            user_content += f"\n\n[محتوى الملف المستخرج]:\n{extracted_text}"
            
        messages.append({"role": "user", "content": user_content})

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": messages,
                        "stream": True,
                        "temperature": 0.6,
                        "max_completion_tokens": 1024
                    },
                    timeout=30.0
                )
                
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            if "choices" in data and len(data["choices"]) > 0:
                                delta = data["choices"][0].get("delta", {})
                                if "content" in delta and delta["content"]:
                                    yield f"data: {json.dumps({'text': delta['content']})}\n\n"
                        except Exception:
                            pass
                            
                yield "data: [DONE]\n\n"
        except Exception as e:
            print(f"CRITICAL: Unexpected error in generate_chunks: {e}")
            yield f"data: {json.dumps({'text': '⚠️ عذراً، حدث خطأ أثناء الاتصال بـ Groq.'})}\n\n"
            yield "data: [DONE]\n\n"


    return StreamingResponse(generate_chunks(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    # Use multiple workers carefully; loading LLM in memory per worker requires significant RAM
    uvicorn.run(app, host="0.0.0.0", port=5000)
