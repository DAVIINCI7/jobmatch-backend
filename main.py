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
    description="Outil privé d'analyse de CV et de matching d'offres d'emploi.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # On changera plus tard pour ton URL Vercel seulement
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
    published_at: str
    is_paid: bool
    salary_text: str | None


class ContactRequest(BaseModel):
    job_title: str
    company: str
    hr_email: str
    candidate_name: str
    candidate_email: str
    cv_summary: str


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


def extract_profile(cv_text: str) -> dict:
    text = cv_text.lower()

    skill_dict = [
        "support technique", "support informatique", "helpdesk", "service desk",
        "windows", "linux", "macos",
        "active directory", "ad ", "gpo",
        "office 365", "m365", "outlook",
        "sccm", "intune",
        "tcp/ip", "dns", "dhcp", "vpn",
        "virtualisation", "vmware", "hyper-v",
        "powershell", "script", "python",
        "ticketing", "octopus", "jira", "zendesk",
        "impression", "printer", "déploiement poste",
        "customer service", "relation client"
    ]

    skills = sorted({s for s in skill_dict if s in text})

    titles_patterns = [
        r"technicien informatique",
        r"technicien support",
        r"technicien helpdesk",
        r"it support",
        r"support technique",
        r"analyste support",
        r"spécialiste support",
    ]

    titles = []
    for p in titles_patterns:
        titles += re.findall(p, text)

    if not titles:
        titles = ["technicien support informatique", "it support specialist"]

    return {"skills": skills, "titles": list(dict.fromkeys(titles))}


def build_search_queries(profile: dict) -> List[str]:
    titles = profile["titles"]
    skills = profile["skills"]

    queries = []

    for title in titles:
        base = title.replace(" ", "+")
        if skills:
            top = "+".join(skills[:5])
            queries.append(f"{base}+{top}")
        else:
            queries.append(base)

    return queries or ["it+support"]


def detect_paid(snippet: str, title: str) -> tuple[bool, str | None]:
    txt = (snippet + " " + title).lower()

    unpaid_keywords = ["bénévole", "non rémunéré", "non-remunere", "unpaid", "volunteer"]

    for k in unpaid_keywords:
        if k in txt:
            return False, None

    salary_patterns = [
        r"\$\s?\d+[kK]?",
        r"\d+\s?\$",
        r"\d{2}\s?-\s?\d{2}\s?\$",
        r"\d+\s?k",
        r"\d{2,3}\s?000",
        r"\d+\s?€/h",
        r"\d+\s?\$/h",
    ]

    for p in salary_patterns:
        m = re.search(p, snippet)
        if m:
            return True, m.group(0)

    paid_hint = any(x in txt for x in ["salaire", "remun", "rémun", "$", "€", "cad", "h$", "h/h"])

    return (True, None) if paid_hint else (True, None)


def fake_fetch_jobs(queries: List[str]) -> List[JobOffer]:
    now = datetime.now(timezone.utc)
    offers: List[JobOffer] = []

    for i, q in enumerate(queries):
        base_offers = [
            (
                "Technicien support informatique N2",
                "Centre Hospitalier Moderne",
                "Montréal, QC",
                f"https://ca.indeed.com/jobs?q={q}",
                "Indeed - recherche",
                0.91,
                "Poste permanent, temps plein, salaire compétitif, support utilisateurs, AD, M365."
            ),
            (
                "IT Support Specialist",
                "TechCorp Solutions",
                "Montréal, QC (Hybride)",
                f"https://www.linkedin.com/jobs/search/?keywords={q}",
                "LinkedIn Jobs - recherche",
                0.88,
                "Full-time, avantages, support niveau 1-2, outils ticketing, environnement Microsoft."
            ),
            (
                "Spécialiste Service Desk",
                "Groupe Entreprise",
                "Laval / Rive-Nord",
                f"https://www.glassdoor.ca/Job/jobs.htm?sc.keyword={q}",
                "Glassdoor - recherche",
                0.84,
                "Salaire + prime, gestion incidents, AD, O365, documentation."
            ),
            (
                "Technicien en support TI",
                "Organisation Publique",
                "Montréal, QC",
                f"https://www.talent.com/view?keywords={q}",
                "Talent.com - recherche",
                0.82,
                "Poste syndiqué, échelle salariale, support aux employés, postes Windows."
            ),
        ]

        time_offsets = [5, 20, 60, 180]

        for offset, (title, company, loc, url, src, score, snippet) in zip(time_offsets, base_offers):
            published_at = (now - timedelta(minutes=offset + i * 3)).isoformat()
            is_paid, salary_text = detect_paid(snippet, title)

            offers.append(JobOffer(
                title=title,
                company=company,
                location=loc,
                url=url,
                source=src,
                match_score=score,
                snippet=snippet,
                published_at=published_at,
                is_paid=is_paid,
                salary_text=salary_text
            ))

    unique = {}
    for o in offers:
        key = (o.title, o.company, o.url)
        if key not in unique:
            unique[key] = o

    return list(unique.values())


def refine_match_scores(offers: List[JobOffer], profile: dict) -> List[JobOffer]:
    skills = set(profile["skills"])

    for o in offers:
        bonus = 0.0
        text = (o.snippet + " " + o.title).lower()

        for s in skills:
            if s in text:
                bonus += 0.01

        if o.is_paid:
            bonus += 0.02

        o.match_score = round(min(o.match_score + bonus, 0.99), 2)

    offers.sort(key=lambda x: x.match_score, reverse=True)
    return offers


def filter_recent(offers: List[JobOffer], recent_minutes: int) -> List[JobOffer]:
    if recent_minutes <= 0:
        return offers

    now = datetime.now(timezone.utc)
    limit = now - timedelta(minutes=recent_minutes)

    filtered = []

    for o in offers:
        try:
            pub = datetime.fromisoformat(o.published_at)
        except Exception:
            continue

        if pub >= limit:
            filtered.append(o)

    return filtered


@app.post("/api/match", response_model=List[JobOffer])
async def match_jobs(
    cv: UploadFile = File(...),
    recent_minutes: int = 0,
    only_paid: bool = False
):
    if not cv.filename:
        raise HTTPException(status_code=400, detail="Aucun fichier reçu.")

    cv_text = extract_text_from_upload(cv)

    if not cv_text or len(cv_text.strip()) < 30:
        raise HTTPException(status_code=400, detail="Impossible de lire le CV.")

    profile = extract_profile(cv_text)
    queries = build_search_queries(profile)
    offers = fake_fetch_jobs(queries)
    offers = refine_match_scores(offers, profile)
    offers = filter_recent(offers, recent_minutes)

    if only_paid:
        offers = [o for o in offers if o.is_paid]

    return offers


@app.post("/api/contact-hr")
async def contact_hr(req: ContactRequest):
    if not req.hr_email:
        req.hr_email = "recrutement@" + req.company.replace(" ", "").lower() + ".com"

    email_body = f"""
Bonjour,

Je vous contacte concernant le poste "{req.job_title}" au sein de {req.company}.

Je possède une expérience pertinente en support informatique (niveaux 1-2), gestion des tickets,
environnements Microsoft (Windows, Active Directory, Office 365), ainsi qu'en service à la clientèle.

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
