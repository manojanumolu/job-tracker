"""Job intelligence (Milestone 5, Nov 2026): skills, match estimates, skill
gaps, interview topics, freshness and eligibility explanations.

Ground rules — every one is tested in tests/test_job_intel.py:

  * **Local and deterministic.** No AI service, no network, no paid API:
    a curated skill vocabulary matched with word-boundary rules. The same
    input always gives the same output.
  * **Never a gate.** Nothing here decides whether a job is shown, kept or
    emailed: eligibility stays with the scraper's classifier and
    job_filters. This module only reads jobs that already passed.
  * **Facts and estimates are kept apart.** A *fact* is something the
    tracker verified (a posting line it quoted, a check it passed, when it
    first saw the job); every fact carries its source. A *match* is an
    *estimate*, labelled as one, computed only from skills the posting's
    kept lines name — never guessed from a title alone, never invented.
  * **Private.** A résumé is read in memory to suggest skills; only the
    skill names the person confirms are stored (in their own users/{uid}
    profile). The résumé text is never stored, logged or sent anywhere.

Coverage is limited by design: the tracker keeps the posting's title and
the requirement lines it quoted as evidence, not the whole description, so a
posting can name skills this module never sees. The UI says so.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone

# canonical skill -> (category, patterns). Patterns are regular expressions
# matched case-insensitively at word boundaries. Ambiguous short names
# ("C", "R", "Go") need a disambiguating form.
_V = {
    # languages
    "Python": ("Languages", [r"python"]),
    "Java": ("Languages", [r"java(?!\s*script)"]),
    "JavaScript": ("Languages", [r"javascript", r"\bjs\b", r"ecmascript"]),
    "TypeScript": ("Languages", [r"typescript"]),
    "C++": ("Languages", [r"c\+\+", r"cpp"]),
    "C#": ("Languages", [r"c#", r"c sharp"]),
    "C": ("Languages", [r"\bc programming", r"\bc language", r"\bc/c\+\+"]),
    "Go": ("Languages", [r"golang", r"\bgo programming", r"\bgo language"]),
    "Kotlin": ("Languages", [r"kotlin"]),
    "Swift": ("Languages", [r"swift(?!\s*code)"]),
    "Scala": ("Languages", [r"scala"]),
    "Rust": ("Languages", [r"rust(?!ic)"]),
    "PHP": ("Languages", [r"php"]),
    "Ruby": ("Languages", [r"ruby"]),
    "R": ("Languages", [r"\br programming", r"\br language", r"\br/python", r"python/r\b", r"\bin r\b"]),
    "SQL": ("Data", [r"sql(?!ite)", r"t-sql", r"pl/sql"]),
    "Bash / shell": ("Tools", [r"bash", r"shell script\w*", r"unix shell"]),
    # web
    "HTML/CSS": ("Web", [r"html5?", r"css3?"]),
    "React": ("Web", [r"react(?:\.?js)?(?!ive)", r"react native"]),
    "Angular": ("Web", [r"angular(?:js)?"]),
    "Vue": ("Web", [r"vue(?:\.?js)?"]),
    "Node.js": ("Web", [r"node\.?js", r"nodejs", r"express\.?js"]),
    "Spring": ("Web", [r"spring boot", r"spring framework", r"\bspring\b"]),
    "Django / Flask": ("Web", [r"django", r"flask", r"fastapi"]),
    ".NET": ("Web", [r"\.net", r"asp\.net", r"dotnet"]),
    "REST APIs": ("Web", [r"rest(?:ful)? api\w*", r"\brest\b", r"web services?", r"microservices?"]),
    "GraphQL": ("Web", [r"graphql"]),
    # data and ML
    "Excel": ("Data", [r"excel", r"spreadsheets?"]),
    "Power BI": ("Data", [r"power ?bi"]),
    "Tableau": ("Data", [r"tableau"]),
    "Data analysis": ("Data", [r"data analy\w+", r"analytical skills", r"data visuali[sz]ation"]),
    "Statistics": ("Data", [r"statistic\w*", r"probability"]),
    "Machine learning": ("Data", [r"machine learning", r"\bml\b", r"deep learning", r"neural networks?"]),
    "AI / LLMs": ("Data", [r"\bgen(?:erative)? ?ai\b", r"\bllms?\b", r"large language models?", r"artificial intelligence",
                           r"\bai\b"]),
    "Pandas / NumPy": ("Data", [r"pandas", r"numpy", r"scikit-learn", r"sklearn"]),
    "Spark / big data": ("Data", [r"spark", r"hadoop", r"big data", r"databricks", r"kafka"]),
    "ETL / data pipelines": ("Data", [r"etl", r"data pipelines?", r"data warehous\w+", r"airflow"]),
    "Databases": ("Data", [r"databases?", r"rdbms", r"mysql", r"postgres\w*", r"oracle db", r"mongodb", r"nosql"]),
    # cloud and DevOps
    "AWS": ("Cloud & DevOps", [r"aws", r"amazon web services"]),
    "Azure": ("Cloud & DevOps", [r"azure"]),
    "GCP": ("Cloud & DevOps", [r"gcp", r"google cloud"]),
    "Cloud": ("Cloud & DevOps", [r"cloud computing", r"cloud platforms?", r"cloud services?"]),
    "Docker": ("Cloud & DevOps", [r"docker", r"containers?"]),
    "Kubernetes": ("Cloud & DevOps", [r"kubernetes", r"\bk8s\b"]),
    "CI/CD": ("Cloud & DevOps", [r"ci/cd", r"continuous integration", r"jenkins", r"github actions", r"devops"]),
    "Linux": ("Cloud & DevOps", [r"linux", r"unix"]),
    "Terraform": ("Cloud & DevOps", [r"terraform", r"infrastructure as code"]),
    "Networking": ("Cloud & DevOps", [r"networking", r"tcp/ip", r"\bdns\b", r"network protocols?"]),
    "Security": ("Cloud & DevOps", [r"cyber ?security", r"information security", r"network security",
                                    r"security fundamentals"]),
    # engineering practice
    "Data structures & algorithms": ("CS fundamentals", [r"data structures?", r"algorithms?"]),
    "OOP / design patterns": ("CS fundamentals", [r"object[- ]oriented", r"\boop\b", r"\booad\b", r"design patterns?"]),
    "System design": ("CS fundamentals", [r"system design", r"distributed systems?", r"scalab\w+",
                                          r"performance/scale"]),
    "Operating systems": ("CS fundamentals", [r"operating systems?"]),
    "Git": ("Tools", [r"\bgit\b", r"github", r"gitlab", r"version control"]),
    "Testing / QA": ("Testing", [r"unit test\w*", r"test automation", r"automated test\w*", r"\bqa\b",
                                 r"quality assurance", r"software testing", r"manual testing"]),
    "Selenium": ("Testing", [r"selenium", r"playwright", r"cypress"]),
    "Agile": ("Tools", [r"agile", r"scrum", r"kanban"]),
    "Jira": ("Tools", [r"jira"]),
    "SAP": ("Tools", [r"\bsap\b"]),
    "Salesforce": ("Tools", [r"salesforce"]),
    "ServiceNow": ("Tools", [r"servicenow"]),
    "Mobile apps": ("Web", [r"android", r"\bios\b", r"mobile app\w*", r"flutter", r"react native"]),
    "Embedded / hardware": ("Hardware", [r"embedded", r"microcontrollers?", r"\bvlsi\b", r"verilog", r"\bfpga\b",
                                         r"\brtos\b"]),
    # professional
    "Communication": ("Professional", [r"communication skills?", r"verbal and written", r"written and verbal",
                                       r"communicat\w+ effectively"]),
    "Problem solving": ("Professional", [r"problem[- ]solving", r"critical(?:ly)? think\w*", r"analytical thinking"]),
    "Teamwork": ("Professional", [r"team ?work", r"team player", r"collaborat\w+"]),
    "Accounting / finance": ("Professional", [r"accounting", r"\bgaap\b", r"\bifrs\b", r"financial reporting",
                                              r"reconciliation\w*", r"accounts payable", r"accounts receivable"]),
}


def _compile(patterns):
    return re.compile("|".join(f"(?<![\\w+#.])(?:{p})(?![\\w+#])" for p in patterns), re.I)


SKILLS: dict[str, tuple[str, re.Pattern]] = {name: (cat, _compile(pats)) for name, (cat, pats) in _V.items()}
CATEGORIES = tuple(dict.fromkeys(cat for cat, _ in SKILLS.values()))
MAX_SKILLS = 60                       # stored per person
RESUME_MAX_BYTES = 2 * 1024 * 1024


def category(skill: str) -> str:
    return SKILLS[skill][0] if skill in SKILLS else ""


def extract_skills(text: str) -> set[str]:
    """Canonical skills a text names (deterministic; empty for no text)."""
    if not isinstance(text, str) or not text.strip():
        return set()
    return {name for name, (_, rx) in SKILLS.items() if rx.search(text)}


def clean_skills(values) -> list[str]:
    """Only known canonical names, deduplicated, in vocabulary order —
    what may be stored for a person (nothing free-form)."""
    chosen = {v for v in values or [] if isinstance(v, str)}
    return [name for name in SKILLS if name in chosen][:MAX_SKILLS]


# ── a job's skills: facts, each with the line that names it ─────────────────

def job_lines(job: dict) -> list[str]:
    """The posting text the tracker kept for this job: its title and the
    requirement lines quoted as evidence."""
    out = []
    title = job.get("title") if isinstance(job, dict) else None
    if isinstance(title, str) and title.strip():
        out.append(title.strip())
    ev = job.get("evidence") if isinstance(job, dict) and isinstance(job.get("evidence"), dict) else {}
    for line in ev.get("experience_lines") or []:
        if isinstance(line, str) and line.strip():
            out.append(line.strip())
    return out


def job_skills(job: dict) -> dict[str, str]:
    """{skill: the kept posting line that names it} — every entry is a
    quote from the posting (a fact with its source), never an inference.
    Scans since Nov 2026 store the skills the full posting names
    (evidence.skills, scraper._posting_skills); older records only have
    their title and requirement lines."""
    found: dict[str, str] = {}
    ev = job.get("evidence") if isinstance(job, dict) and isinstance(job.get("evidence"), dict) else {}
    stored = ev.get("skills") if isinstance(ev.get("skills"), dict) else {}
    for name, line in stored.items():
        if name in SKILLS and isinstance(line, str) and SKILLS[name][1].search(line):   # re-verified, not trusted
            found.setdefault(name, line[:160])
    for line in job_lines(job):
        for name in extract_skills(line):
            found.setdefault(name, line)
    return {name: found[name] for name in SKILLS if name in found}


def match(user_skills, job: dict) -> dict:
    """An explainable match *estimate* between a person's skills and a job.

    score: share of the skills the posting's kept lines name that the person
    has (0-100), or None when those lines name fewer than MIN_SKILLS_FOR_SCORE
    skills (too little to judge). matched / missing: lists of skills, each
    with its quote. Never affects whether the job is shown or emailed."""
    have = set(clean_skills(user_skills))
    named = job_skills(job)
    matched = [(s, q) for s, q in named.items() if s in have]
    missing = [(s, q) for s, q in named.items() if s not in have]
    score = round(100 * len(matched) / len(named)) if len(named) >= MIN_SKILLS_FOR_SCORE else None
    return {"kind": "estimate", "score": score, "matched": matched, "missing": missing, "named": len(named),
            "basis": "skills named in the posting's title and the requirement lines this tracker kept"}


MIN_SKILLS_FOR_SCORE = 2


def match_label(score: int | None) -> str:
    if score is None:
        return "Not enough detail"
    return "Strong" if score >= 75 else "Good" if score >= 50 else "Partial" if score >= 25 else "Low"


# ── skill gaps and a learning roadmap ───────────────────────────────────────

_NEXT_STEPS = {
    "Languages": "Build two small projects end to end; solve 30 practice problems in the language.",
    "Web": "Build and deploy a small CRUD app with this framework; read its official tutorial first.",
    "Data": "Work through a public dataset end to end and publish the notebook or dashboard.",
    "Cloud & DevOps": "Use the free tier to deploy one of your projects; learn the core services it needs.",
    "CS fundamentals": "Revise the topic, then solve 20–40 graded problems on it.",
    "Tools": "Use it daily on a real project; most teams expect working fluency, not theory.",
    "Testing": "Add automated tests to one of your projects; learn one framework well.",
    "Hardware": "Pair a course with a hands-on board or simulator project.",
    "Professional": "Practise with mock interviews and written project summaries.",
}


def skill_gaps(user_skills, jobs: list[dict], top: int = 8) -> list[dict]:
    """The skills the person lacks that the most jobs (of the ones given —
    the jobs they can see) name, with how many jobs name each and a
    suggested next step. Counts are facts; the next step is guidance."""
    have = set(clean_skills(user_skills))
    counts: Counter = Counter()
    for j in jobs or []:
        for s in job_skills(j):
            if s not in have:
                counts[s] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], list(SKILLS).index(kv[0])))[:top]
    return [{"skill": s, "jobs": n, "category": category(s), "next_step": _NEXT_STEPS.get(category(s), "")}
            for s, n in ranked]


# ── interview preparation (a curated bank, never generated) ────────────────

_BANK = {
    "Python": {"basic": ["Lists vs tuples vs sets; when to use each", "How dictionaries work and their complexity",
                         "Exceptions and context managers"],
               "intermediate": ["Generators and iterators", "Decorators", "The GIL and when threads help"]},
    "Java": {"basic": ["OOP pillars with examples", "String immutability; equals vs ==", "Collections: List, Set, Map"],
             "intermediate": ["HashMap internals", "Checked vs unchecked exceptions", "Threads and synchronization"]},
    "JavaScript": {"basic": ["var / let / const and scope", "== vs ===", "Arrays: map, filter, reduce"],
                   "intermediate": ["Closures", "The event loop, promises and async/await", "Prototypes and this"]},
    "SQL": {"basic": ["SELECT with WHERE, GROUP BY, HAVING", "INNER vs LEFT JOIN", "Primary and foreign keys"],
            "intermediate": ["Window functions", "Indexes and when they help", "Normalisation (1NF–3NF)"]},
    "Data structures & algorithms": {
        "basic": ["Arrays vs linked lists", "Stacks and queues", "Big-O of common operations"],
        "intermediate": ["Hash tables and collisions", "BFS vs DFS", "Sorting algorithms and their trade-offs"]},
    "OOP / design patterns": {"basic": ["Encapsulation, inheritance, polymorphism, abstraction",
                                        "Interfaces vs abstract classes"],
                              "intermediate": ["SOLID principles", "Singleton, Factory and Observer patterns"]},
    "React": {"basic": ["Components, props and state", "Lists and keys"],
              "intermediate": ["useEffect and its dependencies", "Lifting state up vs context"]},
    "Git": {"basic": ["commit, branch, merge", "Resolving a merge conflict"],
            "intermediate": ["rebase vs merge", "Reverting a pushed commit safely"]},
    "Machine learning": {"basic": ["Supervised vs unsupervised learning", "Overfitting and how to detect it"],
                         "intermediate": ["Bias–variance trade-off", "Precision, recall and F1"]},
    "Statistics": {"basic": ["Mean, median, standard deviation", "Correlation vs causation"],
                   "intermediate": ["Hypothesis testing and p-values", "Confidence intervals"]},
    "Data analysis": {"basic": ["Cleaning missing values", "Choosing a chart for the question"],
                      "intermediate": ["A/B test analysis", "Cohort analysis"]},
    "Excel": {"basic": ["VLOOKUP / XLOOKUP", "Pivot tables"], "intermediate": ["INDEX-MATCH", "What-if analysis"]},
    "Cloud": {"basic": ["IaaS vs PaaS vs SaaS", "Regions and availability zones"],
              "intermediate": ["Designing for failure", "Cost basics"]},
    "AWS": {"basic": ["EC2, S3 and IAM in one sentence each"], "intermediate": ["VPC basics", "S3 consistency and storage classes"]},
    "Linux": {"basic": ["File permissions", "Processes and signals"], "intermediate": ["grep, sed, awk pipelines", "systemd basics"]},
    "Docker": {"basic": ["Images vs containers", "A minimal Dockerfile"], "intermediate": ["Layers and caching", "Volumes and networks"]},
    "REST APIs": {"basic": ["HTTP methods and status codes", "Idempotency"],
                  "intermediate": ["Pagination and versioning", "Authentication: sessions vs tokens"]},
    "Testing / QA": {"basic": ["Unit vs integration vs end-to-end tests", "Writing a good bug report"],
                     "intermediate": ["Test pyramid", "Mocking: when and when not"]},
    "System design": {"basic": ["Client–server and load balancing", "Caching"],
                      "intermediate": ["Designing a URL shortener", "Consistency vs availability"]},
    "Communication": {"basic": ["Tell me about yourself (90 seconds)", "Explain a project to a non-engineer"],
                      "intermediate": ["A disagreement in a team and how it ended", "A time you missed a deadline"]},
    "Problem solving": {"basic": ["Walk through a problem out loud", "Estimation questions"],
                        "intermediate": ["Debugging an unfamiliar system", "Trade-offs you made in a project"]},
}
DIFFICULTIES = ("basic", "intermediate")


def interview_topics(skills, difficulty: str = "basic") -> dict[str, list[str]]:
    """Curated topics per skill (only skills the bank covers)."""
    level = difficulty if difficulty in DIFFICULTIES else "basic"
    return {s: list(_BANK[s][level]) for s in clean_skills(skills) if s in _BANK}


# ── freshness and eligibility explanations (facts the tracker verified) ────

def _when(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def freshness(job: dict, now: datetime | None = None) -> dict:
    """When the posting was published (if the site said) and when this
    tracker first found it — both facts, each with its source."""
    now = now or datetime.now(timezone.utc)
    posted, found = _when(job.get("posted_at")), _when(job.get("first_seen"))
    ref = posted or found
    days = None if ref is None else max(0, (now - ref).days)
    label = ("Unknown" if days is None else "New today" if days == 0 else "This week" if days <= 7
             else "This month" if days <= 30 else "Older")
    return {"days": days, "label": label, "posted_at": posted, "first_seen": found,
            "source": "the company's posting date" if posted else "when this tracker first found it" if found else ""}


_CHECK_TEXT = {
    "real_job_posting": "It is a real job posting (not a news story or a programme page)",
    "india_location": "The posting's location is in India",
    "detail_read": "The tracker read the posting itself, not just a search result",
    "detail_is_this_job": "The page it read is this job's own posting",
    "no_conflicting_experience": "No line asks for more experience than a fresher has",
    "not_programme_story_talent_recruiter": "Not a programme story, talent community or recruiter page",
    "evidence_not_staff_context": "The fresher evidence isn't about existing staff",
    "fresher_or_entry_evidence": "The posting itself says fresher / entry level",
}


def eligibility_explanation(job: dict) -> list[dict]:
    """The safety checks this job passed, in plain words, with the quote the
    classifier relied on — read from the stored evidence (facts), never
    recomputed or relaxed here."""
    ev = job.get("evidence") if isinstance(job.get("evidence"), dict) else {}
    checks = ev.get("checks") if isinstance(ev.get("checks"), dict) else {}
    out = [{"check": k, "text": _CHECK_TEXT.get(k, k.replace("_", " ")), "passed": v is True}
           for k, v in checks.items()]
    quote = ev.get("fresher_evidence")
    if isinstance(quote, str) and quote.strip():
        out.append({"check": "quote", "text": f"Quoted from the posting: “{quote.strip()[:200]}”", "passed": True})
    return out


# ── résumé text (read in memory only) ───────────────────────────────────────

class ResumeError(ValueError):
    """A résumé file that can't be read here."""


def resume_text(name: str, data: bytes) -> str:
    """Plain text from a .txt or .docx résumé, in memory. PDFs need a PDF
    library this app doesn't ship: paste the text instead."""
    if not isinstance(data, (bytes, bytearray)) or len(data) > RESUME_MAX_BYTES:
        raise ResumeError("The file is larger than 2 MB.")
    lower = (name or "").lower()
    if lower.endswith(".txt"):
        return bytes(data).decode("utf-8", errors="replace")
    if lower.endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(bytes(data))) as z:
                info = z.getinfo("word/document.xml")
                if info.file_size > 20 * RESUME_MAX_BYTES:           # a zip bomb
                    raise ResumeError("The document is too large to read.")
                xml = z.read(info).decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError, OSError):
            raise ResumeError("That doesn't look like a Word (.docx) file.") from None
        xml = re.sub(r"</w:p>", "\n", xml)
        return re.sub(r"<[^>]+>", " ", xml)
    raise ResumeError("Upload a .txt or .docx file, or paste the text.")
