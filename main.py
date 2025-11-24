from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import io
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document

API_KEY = "60299ec3b7mshaaff2aec49fb6b7p114bafjsn07c887579f76"   # ← METS TA CLÉ ICI
API_HOST = "jsearch.p.rapidapi.com"

app = FastAPI(
    title="JobMatch Assistant PRO",
    description="Analyse ton CV + recherches sur toutes les plateformes d’emploi.",
    version="6.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def extract_text(upload: UploadFile) -> str:
    content = upload.file.read()
    filename = upload.filename.lower()

    if filename.endswith(".pdf"):
        return pdf_extract_text(io.BytesIO(content))

    if filename.endswith(".docx"):
        doc = Document(io.BytesIO(content))
        return "\n".join([p.text for p in doc.paragraphs])

    try:
        return content.decode("utf-8", errors="ignore")
    except:
        return content.decode(errors="ignore")


@app.post("/api/match")
async def match_jobs(cv: UploadFile = File(...)):
    text = extract_text(cv)

    if len(text.strip()) < 20:
        raise HTTPException(status_code=400, detail="CV vide ou illisible.")

    # Utiliser les premiers mots du CV comme mots-clés
    words = [w for w in text.split() if len(w) > 3]
    query = " ".join(words[:5])  # mots clés pour la recherche

    url = "https://jsearch.p.rapidapi.com/search"

    headers = {
        "X-RapidAPI-Key": API_KEY,
        "X-RapidAPI-Host": API_HOST
    }

    params = {
        "query": query,
        "num_pages": "1"
    }

    response = requests.get(url, headers=headers, params=params)

    if response.status_code != 200:
        return []

    data = response.json().get("data", [])

    results = []
    for job in data:
        results.append({
            "title": job.get("job_title"),
            "company": job.get("employer_name"),
            "location": job.get("job_city"),
            "description": job.get("job_description"),
            "url": job.get("job_apply_link"),
            "source": job.get("job_posted_at"),
        })

    return results[:20]  # 20 offres minimum
