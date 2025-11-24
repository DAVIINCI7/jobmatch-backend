from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import io
import re
import requests

from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document

# ============================================================
# CONFIG BACKEND
# ============================================================

# 👉 À REMPLACER avec ta vraie clé d'API d'un agrégateur de jobs
JOB_API_KEY = "YOUR_API_KEY"

# 👉 Exemple d'endpoint d'un agrégateur (à adapter à ton API réelle)
JOB_API_URL = "https://example-job-aggregator.com/search"  # TODO: mettre l'URL réelle

# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="JobMatch Assistant Pro",
    description="Analyse un CV et récupère des offres sur plusieurs sites d'emploi.",
    version="6.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # plus tard: restreindre à ton domaine Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# MODELES
# ============================================================

class JobOffer(BaseModel):
    title: str
    company: str
    location: str
    url: str
    source: str           # Indeed / LinkedIn / Glassdoor / ...
    match_score: float    # 0.0 -> 1.0
    snippet: str
    published_at: str | None   # ex: "il y a 2 jours" ou "2025-11-23"
    is_paid: bool
    salary_text: str | None


class ContactRequest(BaseModel):
    job_title: str
    company: str
    hr_email: str
    candidate_name: str
    candidate_email: str
    cv_summary: str


# ============================================================
# UTILS CV
# ============================================================

STOPWORDS = {
    "je", "nous", "vous", "ils", "elles", "le", "la", "les", "des", "de", "du", "un", "une",
    "et", "ou", "mais", "dans", "sur", "avec", "pour", "par", "mon", "ma", "mes", "ton",
    "ta", "tes", "son", "sa", "ses", "notre", "vos", "leur", "leurs",
    "the", "a", "an", "of", "in", "on", "at", "for", "to", "from", "by", "and", "or",
    "this", "that", "these", "those"
}


def extract_text_from_upload(upload: UploadFile) -> str:
    """
    Lis le CV (PDF / DOCX / DOC / TXT) et renvoie le texte brut.
    """
    content = upload.file.read()
    filename = (upload.filename or "").lower()

    # PDF
    if filename.endswith(".pdf"):
        try:
            return pdf_extract_text(io.BytesIO(content))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erreur lecture PDF: {str(e)}")

    # DOCX
    if filename.endswith(".docx"):
        try:
            file_like = io.BytesIO(content)
            document = Document(file_like)
            return "\n".join(p.text for p in document.paragraphs)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erreur lecture DOCX: {str(e)}")

    # Autres (DOC, TXT, etc.)
    try:
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return content.decode(errors="ignore")


def extract_profile(cv_text: str) -> dict:
    """
    Extrait un "profil" générique :
    - mots-clés fréquents
    - tentative de titre de poste
    """
    text = cv_text

    # 1) Mots-clés
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{4,}", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w in STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1

    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    top_keywords = [w for w, c in sorted_words[:20]]

    # 2) Titre(s)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title_candidates: list[str] = []
    title_keywords = [
        "technicien", "technicienne", "agent", "agente", "analyste",
        "ingénieur", "ingenieur", "développeur", "developpeur", "développeuse",
        "assistant", "assistante", "conseiller", "conseillère",
        "représentant", "representant", "superviseur", "gestionnaire",
        "préparateur", "préparatrice", "caissier", "caissière",
        "vendeur", "vendeuse", "chauffeur", "livreur", "magasinier",
        "comptable", "infirmier", "infirmière", "administrateur",
        "administratrice", "coordonnateur", "coordonnatrice",
        "support", "informatique", "logistique", "banque", "financier"
    ]

    for line in lines:
        lower = line.lower()
        if any(k in lower for k in title_keywords):
            if 4 <= len(line) <= 120:
                title_candidates.append(line)

    titles = list(dict.fromkeys(title_candidates))

    if not titles:
        if len(top_keywords) >= 2:
            main_title = f"{top_keywords[0].capitalize()} / {top_keywords[1].capitalize()}"
        elif top_keywords:
            main_title = top_keywords[0].capitalize()
        else:
            main_title = "Profil expérimenté"
        titles = [main_title]

    return {"titles": titles, "keywords": top_keywords}


def build_search_query(profile: dict) -> str:
    """
    Construit UNE requête globale à partir du titre + quelques mots-clés.
    (On préfère UNE requête bien dense pour l'API d'agrégation.)
    """
    title = profile["titles"][0] if profile["titles"] else "emploi"
    words = profile["keywords"][:5]
    base = "+".join(title.lower().split())
    extra = "+".join(words) if words else ""
    if extra:
        return f"{base}+{extra}"
    return base


# ============================================================
# FETCH JOBS VIA AGRÉGATEUR
# ============================================================

def fetch_jobs_from_aggregator(query: str, limit: int = 30) -> List[JobOffer]:
    """
    Appelle un agrégateur d'offres (multi-sites : Indeed, LinkedIn, Glassdoor, etc.)
    et renvoie une liste d'offres normalisées.
    ⚠ Tu dois adapter cette fonction au format réel de l'API que tu vas utiliser.
    """
    headers = {
        "User-Agent": "JobMatchAssistant/1.0",
        "Accept": "application/json",
        # Ex: pour RapidAPI : "X-RapidAPI-Key": JOB_API_KEY,
        #                     "X-RapidAPI-Host": "XXX.p.rapidapi.com",
    }

    params = {
        "q": query,       # à adapter selon la doc de l'API
        "country": "CA",  # cible Canada
        "page": 1,
        "limit": limit,
    }

    try:
        resp = requests.get(JOB_API_URL, headers=headers, params=params, timeout=15)
    except Exception as e:
        print("Erreur réseau job API:", e)
        return []

    if resp.status_code != 200:
        print("Statut HTTP non 200 job API:", resp.status_code, resp.text[:200])
        return []

    try:
        data = resp.json()
    except Exception as e:
        print("Erreur parse JSON job API:", e)
        return []

    offers: List[JobOffer] = []

    # ⚠ ADAPTER CE MAPPING à la structure réelle du JSON renvoyé par ton API.
    # Ici j'imagine un format type :
    # {
    #   "jobs": [
    #     {
    #       "title": "...",
    #       "company": "...",
    #       "location": "...",
    #       "description": "...",
    #       "url": "...",
    #       "source": "indeed",
    #       "posted_at": "...",
    #       "salary": "...",
    #     }, ...
    #   ]
    # }
    jobs = data.get("jobs") or data.get("data") or []
    for j in jobs[:limit]:
        title = j.get("title") or "Titre non disponible"
        company = j.get("company") or j.get("employer_name") or "Employeur non précisé"
        location = j.get("location") or j.get("city") or "Lieu non précisé"
        url = j.get("url") or j.get("job_url") or ""
        source = j.get("source") or j.get("job_platform") or "Inconnu"
        snippet = j.get("description") or j.get("snippet") or ""
        posted = j.get("posted_at") or j.get("date_posted") or None
        salary_text = j.get("salary") or j.get("salary_text") or None

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=url,
            source=source.capitalize(),
            match_score=0.8,    # ajustable
            snippet=snippet[:300],
            published_at=posted,
            is_paid=True,
            salary_text=salary_text,
        ))

    return offers


def enrich_scores(offers: List[JobOffer], profile: dict) -> List[JobOffer]:
    """
    Améliore un peu le score des offres qui contiennent les mots-clés du CV.
    """
    keywords = set(profile["keywords"])

    for o in offers:
        txt = (o.title + " " + o.snippet).lower()
        bonus = 0.0
        for kw in keywords:
            if kw and kw in txt:
                bonus += 0.01
        o.match_score = min(0.99, round(o.match_score + bonus, 2))

    offers.sort(key=lambda x: o.match_score, reverse=True)
    return offers


# ============================================================
# ENDPOINTS
# ============================================================

@app.post("/api/match", response_model=List[JobOffer])
async def match_jobs(
    cv: UploadFile = File(...),
    recent_minutes: int = 0,
    only_paid: bool = False,
):
    """
    Analyse le CV et retourne une liste d'offres d'emploi (multi-plateformes)
    via un agrégateur d'offres.
    """
    if not cv.filename:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu.")

    cv_text = extract_text_from_upload(cv)
    if not cv_text or len(cv_text.strip()) < 30:
        raise HTTPException(status_code=400, detail="Impossible de lire le contenu du CV.")

    profile = extract_profile(cv_text)
    query = build_search_query(profile)

    offers = fetch_jobs_from_aggregator(query, limit=30)

    if not offers:
        # fallback très simple : au cas où l'API renvoie rien
        offers = []

    # filtrage salaire si tu veux (pour only_paid) → à adapter à ton API
    if only_paid:
        offers = [o for o in offers if o.salary_text]

    # on garde max 30
    return offers[:30]


@app.post("/api/contact-hr")
async def contact_hr(req: ContactRequest):
    """
    Génère un email à envoyer à HR.
    """
    if not req.hr_email:
        req.hr_email = "recrutement@" + req.company.replace(" ", "").lower() + ".com"

    email_body = f"""
Bonjour,

Je vous contacte concernant le poste « {req.job_title} » au sein de {req.company}.

Mon profil et mon expérience sont étroitement liés à ce type de poste, comme détaillé dans mon CV.

Résumé rapide de mon profil :
{req.cv_summary}

Je serais ravi d'échanger avec vous pour discuter de ma candidature.

Cordialement,
{req.candidate_name}
{req.candidate_email}
""".strip()

    return {
        "to": req.hr_email,
        "subject": f"Candidature - {req.job_title} - {req.candidate_name}",
        "body": email_body
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
