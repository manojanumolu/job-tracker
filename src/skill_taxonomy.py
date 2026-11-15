"""The M6 skill dictionary: canonical skills, their aliases and the related
(transferable) skills, in one transparent table (Nov 2026).

Why a second vocabulary next to job_intel's (M5)? M5 groups some skills
("Django / Flask", "Databases") because it only estimates; M6 compares a
résumé with a job requirement by requirement, and "Flask" is not "Django".
So M6 keeps them apart and says how they relate instead:

  * an **alias** is the same skill written differently ("Postgres" is
    PostgreSQL, "JS" is JavaScript). An alias match is an exact match.
  * a **related** skill is a different skill that transfers in part
    (Flask -> Django, MySQL -> PostgreSQL). It is never counted as the
    skill itself: the matcher reports it as "Related or transferable".
  * an **ambiguous** alias ("Go", "R", "C", "Spring", "AI", "REST") is only
    trusted in an unambiguous form; the bare word can mean something else,
    so a skill found only that way is reported as "Needs manual
    verification" rather than as found.

Each canonical skill also names its M5 equivalent (``M5``), so the skills an
admin confirms here can be copied into Settings → Skills & interview prep
(users/{uid}.skills) without a second storage system.

Deterministic, no network, no Streamlit. Tested in tests/test_m6_*.py.
"""

from __future__ import annotations

import re
from functools import lru_cache

# category labels (Part 4 of the M6 brief: languages, frameworks and
# libraries, databases, cloud, tools and platforms; plus the rest)
LANG, FRAME, DB, CLOUD, TOOLS, DATA, CS, TEST, PRO, DOMAIN = (
    "Programming languages", "Frameworks & libraries", "Databases", "Cloud & DevOps", "Tools & platforms",
    "Data & ML", "CS fundamentals", "Testing", "Professional skills", "Domain & business")
CATEGORIES = (LANG, FRAME, DB, CLOUD, TOOLS, DATA, CS, TEST, PRO, DOMAIN)

# name: (category, exact alias patterns, ambiguous patterns, M5 name or "")
# Patterns are case-insensitive regular expressions matched at word
# boundaries (see _compile). Keep each list short and obvious: this table is
# meant to be read and corrected by a person.
_T: dict[str, tuple[str, list[str], list[str], str]] = {
    # programming languages
    "Python": (LANG, [r"python3?"], [], "Python"),
    "Java": (LANG, [r"java(?!\s*script)(?:\s*(?:8|11|17|21))?", r"core java", r"j2ee", r"java ee"], [], "Java"),
    "JavaScript": (LANG, [r"javascript", r"ecmascript", r"es6"], [r"js"], "JavaScript"),
    "TypeScript": (LANG, [r"typescript"], [r"ts"], "TypeScript"),
    "C++": (LANG, [r"c\+\+(?:1[147]|20)?", r"cpp"], [], "C++"),
    "C#": (LANG, [r"c#", r"c sharp", r"csharp"], [], "C#"),
    "C": (LANG, [r"c programming", r"c language", r"ansi c", r"embedded c"], [r"c"], "C"),
    "Go": (LANG, [r"golang", r"go programming", r"go language"], [r"go"], "Go"),
    "Kotlin": (LANG, [r"kotlin"], [], "Kotlin"),
    "Swift": (LANG, [r"(?-i:Swift|SWIFT|SwiftUI)(?!\s*code)"], [], "Swift"),       # not the adjective "swift"
    "Scala": (LANG, [r"scala"], [], "Scala"),
    "Rust": (LANG, [r"rust(?:lang)?(?!ic|y)"], [], "Rust"),
    "PHP": (LANG, [r"php"], [], "PHP"),
    "Ruby": (LANG, [r"ruby"], [], "Ruby"),
    "R": (LANG, [r"r programming", r"r language", r"rstudio", r"r/python", r"python/r"], [r"r"], "R"),
    "MATLAB": (LANG, [r"matlab"], [], ""),
    "SQL": (LANG, [r"sql", r"t-sql", r"pl/sql", r"structured query language"], [], "SQL"),
    "Bash": (LANG, [r"bash", r"shell script\w*", r"unix shell", r"powershell"], [], "Bash / shell"),
    "HTML": (LANG, [r"html5?"], [], "HTML/CSS"),
    "CSS": (LANG, [r"css3?", r"sass", r"scss", r"tailwind(?:\s*css)?", r"bootstrap"], [], "HTML/CSS"),
    "Verilog / VHDL": (LANG, [r"verilog", r"vhdl", r"systemverilog"], [], "Embedded / hardware"),
    # frameworks and libraries
    "React": (FRAME, [r"react(?:\.?js)?(?!ive|\s*native|\s+(?:to|quickly|fast|well|calmly|positively|promptly)\b)",
                      r"redux", r"next\.?js"], [], "React"),            # not the verb ("react quickly")
    "React Native": (FRAME, [r"react native"], [], "Mobile apps"),
    "Angular": (FRAME, [r"angular(?:js)?"], [], "Angular"),
    "Vue": (FRAME, [r"vue(?:\.?js)?", r"nuxt"], [], "Vue"),
    "Node.js": (FRAME, [r"node\.?js", r"nodejs"], [], "Node.js"),
    "Express": (FRAME, [r"express\.?js"], [], "Node.js"),
    "Django": (FRAME, [r"django(?:\s*rest\s*framework)?", r"\bdrf\b"], [], "Django / Flask"),
    "Flask": (FRAME, [r"flask"], [], "Django / Flask"),
    "FastAPI": (FRAME, [r"fast\s?api"], [], "Django / Flask"),
    "Spring Boot": (FRAME, [r"spring\s*boot", r"spring framework", r"spring mvc", r"hibernate"], [r"spring"], "Spring"),
    ".NET": (FRAME, [r"\.net(?:\s*core)?", r"asp\.net", r"dotnet"], [], ".NET"),
    "Flutter": (FRAME, [r"flutter", r"dart"], [], "Mobile apps"),
    "Android": (FRAME, [r"android(?:\s*sdk|\s*studio)?", r"jetpack compose"], [], "Mobile apps"),
    "iOS": (FRAME, [r"\bios\b(?:\s*development)?", r"xcode"], [], "Mobile apps"),
    "Pandas": (FRAME, [r"pandas"], [], "Pandas / NumPy"),
    "NumPy": (FRAME, [r"numpy"], [], "Pandas / NumPy"),
    "scikit-learn": (FRAME, [r"scikit[- ]learn", r"sklearn"], [], "Pandas / NumPy"),
    "TensorFlow": (FRAME, [r"tensorflow", r"keras"], [], "Machine learning"),
    "PyTorch": (FRAME, [r"pytorch", r"torch"], [], "Machine learning"),
    "Matplotlib / Seaborn": (FRAME, [r"matplotlib", r"seaborn", r"plotly"], [], "Data analysis"),
    "Spark": (FRAME, [r"apache spark", r"pyspark", r"spark sql", r"databricks"], [r"spark"], "Spark / big data"),
    "Hadoop": (FRAME, [r"hadoop", r"hdfs", r"mapreduce", r"hive"], [], "Spark / big data"),
    "Kafka": (FRAME, [r"kafka"], [], "Spark / big data"),
    "Selenium": (FRAME, [r"selenium(?:\s*webdriver)?"], [], "Selenium"),
    "Playwright / Cypress": (FRAME, [r"playwright", r"cypress"], [], "Selenium"),
    "JUnit / pytest": (FRAME, [r"junit", r"pytest", r"testng", r"mockito", r"jest", r"unittest"], [], "Testing / QA"),
    "GraphQL": (FRAME, [r"graphql"], [], "GraphQL"),
    "LangChain / LLM APIs": (FRAME, [r"langchain", r"llama\s?index", r"openai api", r"hugging\s?face", r"transformers"],
                             [], "AI / LLMs"),
    # databases
    "MySQL": (DB, [r"mysql", r"mariadb"], [], "Databases"),
    "PostgreSQL": (DB, [r"postgres(?:ql)?", r"psql"], [], "Databases"),
    "Oracle Database": (DB, [r"oracle db", r"oracle database", r"oracle sql", r"oracle 1[19]c"], [], "Databases"),
    "SQL Server": (DB, [r"sql server", r"mssql", r"ms sql"], [], "Databases"),
    "SQLite": (DB, [r"sqlite"], [], "Databases"),
    "MongoDB": (DB, [r"mongo\s?db", r"mongoose"], [], "Databases"),
    "Redis": (DB, [r"redis"], [], "Databases"),
    "Firebase": (DB, [r"firebase", r"firestore"], [], "Databases"),
    "NoSQL": (DB, [r"nosql", r"cassandra", r"dynamodb", r"couchdb"], [], "Databases"),
    "Data warehousing": (DB, [r"data warehous\w+", r"snowflake", r"bigquery", r"redshift"], [], "ETL / data pipelines"),
    # cloud and DevOps
    "AWS": (CLOUD, [r"aws", r"amazon web services", r"ec2", r"\bs3\b", r"aws lambda"], [], "AWS"),
    "Azure": (CLOUD, [r"azure", r"microsoft azure"], [], "Azure"),
    "GCP": (CLOUD, [r"gcp", r"google cloud(?: platform)?"], [], "GCP"),
    "Docker": (CLOUD, [r"docker(?:file|\s*compose)?"], [r"containers?"], "Docker"),
    "Kubernetes": (CLOUD, [r"kubernetes", r"k8s", r"helm", r"openshift"], [], "Kubernetes"),
    "CI/CD": (CLOUD, [r"ci/cd", r"continuous integration", r"continuous delivery", r"jenkins", r"github actions",
                      r"gitlab ci", r"azure devops"], [r"devops"], "CI/CD"),
    "Terraform": (CLOUD, [r"terraform", r"infrastructure as code", r"\biac\b", r"ansible"], [], "Terraform"),
    "Linux": (CLOUD, [r"linux", r"ubuntu", r"unix", r"red hat", r"centos"], [], "Linux"),
    "Networking": (CLOUD, [r"computer networks?", r"networking", r"tcp/ip", r"\bdns\b", r"\bhttp/?s?\b protocols?",
                           r"network protocols?", r"ccna"], [], "Networking"),
    "Cybersecurity": (CLOUD, [r"cyber ?security", r"information security", r"network security", r"owasp",
                              r"penetration testing", r"\bsiem\b"], [r"security"], "Security"),
    # tools and platforms
    "Git": (TOOLS, [r"\bgit\b", r"github", r"gitlab", r"bitbucket", r"version control"], [], "Git"),
    "Jira": (TOOLS, [r"jira", r"confluence"], [], "Jira"),
    "Excel": (TOOLS, [r"ms excel", r"microsoft excel", r"advanced excel", r"(?-i:Excel|EXCEL)(?!\s+(?:at|in)\b)",
                      r"vlookup", r"xlookup", r"pivot tables?"],
              [], "Excel"),
    "Power BI": (TOOLS, [r"power ?bi", r"\bdax\b"], [], "Power BI"),
    "Tableau": (TOOLS, [r"tableau"], [], "Tableau"),
    "Postman": (TOOLS, [r"postman"], [], "REST APIs"),
    "Figma": (TOOLS, [r"figma"], [], ""),
    "SAP": (TOOLS, [r"\bsap\b(?:\s*(?:abap|fico|mm|sd|hana))?", r"abap"], [], "SAP"),
    "Salesforce": (TOOLS, [r"salesforce", r"\bapex\b"], [], "Salesforce"),
    "ServiceNow": (TOOLS, [r"servicenow"], [], "ServiceNow"),
    "Airflow": (TOOLS, [r"airflow"], [], "ETL / data pipelines"),
    # data and ML
    "Machine learning": (DATA, [r"machine learning", r"supervised learning", r"unsupervised learning",
                                r"regression models?", r"classification models?", r"random forest", r"xgboost"],
                         [r"ml"], "Machine learning"),
    "Deep learning": (DATA, [r"deep learning", r"neural networks?", r"\bcnns?\b", r"\brnns?\b", r"lstm"], [],
                      "Machine learning"),
    "NLP": (DATA, [r"natural language processing", r"\bnlp\b", r"text classification", r"sentiment analysis"], [],
            "AI / LLMs"),
    "Computer vision": (DATA, [r"computer vision", r"opencv", r"image (?:classification|segmentation|processing)",
                               r"object detection", r"yolo"], [], "Machine learning"),
    "Generative AI / LLMs": (DATA, [r"generative ai", r"gen ?ai", r"large language models?", r"\bllms?\b",
                                    r"prompt engineering", r"\brag\b"], [r"ai", r"artificial intelligence"], "AI / LLMs"),
    "Data analysis": (DATA, [r"data analy(?:sis|tics|zing|sing)", r"exploratory data analysis", r"\beda\b",
                             r"data cleaning", r"data wrangling"], [r"analytics"], "Data analysis"),
    "Data visualization": (DATA, [r"data visuali[sz]ation"], [r"dashboards?"], "Data analysis"),
    "Statistics": (DATA, [r"statistics", r"statistical (?:analysis|modelling|modeling|methods)", r"probability",
                          r"hypothesis testing", r"a/b testing"], [], "Statistics"),
    "ETL": (DATA, [r"\betl\b", r"elt", r"data pipelines?", r"data ingestion"], [], "ETL / data pipelines"),
    # CS fundamentals
    "Data structures & algorithms": (CS, [r"data structures?(?:\s*(?:and|&)\s*algorithms?)?", r"\bdsa\b",
                                          r"competitive programming", r"leetcode", r"codechef", r"codeforces"],
                                     [r"algorithms?"],
                                     "Data structures & algorithms"),
    "OOP": (CS, [r"object[- ]oriented(?:\s*programming|\s*design)?", r"\boops?\b", r"\booad\b"], [],
            "OOP / design patterns"),
    "Design patterns": (CS, [r"design patterns?", r"solid principles"], [], "OOP / design patterns"),
    "System design": (CS, [r"system design", r"distributed systems?", r"low[- ]level design", r"high[- ]level design",
                           r"scalab(?:le|ility) systems?"], [], "System design"),
    "Operating systems": (CS, [r"operating systems?", r"\bos concepts\b"], [], "Operating systems"),
    "DBMS": (CS, [r"\bdbms\b", r"database management systems?", r"normali[sz]ation", r"\bacid\b"], [], "Databases"),
    "REST APIs": (CS, [r"rest(?:ful)? apis?", r"rest(?:ful)? (?:web )?services?", r"api development"],
                  [r"rest", r"apis?"], "REST APIs"),
    "Microservices": (CS, [r"micro-?services?"], [], "REST APIs"),
    "Software testing": (TEST, [r"software testing", r"manual testing", r"test cases?", r"test plans?",
                                r"regression testing", r"quality assurance", r"\bqa\b", r"\bstlc\b"], [], "Testing / QA"),
    "Test automation": (TEST, [r"test automation", r"automation testing", r"automated tests?", r"unit tests?",
                               r"unit testing", r"integration tests?"], [], "Testing / QA"),
    "Agile / Scrum": (TOOLS, [r"agile", r"scrum", r"kanban", r"sprints?"], [], "Agile"),
    "Embedded systems": (DOMAIN, [r"embedded systems?", r"microcontrollers?", r"arduino", r"raspberry pi", r"\brtos\b",
                                  r"\bstm32\b", r"\bfpga\b", r"\bvlsi\b"], [], "Embedded / hardware"),
    # professional
    "Communication": (PRO, [r"communication skills?", r"verbal and written", r"written and verbal",
                            r"communicat\w+ effectively", r"presentation skills?"], [], "Communication"),
    "Problem solving": (PRO, [r"problem[- ]solving", r"critical thinking", r"analytical thinking", r"analytical skills?"],
                        [], "Problem solving"),
    "Teamwork": (PRO, [r"team ?work", r"team player", r"collaborat(?:ion|ive|ed|ing)"], [], "Teamwork"),
    "Leadership": (PRO, [r"leadership", r"led a team", r"team lead"], [], ""),
    # domain
    "Accounting": (DOMAIN, [r"accounting", r"\bgaap\b", r"\bifrs\b", r"financial reporting", r"reconciliations?",
                            r"accounts payable", r"accounts receivable", r"tally"], [], "Accounting / finance"),
}

# Related (transferable) skills: (a, b, note). Symmetric. A related skill is
# never the skill itself — the matcher shows it as transferable, with this
# note, and a person decides.
_RELATED = [
    ("Django", "Flask", "Both are Python web frameworks"),
    ("Django", "FastAPI", "Both are Python web frameworks"),
    ("Flask", "FastAPI", "Both are lightweight Python web frameworks"),
    ("React", "Angular", "Both are front-end frameworks"),
    ("React", "Vue", "Both are component-based front-end frameworks"),
    ("Angular", "Vue", "Both are front-end frameworks"),
    ("React", "React Native", "React Native uses React's component model"),
    ("Node.js", "Express", "Express is a Node.js framework"),
    ("JavaScript", "TypeScript", "TypeScript is JavaScript with types"),
    ("Java", "Kotlin", "Both run on the JVM"),
    ("Java", "C#", "Similar object-oriented languages"),
    ("C", "C++", "C++ builds on C"),
    ("Python", "R", "Both are used for data analysis"),
    ("MySQL", "PostgreSQL", "Both are relational SQL databases"),
    ("MySQL", "SQL Server", "Both are relational SQL databases"),
    ("MySQL", "Oracle Database", "Both are relational SQL databases"),
    ("PostgreSQL", "SQL Server", "Both are relational SQL databases"),
    ("PostgreSQL", "Oracle Database", "Both are relational SQL databases"),
    ("MySQL", "SQLite", "Both are relational SQL databases"),
    ("PostgreSQL", "SQLite", "Both are relational SQL databases"),
    ("SQL", "MySQL", "Using MySQL usually means writing SQL — confirm the queries you wrote"),
    ("SQL", "PostgreSQL", "Using PostgreSQL usually means writing SQL — confirm the queries you wrote"),
    ("SQL", "Oracle Database", "Using Oracle usually means writing SQL — confirm the queries you wrote"),
    ("SQL", "SQL Server", "Using SQL Server usually means writing SQL — confirm the queries you wrote"),
    ("SQL", "SQLite", "Using SQLite usually means writing SQL — confirm the queries you wrote"),
    ("SQL", "DBMS", "DBMS coursework covers SQL"),
    ("MongoDB", "NoSQL", "MongoDB is a NoSQL database"),
    ("Firebase", "NoSQL", "Firestore is a NoSQL database"),
    ("AWS", "Azure", "Both are public clouds with similar core services"),
    ("AWS", "GCP", "Both are public clouds with similar core services"),
    ("Azure", "GCP", "Both are public clouds with similar core services"),
    ("Docker", "Kubernetes", "Kubernetes runs containers"),
    ("Power BI", "Tableau", "Both are BI dashboard tools"),
    ("Excel", "Power BI", "Both are used for business reporting"),
    ("Data analysis", "Data visualization", "Analysis usually ends in a visualisation"),
    ("Pandas", "Data analysis", "Pandas is a data-analysis library"),
    ("Pandas", "NumPy", "Used together for numerical data"),
    ("scikit-learn", "Machine learning", "scikit-learn is a machine-learning library"),
    ("TensorFlow", "Deep learning", "TensorFlow is a deep-learning framework"),
    ("PyTorch", "Deep learning", "PyTorch is a deep-learning framework"),
    ("TensorFlow", "PyTorch", "Both are deep-learning frameworks"),
    ("Machine learning", "Deep learning", "Deep learning is a branch of machine learning"),
    ("Deep learning", "Computer vision", "Modern computer vision uses deep learning"),
    ("Deep learning", "NLP", "Modern NLP uses deep learning"),
    ("NLP", "Generative AI / LLMs", "LLMs are an NLP technique"),
    ("LangChain / LLM APIs", "Generative AI / LLMs", "LLM tooling"),
    ("Spark", "Hadoop", "Both are big-data processing frameworks"),
    ("Spark", "Kafka", "Both are used in big-data pipelines"),
    ("ETL", "Airflow", "Airflow orchestrates ETL pipelines"),
    ("ETL", "Data warehousing", "ETL loads data warehouses"),
    ("Selenium", "Playwright / Cypress", "All are browser-automation tools"),
    ("Selenium", "Test automation", "Selenium is a test-automation tool"),
    ("Software testing", "Test automation", "Automation is one kind of testing"),
    ("JUnit / pytest", "Test automation", "Unit-test frameworks automate tests"),
    ("REST APIs", "Microservices", "Microservices talk over APIs"),
    ("REST APIs", "Postman", "Postman is used to test APIs"),
    ("REST APIs", "GraphQL", "Both are API styles"),
    ("OOP", "Design patterns", "Design patterns build on OOP"),
    ("Linux", "Bash", "Shell scripting is a Linux skill"),
    ("Git", "CI/CD", "CI/CD pipelines start from version control"),
    ("CI/CD", "Docker", "Pipelines often build containers"),
    ("Terraform", "AWS", "Terraform provisions cloud resources"),
    ("HTML", "CSS", "Used together for web pages"),
    ("C", "Embedded systems", "Embedded work is mostly in C"),
    ("Verilog / VHDL", "Embedded systems", "Hardware description languages"),
    ("Android", "Kotlin", "Android apps are written in Kotlin or Java"),
    ("Android", "Java", "Android apps are written in Kotlin or Java"),
    ("iOS", "Swift", "iOS apps are written in Swift"),
    ("Flutter", "Android", "Flutter builds Android apps"),
    ("Flutter", "iOS", "Flutter builds iOS apps"),
    ("Spring Boot", "Java", "Spring Boot is a Java framework"),
    ("Django", "Python", "Django is a Python framework"),
    ("Flask", "Python", "Flask is a Python framework"),
    ("FastAPI", "Python", "FastAPI is a Python framework"),
    ("Pandas", "Python", "Pandas is a Python library"),
    (".NET", "C#", ".NET apps are usually written in C#"),
    ("Data structures & algorithms", "Problem solving", "DSA practice is problem solving"),
]


def _compile(patterns: list[str]) -> re.Pattern | None:
    if not patterns:
        return None
    return re.compile("|".join(f"(?<![\\w+#.])(?:{p})(?![\\w+#])" for p in patterns), re.I)


SKILLS: dict[str, dict] = {}
for _name, (_cat, _exact, _amb, _m5) in _T.items():
    SKILLS[_name] = {"category": _cat, "exact": _compile(_exact), "ambiguous": _compile(_amb), "m5": _m5,
                     "aliases": tuple(_exact), "ambiguous_aliases": tuple(_amb)}

RELATED: dict[str, dict[str, str]] = {name: {} for name in SKILLS}
for _a, _b, _note in _RELATED:
    assert _a in SKILLS and _b in SKILLS, (_a, _b)
    RELATED[_a][_b] = _note
    RELATED[_b][_a] = _note
del _name, _cat, _exact, _amb, _m5, _a, _b, _note

ORDER = {name: i for i, name in enumerate(SKILLS)}


def category(skill: str) -> str:
    return SKILLS[skill]["category"] if skill in SKILLS else ""


def known(skill: object) -> bool:
    return isinstance(skill, str) and skill in SKILLS


def sort_skills(skills) -> list[str]:
    return sorted({s for s in skills if s in SKILLS}, key=ORDER.__getitem__)


@lru_cache(maxsize=4096)
def _find(text: str) -> tuple[tuple[str, bool], ...]:
    out = []
    for name, spec in SKILLS.items():
        if spec["exact"] is not None and spec["exact"].search(text):
            out.append((name, True))
        elif spec["ambiguous"] is not None and _ambiguous_hit(name, spec["ambiguous"], text):
            out.append((name, False))
    return tuple(out)


# A bare ambiguous word counts (as "needs verification") only where it reads
# like a skill: in a comma/pipe/slash-separated list or after "in"/"using".
_LISTY = re.compile(r"[,|/•·;:]|\b(?:using|in|with|and)\b", re.I)


def _ambiguous_hit(name: str, rx: re.Pattern, text: str) -> bool:
    if name in ("C", "R", "Go"):
        # one-letter / two-letter words: only as a list item ("C, C++", "Python | R")
        return bool(re.search(rf"(?:^|[,|/;:•·(]\s*){re.escape(name)}(?=\s*(?:[,|/;)•·]|$))", text, re.I))
    return bool(rx.search(text)) and bool(_LISTY.search(text))


def find_skills(text: str) -> dict[str, bool]:
    """{canonical skill: certain?} named in ``text``. certain=False means
    only an ambiguous form was found ("Go", "AI", "Spring", "REST"): the
    caller must treat it as needing manual verification, never as found."""
    if not isinstance(text, str) or not text.strip():
        return {}
    return dict(_find(text[:5000]))


def related(skill: str) -> dict[str, str]:
    return dict(RELATED.get(skill, {}))


def to_m5(skills) -> list[str]:
    """The M5 (Settings) names for these M6 skills, deduplicated."""
    out: list[str] = []
    for s in sort_skills(skills):
        m5 = SKILLS[s]["m5"]
        if m5 and m5 not in out:
            out.append(m5)
    return out


def table() -> list[dict]:
    """The dictionary as rows (shown in the app, so it can be reviewed)."""
    return [{"skill": name, "category": spec["category"], "aliases": ", ".join(spec["aliases"]),
             "ambiguous": ", ".join(spec["ambiguous_aliases"]), "related": ", ".join(sorted(RELATED[name])),
             "settings_name": spec["m5"]} for name, spec in SKILLS.items()]
