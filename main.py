from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import io
import re
from datetime import datetime, timedelta, timezone

from pdfminer.high_level import extract_text as pdf_extract_text

app = FastAPI(
    title="JobMatch Assistant",
    description="Outil privé d'analyse de CV et de génération de recherches d'offres d'emploi.",
    version="3.0.0"
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
    published_at: str | None  # on garde le champ pour plus tard, mais on ne l'affiche plus
    is_paid: bool
    salary_text: str | None


class ContactRequest(BaseModel):
    job_title: str
    company: str
    hr_email: str
    candidate_name: str
    candidate_email: str
    cv_summary: str


# ---------- Utils CV ----------

def extract_text_from_upload(upload: UploadFile) -> str:
    content = upload.file.read()
    filename = (upload.filename or "").lower()

    if filename.endswith(".pdf"):
        try:
            return pdf_extract_text(io.BytesIO(content))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erreur lecture PDF: {str(e)}")

    try:
        return content.decode("utf-8", errors="ignore")
    except Exception:
        return content.decode(errors="ignore")


STOPWORDS = {
    "je", "nous", "vous", "ils", "elles", "le", "la", "les", "des", "de", "du", "un", "une",
    "et", "ou", "mais", "dans", "sur", "avec", "pour", "par", "mon", "ma", "mes", "ton",
    "ta", "tes", "son", "sa", "ses", "notre", "vos", "leur", "leurs",
    "the", "a", "an", "of", "in", "on", "at", "for", "to", "from", "by", "and", "or",
    "this", "that", "these", "those"
}


def extract_profile(cv_text: str) -> dict:
    """
    Analyse générique : on essaye de récupérer des titres de poste et des mots-clés
    quelle que soit la spécialité (IT, logistique, banque, retail, santé, etc.)
    """
    text = cv_text

    # 1) Détecter des "lignes titres" (avec mots comme technicien, agent, analyste, etc.)
    title_keywords = [
        "technicien", "technicienne", "agent", "agente", "analyste", "ingénieur", "ingenieur",
        "développeur", "developpeur", "développeuse", "assistant", "assistante",
        "conseiller", "conseillère", "representant", "représentant", "représentante",
        "gestionnaire", "superviseur", "superviseure", "responsable",
        "préposé", "préposée", "caissier", "caissière", "vendeur", "vendeuse",
        "chauffeur", "livreur", "préparateur", "préparatrice", "magasinier",
        "comptable", "infirmier", "infirmière", "administrateur", "administratrice",
        "coordonateur", "coordonnateur", "coordonnatrice"
    ]

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    found_titles: list[str] = []

    for line in lines:
        lower = line.lower()
        if any(k in lower for k in title_keywords):
            # On garde la ligne complète comme "titre" potentiel
            if 4 <= len(line) <= 120:
                found_titles.append(line)

    # Nettoyage doublons
    titles = list(dict.fromkeys(found_titles))

    if not titles:
        # fallback très général
        titles = ["candidat expérimenté", "professionnel polyvalent"]

    # 2) Extraire des mots-clés fréquents dans le CV
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]{4,}", text.lower())
    freq: dict[str, int] = {}
    for w in words:
        if w in STOPWORDS:
            continue
        freq[w] = freq.get(w, 0) + 1

    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    top_keywords = [w for w, c in sorted_words[:15]]

    return {"titles": titles, "keywords": top_keywords}


def build_search_queries(profile: dict) -> List[str]:
    """
    Construit des requêtes de recherche à partir des titres + mots-clés.
    Ces requêtes sont génériques et marchent pour tout type de CV.
    """
    titles = profile["titles"]
    keywords = profile["keywords"]

    queries: List[str] = []

    for title in titles[:3]:  # on prend 3 titres max
        base = "+".join(title.lower().split())
        extra = "+".join(keywords[:5]) if keywords else ""
        if extra:
            queries.append(f"{base}+{extra}")
        else:
            queries.append(base)

    # Fallback si jamais vide
    if not queries:
        queries = ["emploi+canada"]

    return queries


# ---------- Génération d'offres = en réalité des recherches intelligentes ----------

def fake_fetch_jobs(queries: List[str], profile: dict) -> List[JobOffer]:
    """
    IMPORTANT : ici on ne crée pas de "vraies" offres individuelles (on ne scrape pas les sites),
    on crée des BLOCS DE RECHERCHE intelligents vers Indeed / LinkedIn / Glassdoor / Talent.

    Chaque JobOffer = une recherche pré-configurée basée sur ton CV.
    Les vraies dates de publication sont visibles directement sur les sites eux-mêmes.
    """
    now = datetime.now(timezone.utc)
    offers: List[JobOffer] = []

    main_title = profile["titles"][0] if profile["titles"] else "profil"
    keywords_str = ", ".join(profile["keywords"][:6])

    for q in queries:
        # Indeed
        offers.append(JobOffer(
            title=f"Recherche Indeed pour « {main_title} »",
            company="Indeed",
            location="Canada / Montréal et environs",
            url=f"https://ca.indeed.com/jobs?q={q}",
            source="Indeed - recherche",
            match_score=0.90,
            snippet=f"Résultats Indeed adaptés à ton CV (mots-clés : {keywords_str}).",
            published_at=now.isoformat(),  # info technique seulement, non affichée dans l'UI
            is_paid=True,
            salary_text=None
        ))
        # LinkedIn
        offers.append(JobOffer(
            title=f"Recherche LinkedIn pour « {main_title} »",
            company="LinkedIn Jobs",
            location="Canada (hybride / télétravail inclus)",
            url=f"https://www.linkedin.com/jobs/search/?keywords={q}",
            source="LinkedIn Jobs - recherche",
            match_score=0.88,
            snippet=f"Résultats LinkedIn basés sur ton CV (mots-clés : {keywords_str}).",
            published_at=now.isoformat(),
            is_paid=True,
            salary_text=None
        ))
        # Glassdoor
        offers.append(JobOffer(
            title=f"Recherche Glassdoor pour « {main_title} »",
            company="Glassdoor",
            location="Canada",
            url=f"https://www.glassdoor.ca/Job/jobs.htm?sc.keyword={q}",
            source="Glassdoor - recherche",
            match_score=0.84,
            snippet="Recherche Glassdoor adaptée à ton profil. Consulte chaque annonce pour vérifier salaire et conditions.",
            published_at=now.isoformat(),
            is_paid=True,
            salary_text=None
        ))
        # Talent.com
        offers.append(JobOffer(
            title=f"Recherche Talent.com pour « {main_title} »",
            company="Talent.com",
            location="Canada",
            url=f"https://www.talent.com/jobs?k={q}",
            source="Talent.com - recherche",
            match_score=0.82,
            snippet="Résultats Talent.com basés sur ton CV pour différents employeurs.",
            published_at=now.isoformat(),
            is_paid=True,
            salary_text=None
        ))

    # Unicité
    unique: dict[tuple[str, str, str], JobOffer] = {}
    for o in offers:
        key = (o.title, o.company, o.url)
        if key not in unique:
            unique[key] = o

    # Tri par score
    res = list(unique.values())
    res.sort(key=lambda x: x.match_score, reverse=True)
    return res


def filter_recent(offers: List[JobOffer], recent_minutes: int) -> List[JobOffer]:
    """
    On garde cette fonction pour compatibilité, mais attention :
    comme chaque JobOffer représente une RECHERCHE et pas une annonce unique,
    la notion de "fraîcheur" est une approximation.
    Pour ne pas mentir, l'UI n'affiche plus ces dates.
    """
    if recent_minutes <= 0:
        return offers
    now = datetime.now(timezone.utc)
    limit = now - timedelta(minutes=recent_minutes)
    filtered = []
    for o in offers:
        try:
            pub = datetime.fromisoformat(o.published_at) if o.published_at else None
        except Exception:
            pub = None
        if pub is None or pub >= limit:
            filtered.append(o)
    return filtered


# ---------- Endpoints ----------

@app.post("/api/match", response_model=List[JobOffer])
async def match_jobs(
    cv: UploadFile = File(...),
    recent_minutes: int = 0,
    only_paid: bool = False  # gardé pour compat avec l'UI, mais on ne filtre pas vraiment
):
    if not cv.filename:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu.")

    cv_text = extract_text_from_upload(cv)
    if not cv_text or len(cv_text.strip()) < 30:
        raise HTTPException(status_code=400, detail="Impossible de lire le contenu du CV.")

    profile = extract_profile(cv_text)
    queries = build_search_queries(profile)
    offers = fake_fetch_jobs(queries, profile)
    offers = filter_recent(offers, recent_minutes)

    # pour l'instant toutes les recherches sont considérées comme "payées"
    # -> les vraies infos de salaire sont sur les sites eux-mêmes
    return offers


@app.post("/api/contact-hr")
async def contact_hr(req: ContactRequest):
    if not req.hr_email:
        req.hr_email = "recrutement@" + req.company.replace(" ", "").lower() + ".com"

    email_body = f"""
Bonjour,

Je vous contacte concernant le poste ou les opportunités associées à « {req.job_title} » au sein de {req.company}.

Je possède une expérience pertinente et des compétences en lien avec ce type de poste.

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
