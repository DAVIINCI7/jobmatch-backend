from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import io
import re

from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------
# FastAPI app + CORS
# ---------------------------------------------------------

app = FastAPI(
    title="JobMatch Assistant Pro",
    description="Analyse un CV et trouve des offres sur plusieurs sites.",
    version="6.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # plus tard: restreindre à ton domaine Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# Modèles de données
# ---------------------------------------------------------

class JobOffer(BaseModel):
    title: str
    company: str
    location: str
    url: str
    source: str
    match_score: float
    snippet: str
    published_at: str | None
    is_paid: bool
    salary_text: str | None


class ContactRequest(BaseModel):
    job_title: str
    company: str
    hr_email: str
    candidate_name: str
    candidate_email: str
    cv_summary: str


# ---------------------------------------------------------
# Utils CV
# ---------------------------------------------------------

STOPWORDS = {
    "je", "nous", "vous", "ils", "elles", "le", "la", "les", "des", "de", "du", "un", "une",
    "et", "ou", "mais", "dans", "sur", "avec", "pour", "par", "mon", "ma", "mes", "ton",
    "ta", "tes", "son", "sa", "ses", "notre", "vos", "leur", "leurs",
    "the", "a", "an", "of", "in", "on", "at", "for", "to", "from", "by", "and", "or",
    "this", "that", "these", "those"
}


def extract_text_from_upload(upload: UploadFile) -> str:
    """
    Lit le CV (PDF, DOCX, DOC, TXT) et renvoie le texte.
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
    Extrait un 'profil' à partir du CV:
    - mots-clés fréquents
    - titre probable
    """
    text = cv_text

    # Mots-clés
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{4,}", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w in STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1

    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    top_keywords = [w for w, _ in sorted_words[:25]]

    # Titres possibles
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


def build_search_queries(profile: dict) -> list[str]:
    """
    Construit 1 à 2 requêtes de recherche à partir du CV.
    On reste court pour aller vite.
    """
    titles = profile["titles"]
    keywords = profile["keywords"]

    queries: list[str] = []

    if titles:
        base = "+".join(titles[0].lower().split())
        extra = "+".join(keywords[:3]) if keywords else ""
        if extra:
            queries.append(f"{base}+{extra}")
        else:
            queries.append(base)

    if not queries:
        queries = ["emploi+canada"]

    return queries[:2]


# ---------------------------------------------------------
# Scrapers multi-sites (simplifiés)
# ⚠️ Ça dépend de la structure HTML des sites.
#    Si les sites changent, il faudra ajuster les sélecteurs.
# ---------------------------------------------------------

def fetch_indeed_jobs(query: str, max_results: int = 10) -> List[JobOffer]:
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
        title_el = card.select_one("h2.jobTitle span") or card.select_one("h2.jobTitle")
        company_el = card.select_one("span.companyName")
        loc_el = card.select_one("div.companyLocation")
        date_el = card.select_one("span.date")
        snippet_el = card.select_one("div.job-snippet")
        salary_el = card.select_one("div.metadata.salary-snippet-container")

        title = title_el.get_text(strip=True) if title_el else "Titre non disponible"
        company = company_el.get_text(strip=True) if company_el else "Employeur non précisé"
        location = loc_el.get_text(strip=True) if loc_el else "Lieu non précisé"
        date_txt = date_el.get_text(strip=True) if date_el else None
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        salary_text = salary_el.get_text(" ", strip=True) if salary_el else None

        href = card.get("href", "")
        job_url = "https://ca.indeed.com" + href if href.startswith("/") else href

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=job_url,
            source="Indeed",
            match_score=0.8,
            snippet=snippet[:300],
            published_at=date_txt,
            is_paid=True,
            salary_text=salary_text
        ))

    return offers


def fetch_jobbank_jobs(query: str, max_results: int = 8) -> List[JobOffer]:
    url = f"https://www.jobbank.gc.ca/jobsearch/jobsearch?searchstring={query}"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        print("Erreur réseau JobBank:", e)
        return []

    if resp.status_code != 200:
        print("Statut HTTP non 200 pour JobBank:", resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("article.resultJobItem")[:max_results]

    offers: List[JobOffer] = []

    for c in cards:
        title_el = c.select_one("a.title")
        company_el = c.select_one("li.business")
        loc_el = c.select_one("li.location")
        date_el = c.select_one("li.date")

        title = title_el.get_text(strip=True) if title_el else "Titre non disponible"
        company = company_el.get_text(strip=True) if company_el else "Employeur non précisé"
        location = loc_el.get_text(strip=True) if loc_el else "Lieu non précisé"
        date_txt = date_el.get_text(strip=True) if date_el else None

        href = title_el["href"] if title_el and title_el.has_attr("href") else ""
        job_url = "https://www.jobbank.gc.ca" + href if href.startswith("/") else href

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=job_url,
            source="JobBank",
            match_score=0.7,
            snippet="Offre trouvée sur JobBank Canada.",
            published_at=date_txt,
            is_paid=True,
            salary_text=None
        ))

    return offers


def fetch_talent_jobs(query: str, max_results: int = 8) -> List[JobOffer]:
    url = f"https://ca.talent.com/jobs?k={query}&l=Canada"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        print("Erreur réseau Talent:", e)
        return []

    if resp.status_code != 200:
        print("Statut HTTP non 200 pour Talent.com:", resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("div.card.card__job")[:max_results]

    offers: List[JobOffer] = []

    for c in cards:
        title_el = c.select_one("h2.card__job-title")
        company_el = c.select_one("div.card__job-empname-label")
        loc_el = c.select_one("div.card__job-location-label")
        link_el = c.select_one("a")

        title = title_el.get_text(strip=True) if title_el else "Titre non disponible"
        company = company_el.get_text(strip=True) if company_el else "Employeur non précisé"
        location = loc_el.get_text(strip=True) if loc_el else "Lieu non précisé"
        href = link_el["href"] if link_el and link_el.has_attr("href") else ""
        job_url = href if href.startswith("http") else "https://ca.talent.com" + href

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=job_url,
            source="Talent.com",
            match_score=0.7,
            snippet="Offre trouvée sur Talent.com.",
            published_at=None,
            is_paid=True,
            salary_text=None
        ))

    return offers


def enrich_scores(offers: List[JobOffer], profile: dict) -> List[JobOffer]:
    """
    Boost les offres qui contiennent des mots-clés du CV.
    """
    keywords = set(profile["keywords"])
    for o in offers:
        txt = (o.title + " " + o.snippet).lower()
        bonus = 0.0
        for kw in keywords:
            if kw and kw in txt:
                bonus += 0.01
        if o.source == "Indeed":
            bonus += 0.02
        o.match_score = min(0.99, round(o.match_score + bonus, 2))

    offers.sort(key=lambda x: x.match_score, reverse=True)
    return offers


# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------

@app.post("/api/match", response_model=List[JobOffer])
async def match_jobs(
    cv: UploadFile = File(...),
    recent_minutes: int = 0,    # gardé pour compat front
    only_paid: bool = False     # idem
):
    """
    Analyse le CV et renvoie une liste d'offres multi-sites.
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
        all_offers += fetch_indeed_jobs(q, max_results=10)
        all_offers += fetch_jobbank_jobs(q, max_results=8)
        all_offers += fetch_talent_jobs(q, max_results=8)

    # dédoublonnage
    unique: dict[tuple[str, str, str, str], JobOffer] = {}
    for o in all_offers:
        key = (o.title, o.company, o.url, o.source)
        if key not in unique:
            unique[key] = o

    offers = list(unique.values())
    offers = enrich_scores(offers, profile)

    # filtre offres avec salaire si besoin
    if only_paid:
        offers = [o for o in offers if o.salary_text]

    # min 20 / max 40
    if len(offers) < 20:
        # on renvoie tout ce qu'on a
        return offers
    return offers[:40]


@app.post("/api/contact-hr")
async def contact_hr(req: ContactRequest):
    """
    Génère un email type à envoyer à HR.
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
