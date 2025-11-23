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

# -----------------------------------------------------------------------------
# App & CORS
# -----------------------------------------------------------------------------

app = FastAPI(
    title="JobMatch Assistant",
    description="Analyse ton CV et trouve des offres sur plusieurs plateformes.",
    version="5.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # plus tard: restreindre à ton domaine Vercel
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Modèles
# -----------------------------------------------------------------------------

class JobOffer(BaseModel):
    title: str
    company: str
    location: str
    url: str
    source: str          # "Indeed", "LinkedIn", "JobBank", "Glassdoor", "Talent"
    match_score: float   # 0.0 → 1.0
    snippet: str
    published_at: str | None  # texte brut ("il y a 2 jours", "23 nov 2025", etc.)
    is_paid: bool
    salary_text: str | None


class ContactRequest(BaseModel):
    job_title: str
    company: str
    hr_email: str
    candidate_name: str
    candidate_email: str
    cv_summary: str


# -----------------------------------------------------------------------------
# Utils CV
# -----------------------------------------------------------------------------

STOPWORDS = {
    "je", "nous", "vous", "ils", "elles", "le", "la", "les", "des", "de", "du", "un", "une",
    "et", "ou", "mais", "dans", "sur", "avec", "pour", "par", "mon", "ma", "mes", "ton",
    "ta", "tes", "son", "sa", "ses", "notre", "vos", "leur", "leurs",
    "the", "a", "an", "of", "in", "on", "at", "for", "to", "from", "by", "and", "or",
    "this", "that", "these", "those"
}


def extract_text_from_upload(upload: UploadFile) -> str:
    """
    Lit le contenu du CV (PDF / DOCX / autres) et renvoie le texte.
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

    # Autres (DOC, TXT, etc.) -> tentative de décodage texte
    try:
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return content.decode(errors="ignore")


def extract_profile(cv_text: str) -> dict:
    """
    Analyse générique : mots-clés fréquents + titres potentiels dans le CV.
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
    top_keywords = [w for w, c in sorted_words[:25]]

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
        "administratrice", "coordonnateur", "coordonnatrice", "support",
        "technologies", "informatique", "logistique", "banque", "financier"
    ]

    for line in lines:
        lower = line.lower()
        if any(k in lower for k in title_keywords):
            if 4 <= len(line) <= 120:
                title_candidates.append(line)

    titles = list(dict.fromkeys(title_candidates))

    # Si aucun titre détecté, on fabrique un titre à partir des mots-clés
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
    Construit des requêtes de recherche à partir des titres + mots-clés.
    Exemple : "technicien+informatique+support"
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


# -----------------------------------------------------------------------------
# Scrapers par plateforme
# (les sélecteurs HTML peuvent casser si les sites changent leur structure)
# -----------------------------------------------------------------------------

def fetch_indeed_jobs(query: str, max_results: int = 8) -> List[JobOffer]:
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
            is_paid=True,  # on suppose payé
            salary_text=salary_text,
        ))

    return offers


def fetch_linkedin_jobs(query: str, max_results: int = 6) -> List[JobOffer]:
    """
    Scraper simple LinkedIn Jobs (résultats publics).
    """
    url = f"https://www.linkedin.com/jobs/search?keywords={query}&location=Canada&f_TPR=r86400"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.8",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        print("Erreur réseau LinkedIn:", e)
        return []

    if resp.status_code != 200:
        print("Statut HTTP non 200 pour LinkedIn:", resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("div.base-card")[:max_results]

    offers: List[JobOffer] = []

    for c in cards:
        title_el = c.select_one("h3")
        company_el = c.select_one("h4")
        loc_el = c.select_one("span.job-search-card__location")
        date_el = c.select_one("time")
        link_el = c.select_one("a.base-card__full-link")

        title = title_el.get_text(strip=True) if title_el else "Titre non disponible"
        company = company_el.get_text(strip=True) if company_el else "Employeur non précisé"
        location = loc_el.get_text(strip=True) if loc_el else "Lieu non précisé"
        date_txt = date_el.get_text(strip=True) if date_el else None
        url_job = link_el["href"] if link_el and link_el.has_attr("href") else ""

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=url_job,
            source="LinkedIn",
            match_score=0.78,
            snippet="Offre trouvée sur LinkedIn Jobs.",
            published_at=date_txt,
            is_paid=True,
            salary_text=None,
        ))

    return offers


def fetch_jobbank_jobs(query: str, max_results: int = 6) -> List[JobOffer]:
    """
    Scraper JobBank Canada.
    """
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
        url_job = ""
        if title_el and title_el.has_attr("href"):
            href = title_el["href"]
            url_job = "https://www.jobbank.gc.ca" + href

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=url_job,
            source="JobBank",
            match_score=0.72,
            snippet="Offre trouvée sur JobBank Canada.",
            published_at=date_txt,
            is_paid=True,
            salary_text=None,
        ))

    return offers


def fetch_glassdoor_jobs(query: str, max_results: int = 6) -> List[JobOffer]:
    """
    Scraper simple Glassdoor (structure peut changer).
    """
    url = f"https://www.glassdoor.ca/Job/canada-{query}-jobs-SRCH_IL.0,6_IN3.htm"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        print("Erreur réseau Glassdoor:", e)
        return []

    if resp.status_code != 200:
        print("Statut HTTP non 200 pour Glassdoor:", resp.status_code)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select("li.react-job-listing")[:max_results]

    offers: List[JobOffer] = []

    for c in cards:
        title = c.get("data-normalize-job-title", "Titre non disponible")
        company = c.get("data-employer-name", "Employeur non précisé")
        location = c.get("data-job-loc", "Lieu non précisé")
        href = c.get("data-link", "")
        url_job = "https://www.glassdoor.ca" + href if href else ""

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=url_job,
            source="Glassdoor",
            match_score=0.68,
            snippet="Offre trouvée sur Glassdoor.",
            published_at=None,
            is_paid=True,
            salary_text=None,
        ))

    return offers


def fetch_talent_jobs(query: str, max_results: int = 6) -> List[JobOffer]:
    """
    Scraper simple Talent.com.
    """
    url = f"https://ca.talent.com/jobs?k={query}&l=Canada"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except Exception as e:
        print("Erreur réseau Talent.com:", e)
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
        url_job = link_el["href"] if link_el and link_el.has_attr("href") else ""

        offers.append(JobOffer(
            title=title,
            company=company,
            location=location,
            url=url_job,
            source="Talent.com",
            match_score=0.7,
            snippet="Offre trouvée sur Talent.com.",
            published_at=None,
            is_paid=True,
            salary_text=None,
        ))

    return offers


# -----------------------------------------------------------------------------
# Scoring & fusion
# -----------------------------------------------------------------------------

def enrich_scores(offers: List[JobOffer], profile: dict) -> List[JobOffer]:
    """
    On booste les offres qui contiennent des mots-clés du CV dans le titre/snippet.
    """
    keywords = set(profile["keywords"])

    for o in offers:
        txt = (o.title + " " + o.snippet).lower()
        bonus = 0.0
        for kw in keywords:
            if kw and kw in txt:
                bonus += 0.01
        # petit bonus selon la source
        if o.source == "Indeed":
            bonus += 0.03
        elif o.source == "LinkedIn":
            bonus += 0.02
        o.match_score = min(0.99, round(o.match_score + bonus, 2))

    offers.sort(key=lambda x: x.match_score, reverse=True)
    return offers


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------

@app.post("/api/match", response_model=List[JobOffer])
async def match_jobs(
    cv: UploadFile = File(...),
    recent_minutes: int = 0,   # gardé pour compat avec le front, non utilisé ici
    only_paid: bool = False    # idem
):
    """
    1. Lit le CV
    2. Extrait un "profil" (titres + mots-clés)
    3. Construit plusieurs requêtes
    4. Va chercher des offres sur Indeed, LinkedIn, JobBank, Glassdoor, Talent
    5. Fusionne, dédoublonne et renvoie max ~30 offres
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
        # Pour chaque plateforme on limite pour ne pas exploser le temps de réponse
        all_offers += fetch_indeed_jobs(q, 7)
        all_offers += fetch_linkedin_jobs(q, 5)
        all_offers += fetch_jobbank_jobs(q, 5)
        all_offers += fetch_glassdoor_jobs(q, 4)
        all_offers += fetch_talent_jobs(q, 4)

    # Dédoublonnage (titre + entreprise + url + source)
    unique: dict[tuple[str, str, str, str], JobOffer] = {}
    for o in all_offers:
        key = (o.title, o.company, o.url, o.source)
        if key not in unique:
            unique[key] = o

    offers = list(unique.values())
    offers = enrich_scores(offers, profile)

    # On ne renvoie que les 30 meilleures pour rester rapide
    offers = offers[:30]

    return offers


@app.post("/api/contact-hr")
async def contact_hr(req: ContactRequest):
    """
    Génère un email pour contacter HR (pas d'envoi réel).
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
