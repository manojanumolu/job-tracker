"""M6 evaluation set (Part 13): synthetic résumés and job descriptions with
expected outcomes written by hand when each text was written — before the
extractor was run on it. All people, employers and institutions are
fictional (example.* domains, "Example …" names).

Labels use the canonical names of src/skill_taxonomy.py:
  skills        every skill the résumé genuinely claims (anywhere)
  demonstrated  the subset shown in an employment entry, an internship or a
                project (not merely listed, studied or certified)
  evidence      {skill: strongest kind} for a sample of skills:
                employment / internship / project / certification /
                coursework / listed
  grad_year     graduation year of the highest degree (None = not stated)
  emp_months    months of dated professional employment
Jobs:
  mandatory / preferred   skills the posting requires / prefers
  min_years               the stated minimum experience (0 = fresher)
Pairs:
  critical      True if the résumé fails a stated eligibility condition
  satisfied     the job's mandatory skills the résumé shows any evidence of
                (demonstrated or mentioned)

Limits, stated plainly: the set is small (12 résumés, 8 jobs, 24 pairs),
written by the same team that wrote the extractor, in English, and in the
formats this app's users are likely to upload. It measures the rules; it is
not an independent benchmark, and it can't support a "99% accuracy" claim.
"""
from pathlib import Path

FIX = Path(__file__).parent / "fixtures" / "m6"

RESUMES = {
    "r01_swe_fresher": {
        "text": (FIX / "resume_fresher_swe.txt").read_text(),
        "skills": ["Python", "Java", "JavaScript", "C", "SQL", "CSS", "React", "Node.js", "Django", "Flask",
                   "JUnit / pytest", "MySQL", "PostgreSQL", "MongoDB", "AWS", "Docker", "Kubernetes", "Linux", "Git",
                   "Postman", "Data structures & algorithms", "OOP", "Operating systems", "DBMS", "Networking",
                   "REST APIs", "Test automation"],
        "demonstrated": ["Python", "SQL", "CSS", "React", "Node.js", "Django", "Flask", "JUnit / pytest", "MySQL",
                         "PostgreSQL", "MongoDB", "AWS", "REST APIs", "Test automation"],
        "evidence": {"Flask": "internship", "Django": "project", "Docker": "listed", "OOP": "coursework",
                     "AWS": "project", "Java": "listed"},
        "grad_year": 2026, "emp_months": 0,
    },
    "r02_data_analyst": {
        "text": (FIX / "resume_data_analyst.txt").read_text(),
        "skills": ["Excel", "Power BI", "SQL", "Python", "Pandas", "Statistics", "Tableau", "MySQL",
                   "Matplotlib / Seaborn", "Data analysis"],
        "demonstrated": ["Excel", "Power BI", "SQL", "Python", "Pandas", "MySQL", "Matplotlib / Seaborn",
                         "Data analysis"],
        "evidence": {"Power BI": "project", "Tableau": "listed", "Statistics": "listed"},
        "grad_year": 2024, "emp_months": 0,
    },
    "r03_backend_senior": {
        "text": """Kiran Rao
Bengaluru · kiran@example.com
EDUCATION
B.Tech in Computer Science, Example University, 2019
EXPERIENCE
Senior Software Engineer, Example Corp | Jan 2021 – Dec 2025
• Built Java and Spring Boot microservices with PostgreSQL and Kafka
• Designed REST APIs and CI/CD pipelines on AWS with Docker
Software Engineer, Example Labs | Jul 2019 – Dec 2020
• Python and SQL data services
SKILLS
Java, Python, SQL, Spring Boot, Docker, AWS, Kafka, Kubernetes
""",
        "skills": ["Java", "Python", "SQL", "Spring Boot", "Docker", "AWS", "Kafka", "Kubernetes", "PostgreSQL",
                   "Microservices", "REST APIs", "CI/CD"],
        "demonstrated": ["Java", "Python", "SQL", "Spring Boot", "Docker", "AWS", "Kafka", "PostgreSQL",
                         "Microservices", "REST APIs", "CI/CD"],
        "evidence": {"Kafka": "employment", "Kubernetes": "listed", "Python": "employment"},
        "grad_year": 2019, "emp_months": 78,
    },
    "r04_ml_mtech": {
        "text": """Divya Nair
Kochi | divya@example.com
EDUCATION
M.Tech in Artificial Intelligence, Example Institute of Technology, 2024 – 2026
B.Tech in Computer Science and Engineering, Example College, 2020 – 2024, CGPA 8.9/10
PROJECTS
Medical Image Segmentation (M.Tech thesis)
• Trained a U-Net in PyTorch for image segmentation of CT scans
• Tracked experiments and evaluated with Dice score
Review Sentiment Classifier
• Fine-tuned a transformers model with Hugging Face for sentiment analysis
INTERNSHIP
Research Intern, Example AI Lab | May 2025 – Jul 2025
• Built data pipelines in Python and Pandas; trained XGBoost models
SKILLS
Python, PyTorch, TensorFlow, scikit-learn, NLP, Computer Vision, SQL
""",
        "skills": ["Python", "PyTorch", "TensorFlow", "scikit-learn", "NLP", "Computer vision", "SQL", "Pandas",
                   "Machine learning", "LangChain / LLM APIs", "ETL"],
        "demonstrated": ["PyTorch", "Computer vision", "NLP", "LangChain / LLM APIs", "Python", "Pandas",
                         "Machine learning", "ETL"],
        "evidence": {"PyTorch": "project", "TensorFlow": "listed", "Pandas": "internship"},
        "grad_year": 2026, "emp_months": 0,
    },
    "r05_qa_fresher": {
        "text": """Sneha Patil
Pune — sneha@example.com — +91 90000 11111
Education
B.E. in Information Technology, Example Engineering College, 2025, 76%
Internship
QA Intern, Example Software | Jan 2025 – May 2025
- Wrote test cases and executed regression testing for a billing web app
- Automated login and checkout flows with Selenium WebDriver in Java
- Logged defects in Jira and verified fixes
Projects
Library Management System (mini project)
- Java and MySQL desktop app; wrote SQL queries for reports
Skills
Manual testing, Selenium, Java, SQL, Jira, Postman, Agile
Certifications
ISTQB Foundation Level
""",
        "skills": ["Software testing", "Selenium", "Java", "SQL", "Jira", "Postman", "Agile / Scrum", "MySQL"],
        "demonstrated": ["Software testing", "Selenium", "Java", "SQL", "Jira", "MySQL"],
        "evidence": {"Selenium": "internship", "Postman": "listed", "MySQL": "project"},
        "grad_year": 2025, "emp_months": 0,
    },
    "r06_embedded_ece": {
        "text": """ARJUN MENON
Chennai | arjun@example.com
EDUCATION
B.E. (ECE), Example College of Engineering, 2022 - 2026
PROJECTS
Smart Irrigation Controller (final-year project)
• Programmed an STM32 microcontroller in Embedded C with FreeRTOS
• Simulated the control loop in MATLAB
Line-following robot
• Arduino and C, ultrasonic sensors
SKILLS
C, Embedded C, MATLAB, Verilog, Arduino
COURSEWORK
Digital Electronics, Microprocessors, Operating Systems
""",
        "skills": ["C", "Embedded systems", "MATLAB", "Verilog / VHDL", "Operating systems"],
        "demonstrated": ["C", "Embedded systems", "MATLAB"],
        "evidence": {"Verilog / VHDL": "listed", "Operating systems": "coursework", "MATLAB": "project"},
        "grad_year": 2026, "emp_months": 0,
    },
    "r07_frontend_1yr": {
        "text": """Farah Khan
Hyderabad · farah@example.com
WORK EXPERIENCE
Frontend Developer, Example Commerce Pvt Ltd | Jun 2025 – Present
• Built React and TypeScript components for the checkout flow
• Wrote unit tests with Jest; improved Lighthouse performance score
EDUCATION
B.Tech in Information Technology, Example University, 2025
PROJECTS
Weather dashboard — personal project
• Vue.js app calling a REST API
SKILLS
JavaScript, TypeScript, React, Redux, HTML, CSS, Git, Figma
""",
        "skills": ["JavaScript", "TypeScript", "React", "HTML", "CSS", "Git", "Figma", "Vue", "REST APIs",
                   "JUnit / pytest"],
        "demonstrated": ["TypeScript", "React", "Vue", "REST APIs", "JUnit / pytest"],
        "evidence": {"React": "employment", "Vue": "project", "Figma": "listed"},
        "grad_year": 2025, "emp_months": 18,
    },
    "r08_accountant": {
        "text": """Lakshmi Reddy
Vijayawada | lakshmi@example.com
EDUCATION
B.Com, Example Degree College, 2023, 81%
EXPERIENCE
Accounts Executive, Example Traders | Aug 2023 – Present
• Prepared GST returns and bank reconciliations in Tally
• Maintained accounts payable ledgers in MS Excel
SKILLS
Tally, Excel, Accounting, GST
""",
        "skills": ["Accounting", "Excel"],
        "demonstrated": ["Accounting", "Excel"],
        "evidence": {"Accounting": "employment", "Excel": "employment"},
        "grad_year": 2023, "emp_months": 40,
    },
    "r09_cloud_fresher": {
        "text": """Rohit Singh
Noida | rohit@example.com
EDUCATION
B.Tech in Computer Science, Example Institute, 2021 – 2025, CGPA 7.8/10
PROJECTS
Infrastructure as code for a blog
• Provisioned AWS EC2 and S3 with Terraform; containerised the app with Docker
• Set up GitHub Actions to deploy on every push
Home lab
• Ubuntu Linux server with Nginx and bash scripts for backups
CERTIFICATIONS
AWS Certified Solutions Architect – Associate
Microsoft Azure Fundamentals (AZ-900)
SKILLS
AWS, Docker, Terraform, Linux, Bash, Git, Python
""",
        "skills": ["AWS", "Docker", "Terraform", "Linux", "Bash", "Git", "Python", "CI/CD", "Azure"],
        "demonstrated": ["AWS", "Docker", "Terraform", "Linux", "Bash", "CI/CD", "Git"],
        "evidence": {"Terraform": "project", "Azure": "certification", "Python": "listed"},
        "grad_year": 2025, "emp_months": 0,
    },
    "r10_no_headings": {
        "text": """Vikram Joshi, vikram@example.com
I am a 2025 B.Sc Computer Science graduate. I built a small inventory app with Flask and SQLite and I know
Python and HTML. I like solving problems on LeetCode.
""",
        "skills": ["Flask", "SQLite", "Python", "HTML", "Data structures & algorithms"],
        "demonstrated": ["Flask", "SQLite"],          # a reader sees the app was built; the text has no sections
        "evidence": {},
        "grad_year": None, "emp_months": 0,
    },
    "r11_tricky_words": {
        "text": """Nikhil Das
Kolkata | nikhil@example.com
SUMMARY
I excel at deadlines, react quickly to feedback and go the extra mile. I spent the spring semester as a
teaching assistant and love Java coffee and swift decisions.
EDUCATION
B.Sc in Physics, Example College, 2024
SKILLS
Python, Excel
""",
        "skills": ["Python", "Excel"],
        "demonstrated": [],
        "evidence": {"Python": "listed"},
        "grad_year": 2024, "emp_months": 0,
    },
    "r12_diploma_technician": {
        "text": """Suresh Kumar
Coimbatore | suresh@example.com
EDUCATION
Diploma in Electrical Engineering, Example Polytechnic, 2020
EXPERIENCE
Maintenance Technician, Example Mills | Jun 2020 – May 2024
• Maintained PLC-controlled motors and wiring
SKILLS
AutoCAD, Excel
""",
        "skills": ["Excel"],
        "demonstrated": [],
        "evidence": {"Excel": "listed"},
        "grad_year": 2020, "emp_months": 48,
    },
}

JOBS = {
    "j1_backend_fresher": {"title": "Associate Software Engineer – Backend", "text": (FIX / "job_backend_fresher.txt").read_text(),
                           "mandatory": ["Python", "Java", "SQL", "REST APIs", "Data structures & algorithms"],
                           "preferred": ["Docker", "AWS", "Django", "Spring Boot"], "min_years": 0},
    "j2_data_analyst": {"title": "Data Analyst (Fresher)", "text": """About the role
You will build reports for our sales team.
Requirements
- Strong SQL skills and advanced Excel
- Any graduate (2024, 2025 or 2026 batch)
- Freshers welcome
Good to have
- Power BI or Tableau
- Python for data analysis
""", "mandatory": ["SQL", "Excel"], "preferred": ["Power BI", "Tableau", "Python", "Data analysis"], "min_years": 0},
    "j3_senior_backend": {"title": "Senior Backend Engineer", "text": """Requirements
- 4+ years of experience building backend services
- Must have Java, Spring Boot and Kafka
- Experience with Kubernetes
Preferred
- AWS
""", "mandatory": ["Java", "Spring Boot", "Kafka", "Kubernetes"], "preferred": ["AWS"], "min_years": 4},
    "j4_ml_engineer": {"title": "Machine Learning Engineer", "text": """What you'll need
- M.Tech or B.Tech in Computer Science or AI & ML
- Python and machine learning fundamentals
- Deep learning with PyTorch or TensorFlow
- 0-2 years of experience
Nice to have
- NLP or computer vision projects
""", "mandatory": ["Python", "Machine learning", "Deep learning", "PyTorch"], "preferred": ["NLP", "Computer vision"],
        "min_years": 0},
    "j5_qa_fresher": {"title": "QA Engineer – Fresher", "text": """Eligibility
- B.E. / B.Tech in any branch, 2025 or 2026 batch only
- Manual testing concepts and SQL are mandatory
- Freshers only
Nice to have
- Selenium automation
""", "mandatory": ["Software testing", "SQL"], "preferred": ["Selenium", "Test automation"], "min_years": 0},
    "j6_embedded_intern": {"title": "Embedded Software Intern", "text": """Requirements
- Pursuing B.E./B.Tech in ECE or EEE (2026 batch)
- Strong C programming and embedded systems fundamentals
Nice to have
- RTOS experience
- MATLAB
""", "mandatory": ["C", "Embedded systems"], "preferred": ["MATLAB"], "min_years": 0},
    "j7_frontend": {"title": "Frontend Developer", "text": """Requirements
- 1-3 years of experience
- React, JavaScript, HTML and CSS
Preferred
- TypeScript
""", "mandatory": ["React", "JavaScript", "HTML", "CSS"], "preferred": ["TypeScript"], "min_years": 1},
    "j8_accountant": {"title": "Junior Accountant", "text": """Requirements
- B.Com graduate
- Knowledge of accounting and Tally
- Proficiency in Excel
""", "mandatory": ["Accounting", "Excel"], "preferred": [], "min_years": 0},
}

PAIRS = [
    ("r01_swe_fresher", "j1_backend_fresher", {"critical": False, "satisfied": ["Python", "Java", "SQL", "REST APIs", "Data structures & algorithms"]}),
    ("r01_swe_fresher", "j3_senior_backend", {"critical": True, "satisfied": ["Java", "Kubernetes"]}),
    ("r02_data_analyst", "j2_data_analyst", {"critical": False, "satisfied": ["SQL", "Excel"]}),
    ("r02_data_analyst", "j1_backend_fresher", {"critical": True, "satisfied": ["Python", "SQL"]}),
    ("r03_backend_senior", "j3_senior_backend", {"critical": False, "satisfied": ["Java", "Spring Boot", "Kafka", "Kubernetes"]}),
    ("r03_backend_senior", "j1_backend_fresher", {"critical": True, "satisfied": ["Python", "Java", "SQL", "REST APIs"]}),
    ("r04_ml_mtech", "j4_ml_engineer", {"critical": False, "satisfied": ["Python", "Machine learning", "PyTorch"]}),
    ("r04_ml_mtech", "j2_data_analyst", {"critical": False, "satisfied": ["SQL"]}),
    ("r05_qa_fresher", "j5_qa_fresher", {"critical": False, "satisfied": ["Software testing", "SQL"]}),
    ("r05_qa_fresher", "j7_frontend", {"critical": True, "satisfied": []}),
    ("r06_embedded_ece", "j6_embedded_intern", {"critical": False, "satisfied": ["C", "Embedded systems"]}),
    ("r06_embedded_ece", "j5_qa_fresher", {"critical": False, "satisfied": []}),
    ("r07_frontend_1yr", "j7_frontend", {"critical": False, "satisfied": ["React", "JavaScript", "HTML", "CSS"]}),
    ("r07_frontend_1yr", "j3_senior_backend", {"critical": True, "satisfied": []}),
    ("r08_accountant", "j8_accountant", {"critical": False, "satisfied": ["Accounting", "Excel"]}),
    ("r08_accountant", "j2_data_analyst", {"critical": True, "satisfied": ["Excel"]}),      # 2023 vs the 2024-26 batch
    ("r09_cloud_fresher", "j1_backend_fresher", {"critical": False, "satisfied": ["Python"]}),
    ("r09_cloud_fresher", "j3_senior_backend", {"critical": True, "satisfied": []}),
    ("r10_no_headings", "j1_backend_fresher", {"critical": False, "satisfied": ["Python", "Data structures & algorithms"]}),
    ("r10_no_headings", "j2_data_analyst", {"critical": False, "satisfied": []}),
    ("r11_tricky_words", "j2_data_analyst", {"critical": False, "satisfied": ["Excel"]}),
    ("r11_tricky_words", "j7_frontend", {"critical": True, "satisfied": []}),
    ("r12_diploma_technician", "j8_accountant", {"critical": True, "satisfied": ["Excel"]}),
    ("r12_diploma_technician", "j5_qa_fresher", {"critical": True, "satisfied": []}),
]
