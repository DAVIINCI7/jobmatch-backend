from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import io
import re
from datetime import datetime, timezone

from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document
import requests
from bs4 import BeautifulSoup

app = FastAPI(
    title="JobMatch Assistant",
    description="Analyse ton CV et trouve de vraies offres Indeed adaptées à ton profil.",
    version="4.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # plus tard: remplace par ton URL Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class JobOffer(BaseModel):
    title: str
    company: str
    location: str
    url: str
    source: str
    match_score: float
    snippet: str
    published_at: str | None  # texte brut, ex: "il y a 2 jours"
    is_paid: bool
    salary_text: str | None


class ContactRequest(BaseModel):
    job_title: str
    company: str
    hr_email: str
    candidate_name: str
    candidate_email: str
    cv_summary: str


# ----------------- Utils CV -----------------

STOPWORDS = {
    "je", "nous", "vous", "ils", "elles", "le", "la", "les", "des", "de", "du", "un", "une",
    "et", "ou", "mais", "dans", "sur", "avec", "pour", "par", "mon", "ma", "mes", "ton",
    "ta", "tes", "son", "sa", "ses", "notre", "vos", "leur", "leurs",
    "the", "a", "an", "of", "in", "on", "at", "for", "to", "from", "by", "and", "or",
    "this", "that", "these", "those"
}


def extract_text_from_upload(upload: UploadFile) -> str:
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

    # Autres (DOC, TXT, etc.) -> tentative de décodage
    try:
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return content.decode(errors="ignore")


def extract_profile(cv_text: str) -> dict:
    """
    Analyse générique : on récupère des mots-clés fréquents + on devine un "titre"
    à partir des lignes les plus typiques d'un CV.
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

    # 2) Titre(s) possible(s) depuis les lignes
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
        "administratrice", "coordonnateur", "coordonnatrice"
    ]

    for line in lines:
        lower = line.lower()
        if any(k in lower for k in title_keywords):
            if 4 <= len(line) <= 120:
                title_candidates.append(line)

    titles = list(dict.fromkeys(title_candidates))

    # Si vraiment on n'a pas trouvé de titre, on construit un titre à partir des mots-clés
    if not titles:
        if len(top_keywords) >= 2:
            main_title = f"{top_keywords[0].capitalize()} / {top_keywords[1].capitalize()}"
        elif top_keywords:
            main_title = top_keywords[0].capitalize()
        else:
            main_title = "Profil expérimenté"
        titles = [main_title]

    return {"titles": titles, "keywords": top_keywords}


def build_search_queries(profile: dict) -> List[str]:
    """
    Construit des requêtes Indeed à partir des titres + mots-clés.
    """
    titles = profile["titles"]
    keywords = profile["keywords"]

    queries: List[str] = []

    for title in titles[:3]:  # on prend max 3 titres
        base = "+".join(title.lower().split())
        extra = "+".join(keywords[:3]) if keywords else ""
        if extra:
            queries.append(f"{base}+{extra}")
        else:
            queries.append(base)

    if not queries:
        queries = ["emploi+canada"]

    return queries


# ----------------- Scraping Indeed -----------------

def fetch_indeed_jobs(query: str, max_results: int = 10) -> List[JobOffer]:
    """
    Va chercher de vraies offres sur Indeed (Canada) pour la requête donnée.
    On parse le HTML : titre, entreprise, lieu, extrait, date relative.
    """
    url = f"https://ca.indeed.com/jobs?q={query}&sort=date"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        print("Erreur réseau Indeed:", e)
        return []

    if resp.status_code != 200:
        print("Statut HTTP non 200 pour Indeed:", resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("a.tapItem")

    offers: List[JobOffer] = []

    for card in cards[:max_results]:
        # Titre
        title_el = card.select_one("h2.jobTitle span")
        if not title_el:
            title_el = card.select_one("h2.jobTitle")
        title = title_el.get_text(strip=True) if title_el else "Titre non disponible"

        # Entreprise
        company_el = card.select_one("span.companyName")
        company = company_el.get_text(strip=True) if company_el else "Employeur non précisé"

        # Lieu
        loc_el = card.select_one("div.companyLocation")
        location = loc_el.get_text(strip=True) if loc_el else "Lieu non précisé"

        # Snippet
        snippet_el = card.select_one("div.job-snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else "Description non disponible."

        # Date relative (ex: "il y a 2 jours")
        date_el = card.select_one("span.date")
        date_txt = date_el.get_text(strip=True) if date_el else None

        # Salaire si dispo
        salary_el = card.select_one("div.metadata.salary-snippet-container")
        salary_text = salary_el.get_text(" ", strip=True) if salary_el else None

        # URL de l'offre
        href = card.get("href", "")
        job_url = href
        if job_url.startswith("/"):
            job_url = "https://ca.indeed.com" + job_url

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=job_url,
            source="Indeed",
            match_score=0.9,
            snippet=snippet,
            published_at=date_txt,
            is_paid=True,  # on suppose payé, à vérifier dans l'annonce
            salary_text=salary_text,
        ))

    return offers


def enrich_scores(offers: List[JobOffer], profile: dict) -> List[JobOffer]:
    """
    On booste un peu les offres qui contiennent des mots-clés du CV dans le titre/snippet.
    """
    keywords = set(profile["keywords"])

    for o in offers:
        txt = (o.title + " " + o.snippet).lower()
        bonus = 0.0
        for kw in keywords:
            if kw in txt:
                bonus += 0.01
        o.match_score = min(0.99, round(o.match_score + bonus, 2))

    offers.sort(key=lambda x: x.match_score, reverse=True)
    return offers


# ----------------- Endpoints -----------------

@app.post("/api/match", response_model=List[JobOffer])
async def match_jobs(
    cv: UploadFile = File(...),
    recent_minutes: int = 0,
    only_paid: bool = False   # gardé pour compat UI
):
    """
    1. Lit le CV (PDF / DOCX / autres)
    2. Extrait un "profil" (titres + mots-clés)
    3. Construit plusieurs requêtes Indeed
    4. Retourne de vraies offres Indeed avec leur titre / employeur / lieu / date
    """
    if not cv.filename:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu.")

    cv_text = extract_text_from_upload(cv)
    if not cv_text or len(cv_text.strip()) < 30:
        raise HTTPException(status_code=400, detail="Impossible de lire le contenu du CV.")

    profile = extract_profile(cv_text)
    queries = build_search_queries(profile)

    all_offers: List[JobOffer] = []
    for q in queries:
        all_offers.extend(fetch_indeed_jobs(q, max_results=7))  # ~7 par requête

    # dédoublonnage
    unique: dict[tuple[str, str, str], JobOffer] = {}
    for o in all_offers:
        key = (o.title, o.company, o.url)
        if key not in unique:
            unique[key] = o

    offers = list(unique.values())
    offers = enrich_scores(offers, profile)

    # on peut limiter à 20 pour rester raisonnable
    offers = offers[:20]

    return offers


@app.post("/api/contact-hr")
async def contact_hr(req: ContactRequest):
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
