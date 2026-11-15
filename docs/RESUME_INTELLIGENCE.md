# Resume & Job Intelligence (Milestone 6, Nov 2026)

Admins compare their own résumés with the jobs this tracker found: requirement
by requirement, with the résumé evidence for every decision, eligibility checks
kept apart from the score, skill gaps, interview practice and résumé
suggestions. It is **deterministic and local**: no AI service, no network call,
no paid API. The same inputs always give the same output.

What it is not: a hiring prediction. A "90 %" estimate does not mean a 90 %
chance of being hired, and the page never says "eligible", "shortlisted" or
"interview-ready".

## Contents

1. [User guide: where everything is](#user-guide-where-everything-is)
2. [Architecture](#architecture)
3. [Résumé processing and supported formats](#résumé-processing-and-supported-formats)
4. [Data model, storage and deletion](#data-model-storage-and-deletion)
5. [The matching algorithm](#the-matching-algorithm)
6. [Scoring weights](#scoring-weights)
7. [Evidence classification](#evidence-classification)
8. [Accuracy evaluation](#accuracy-evaluation)
9. [Security controls](#security-controls)
10. [Known limitations](#known-limitations)
11. [Deployment and manual testing](#deployment-and-manual-testing)

## User guide: where everything is

**Who can see it.** Only admins (a verified email in `JT_ADMIN_EMAILS`), signed
in behind the sign-in gate, with personal data configured. Each admin sees only
their own résumés and analyses. Members never see the menu item, and opening the
page by its address (`?page=resume_intel`) sends them to Home.

| Where | What you can do |
|---|---|
| Sidebar → **Discover → Resume & Job Intelligence** (just below Application Tracker) | The page, with seven sections chosen by the pills at the top |
| **Overview** | Résumé count, jobs analysed (your default résumé against every active job), strong matches (≥ 75 % and no critical issue), potential eligibility issues, the skills most often missing, recent activity. Expanders: **Scoring weights** (editable) and **Skill dictionary** (aliases, ambiguous words, related skills) |
| **My Resumes** | Upload a PDF, DOCX or TXT and preview what was extracted, then **Save** as a new résumé or **Replace** an existing one's file (a version bump, after a confirmation). Each résumé card has **Review & correct**, **Set as default**, **Rename**, **Export data** (JSON) and **Delete** |
| My Resumes → **Review & correct** | Fix the name, education (level, specialisation, graduation year), whether each job was employment or an internship, and whether each project was academic or personal. Remove skills that aren't yours, confirm skills found in ambiguous wording, add skills the text doesn't show (kept as "needs manual verification"). **Save and confirm profile**. **Copy skills to Settings** fills Settings → Skills & interview prep (M5) |
| **Job Match** | Every active job against one or more résumés, ranked: no critical issue first, then the estimate. Filters: job title, company, location, experience level, minimum estimate, eligibility, résumé version. **Details** opens one comparison; **Save these results to history** stores the list |
| Job Match → **Details** | The estimate, confidence and evidence completeness; the eligibility issues (critical first); the five parts with their weights; and an evidence table for every skill (requirement line, status, résumé evidence, section). The best-fitting résumé for the job is named. **Paste the full job description** to analyse the whole posting. Buttons: Save to history, Skill gaps, Interview prep, Improve résumé |
| **Skill Gaps** | *One job*: demonstrated, mentioned only, missing mandatory, missing preferred, related/transferable and needs-verification skills, with prioritised next steps. *All jobs*: the skills worth closing across the catalogue, optionally focused on a role family |
| **Interview Preparation** | Choose a résumé, a job (or none) and a level: Basic, Intermediate or Advanced. Questions grouped by track (technical, programming, DSA, databases, frameworks, cloud & DevOps, project, from your résumé, behavioural, HR). Each has why it matters, concepts, an answer structure, an illustrative example and, for résumé questions, the résumé line. Set a practice status per question |
| **Resume Improvement** | Suggestions for a résumé (optionally for a job): before → after → why. **Accept** or **Reject** each one; download the accepted ones as a checklist (.txt). Your résumé is never edited |
| **Analysis History** | Uploads, replacements, corrections, confirmations and saved analyses, newest first; clear it after a confirmation |
| Any job's page → **Match with your résumé** (admins) | Choose a résumé and press **Analyse against résumé** for a compact result; **Open full analysis** goes to Job Match → Details |
| Settings → **Skills & interview prep** (admins) | A link to this page; the skill list there is filled by **Copy skills to Settings** |

## Architecture

All the logic is in `src/`, with no Streamlit import, and unit-tested. The UI is
one section of `streamlit_app.py`.

| Module | Role |
|---|---|
| `resume_parse.py` | File intake: size, extension and signature checks; TXT decoding; runs `resume_worker.py` for PDF/DOCX; text normalisation |
| `resume_worker.py` | Reads one untrusted PDF/DOCX in a **separate, short-lived process** (`python -I`) with CPU-time and memory limits and a wall-clock timeout. It uses only the standard library and `pypdf` |
| `resume_profile.py` | Résumé text → a structured, evidence-carrying profile (schema 1); corrections |
| `skill_taxonomy.py` | 106 canonical skills with aliases, ambiguous words and related (transferable) skills, plus their M5 names |
| `job_requirements.py` | Job text → mandatory / preferred / unspecified skills, experience, education, other conditions; role families; the scanner's requirement lines |
| `resume_match.py` | The matching engine, scoring, ranking, best résumé, per-pair and global skill gaps |
| `interview_prep.py` | The curated question bank (69 questions) and questions generated from the résumé |
| `resume_improve.py` | Improvement suggestions, plus the text checks made once at upload |
| `resume_store.py` | Per-admin storage (`ResumeLibrary`) with an authorisation hook on every operation |

Changes to existing code:

* **`scraper.py`** stores up to 30 requirement lines (`evidence.requirements`,
  each `{text, importance}`) for newly accepted jobs. Like M5's
  `evidence.skills`, they are added **after every gate has passed** and are
  wrapped so a failure only leaves the field out. Acceptance and rejection
  rules are unchanged, and the classifier and `job_filters` don't import M6.
* **`user_store.py`** adds `UserData.resume_library()`. `delete_personal_data()`
  now also deletes the M6 collections.
* **`streamlit_app.py`** adds the page, its navigation entry (admins only), a
  panel on job pages and a link in Settings.
* **`app_theme.py`** adds a small `.ri-*` style block, with mobile rules.
* **`requirements.txt`** adds **pypdf 6.21.4** (pure Python, no dependencies,
  hash-locked; no known advisories when added). No other pin changed.

## Résumé processing and supported formats

| Format | How it is read |
|---|---|
| **PDF** (text-based) | `pypdf`, up to 20 pages and 100,000 characters (the rest is skipped with a warning). A damaged page is skipped and the others are kept. Owner-password-only PDFs open; **user-password PDFs are refused** with a message |
| **DOCX** | The ZIP is checked before reading: at most 400 members, 40 MB uncompressed in total, compression ratio ≤ 120 per large member, `word/document.xml` ≤ 25 MB. Names with `..`, absolute paths or drive letters are refused (nothing is ever extracted to disk). `DOCTYPE`/`ENTITY` declarations are refused. Text, tabs, breaks and table cells (`a \| b`) are kept; headers/footers are read for ATS hints |
| **TXT** | UTF-8 (with or without BOM), UTF-16 with BOM, else cp1252; binary content is refused |

Checks run before any parser sees the bytes: 1 byte to 5 MB; the extension
must be `.pdf`, `.docx` or `.txt`; and the bytes must match it (`%PDF-`, a ZIP,
or text). An OLE file, such as a password-protected DOCX or an old `.doc`, is
refused with instructions.

**Scanned PDFs.** There is no OCR in this app. A PDF whose pages are images has
no text layer: it is reported as **"No text found"** with the reason, never as
read, and it can't be saved. Export the résumé as a text-based PDF or DOCX
instead.

**The file is never stored.** It is read in memory on the server. The text stays
in that browser session's memory until you save or discard the preview. Only
the structured profile and the file's name, type, size and SHA-256 are kept.
The SHA-256 is used to detect duplicate uploads. The text-dependent checks
(repeated lines, date formats, ATS problems) run once at upload and only their
findings are kept.

**Extraction** (`resume_profile.build_profile`) finds the following:

* **Sections.** About 95 common headings (Education, Work Experience,
  Internships, Projects, Technical Skills, Certifications, Achievements,
  Relevant Coursework, …).
* **Name.** Only from the first line, and only when it reads as a name; it is
  never guessed.
* **Contact details.** Kept masked (`p•••@example.com`, `••••••••10`), plus the
  link domains and a city.
* **Education.** Level (school / diploma / bachelors / masters / doctorate),
  degree, specialisation, institution, graduation year (whether "expected") and
  CGPA or percentage.
* **Experience.** Title, organisation, dates (many formats, "Present"), months,
  and **employment vs internship**. Overlapping months are counted once; end
  dates before start dates, and future starts, are flagged and not counted.
* **Projects.** **Academic** (a heading, or words like final-year, capstone,
  semester, thesis), **personal** (personal, side project, GitHub, freelance),
  or **not stated**.
* **Lists.** Certifications, achievements and coursework.
* **Skills.** Every skill with its evidence: section, entry, kind, the line, and
  whether the wording was certain. See [Evidence classification](#evidence-classification).

Nothing is invented. A field the résumé doesn't state stays empty, and the
profile lists warnings (no email, no education section, no standard headings).

## Data model, storage and deletion

Firestore, through the existing Admin SDK. The browser never reaches it: the
security rules deny every client. Everything lives under `users/{uid}/`:

| Collection | One document per | Holds |
|---|---|---|
| `resumes/{rid}` | résumé | name, file_type, file_name, size, sha256, uploaded_at, updated_at, version, versions[≤10], extraction_status, extraction_warnings, pages, is_default, rev, schema |
| `resume_profiles/{rid}` | résumé | the profile as extracted (schema 1) and hints (`tables`, `file_type`, text-check `findings`) |
| `resume_skills/{rid}` | résumé | the admin's corrections; `confirmed`, `confirmed_at` |
| `resume_matches/{sha(rid,job)}` | saved résumé × job | the summary (estimate, verdict, eligibility, parts, missing mandatory skills), the cache key, when |
| `resume_history/{time+random}` | event | resume_id, kind, label, detail, at — at most 300 (oldest dropped) |
| `resume_jobtext/{sha(job)}` | job | the description the admin pasted (public job text, ≤ 20,000 characters) |
| `resume_practice/{sha(question)}` | question | practice status |
| `resume_suggestions/{sha(rid,job)}` | résumé × job | accepted / rejected suggestion IDs |
| `resume_settings/main` | admin | scoring weights |

A profile document is a few tens of KB, far below Firestore's 1 MB limit. No
large document goes into Firestore, because the file itself is never stored.
Corrections are kept apart from the extraction, so they can be undone, and the
extraction is never overwritten by a correction.

The schema is versioned (`schema`), and nothing existing changed shape. Jobs
only gain the optional `evidence.requirements`; older jobs without it still
work (the engine falls back to the lines kept since M1). The Application
Tracker's schema is untouched.

**Retention and deletion.** Data is kept until the admin deletes it:

* **Delete a résumé** removes it and its profile, corrections, saved matches,
  suggestion decisions and history entries.
* **Replace** keeps the same résumé with a new version. The old version's name
  and date stay in `versions`; its profile is replaced and its corrections are
  cleared, because they referred to the old file's entries.
* **Clear history** removes the history.
* **Deleting the account** (Settings → Delete account →
  `delete_personal_data`) removes every M6 collection.

## The matching algorithm

`job_requirements.requirements(job, pasted)` reads the job from the best text
available:

1. **`pasted`**: the full description the admin pasted for this job.
2. **`stored`**: the requirement lines the scanner keeps since M6.
3. **`tracker`**: only the title and the experience and skill lines kept
   earlier. The result says so (`quality`: full / partial / minimal).

Each line's importance comes from its section (Requirements / Minimum
qualifications → mandatory; Preferred / Nice to have → preferred;
Responsibilities / About → context) and is overridden by cues on the line
("must", "required", "proficient in" → mandatory; "is a plus", "preferred",
"good to have" → preferred). "Python **or** Java" makes alternatives: only the
better-shown one is scored. Also extracted:

* **Experience**: ranges, "3+ years", "minimum 2 years", fresher cues.
* **Education**: degree levels and names, specialisations, "any branch", batch
  years.
* **Employment type** and **seniority**, from the title.
* **Other conditions**: minimum marks, backlogs, location restrictions,
  relocation, shifts, bonds, gaps, work authorisation.
* **Contradictions**, e.g. "freshers welcome" together with "3+ years".

`resume_match.analyse(profile, requirements, weights)` compares each skill:

| Status | Meaning | Credit |
|---|---|---|
| Explicitly demonstrated | named in a job, internship or project line (not a "learning…" line) | 1.0 |
| Mentioned but not demonstrated | only in the skills list, coursework, a certification or the summary | 0.5 |
| Related or transferable skill | not there, but a related skill is (e.g. MySQL for PostgreSQL), with the relation shown | 0.35 |
| Needs manual verification | ambiguous wording, or added by you without evidence | 0.25 |
| Not found | — | 0 |

A synonym is never silently an exact match. **Aliases** (Postgres =
PostgreSQL, JS = JavaScript) are the same skill. **Related** skills are
different skills and are shown as such. The skill dictionary is visible on the
Overview page. A posting skill found only in ambiguous wording ("AI", "Spark",
"REST") is shown but not scored.

The parts:

1. **Mandatory skills.** Mean credit over the mandatory requirements. When a
   posting doesn't say which skills are required, every skill it names counts,
   and the page says so.
2. **Preferred skills.** Mean credit over the preferred ones.
3. **Experience.** Dated professional employment only: internships and
   academic projects never count as employment.
   * Fresher roles: ≤ 1 year → 100. A stated range exceeded by more than a year
     → critical.
   * "N+ years" roles: proportional, and **critical** when below 75 % of the
     stated minimum (undated entries → "check yourself").
4. **Education.** A lower degree level than required → critical. A different
   degree at the same level (B.Sc vs B.Tech) → warning. A specialisation not in
   a listed set → warning, or critical when the posting says only/must. A
   graduation year outside the stated batch → critical. Anything unknown →
   "check yourself".
5. **Evidence.** The share of the job's skills shown in work, plus 15 points
   for a relevant certification.
6. **Overall estimate.** The weighted mean of the parts that could be computed,
   re-normalised over the weights available. It reads **"Insufficient
   evidence"** (no number) when:
   * the résumé has nothing recognisable,
   * fewer than 2 skills are named and none is marked mandatory, or
   * less than half of the weight could be computed.

**Eligibility is separate.** Issues are critical, warning, check-yourself or
note, each with the posting's quote. Any critical issue makes the verdict
**"Eligibility issue"**: it is listed first, sorts below eligible jobs, and is
never hidden by a high skills score. Location: a stated restriction the résumé
doesn't meet is a warning; a different city alone is only a note.

**Confidence** (High / Medium / Low) comes from *evidence completeness*: 40 %
the share of parts computed, 35 % how complete the job text is, 25 % the
résumé's standard sections.

**Several résumés, several jobs.** Every combination is supported. Ranking uses
`sort_key` (no critical issue first, then the estimate, then completeness), and
`best_resume` names the best-fitting résumé for a job.

**Caching.** A result is keyed by the engine version, a hash of the corrected
profile, a hash of the requirements and the weights. An unchanged pair is never
recomputed in a session, and résumés are never re-parsed to analyse.
Measured: 3,000 jobs × 4 résumés (12,000 comparisons) take about 1.8 s
including requirement parsing.

**Skill gaps.**

* *Per pair*: missing mandatory skills first (high priority), then related
  ones (built on what you have), then skills listed but not shown, then
  preferred ones (low priority).
* *Global*: priority = 3 × times required + 1 × times preferred + 1.5 × times
  named without a stated importance. It is weighted by the share of those jobs
  in the chosen role family and lowered by 20 % when a related skill is already
  there. A skill named by only one job is left out, so not every missing skill
  is listed.
* A missing skill is never described as making someone ineligible unless the
  posting marks it mandatory.

**Interview preparation.**

* **Curated questions** come from a fixed bank (69 questions: 31 basic, 26 intermediate, 12 advanced),
  chosen by the job's skills, the role family's core topics and the skills the
  résumé shows. They are always labelled as practice questions written for this
  app, **not** as questions the employer asks.
* **Résumé questions** are built from your own projects, internships and skill
  evidence, and quote the line.
* Example answers are labelled illustrative.

**Résumé improvement.** Suggestions only reword or restructure what is already
there:

* Vague contributions ("worked on…") become "[your action: built / designed /
  …]".
* A missing outcome becomes "— result: [add a real figure you can verify]".
* A keyword is suggested only where a related skill is already demonstrated,
  and then as "if you really used X here, name it".
* Also checked: repeated lines and verbs, mixed date or bullet formats, skills
  listed but never shown, and ATS risks (non-standard headings, no email,
  tables, contact details only in the header, multi-column PDFs, icons).
* Nothing is fabricated: no skills, employers, certifications, dates, metrics
  or achievements. Tests check that the "after" text adds no skill and no
  number.

## Scoring weights

| Part | Default weight |
|---|---|
| Mandatory skill alignment | 40 % |
| Preferred skill alignment | 15 % |
| Experience alignment | 20 % |
| Education alignment | 15 % |
| Project & certification evidence | 10 % |

These are starting values, not universal truths. Each admin can change them
(Overview → Scoring weights). They must be whole numbers from 0 to 100 adding
up to 100; anything else falls back to the defaults.

Review notes against the tracker's job data:

* Most stored jobs are fresher roles. For them experience mostly scores 100, so
  skills and education carry most of the difference.
* Many old jobs have only a few kept lines. Their mandatory part is often
  "named skills", which is why pasting the full description matters.
* Education requirements are frequent in Indian campus postings (batch years,
  branches), so they are checked as eligibility, not only scored.

## Evidence classification

| Kind | Where the skill was found | Counts as |
|---|---|---|
| Employment | a line of a professional job | demonstrated |
| Internship | a line of an internship / apprenticeship / industrial training | demonstrated |
| Academic project / Personal project / Project | a project line | demonstrated |
| Certification | a certification line | mentioned |
| Coursework / Education | coursework or education lines | mentioned |
| Skills list | the skills section | mentioned |
| Mentioned | summary, achievements, other sections, or a "learning / familiar with / exposure to" line anywhere | mentioned |
| Ambiguous wording | an ambiguous word outside a skills list | needs verification |
| Added by you | a correction with no evidence in the text | needs verification |

Links are removed before matching (a github.com link is not the skill Git).
One- and two-letter skills (C, R, Go) count only as list items.

## Accuracy evaluation

**The dataset** (`tests/m6_eval_data.py`, run with `python tests/m6_eval.py`):
12 synthetic résumés, 8 job descriptions and 24 labelled pairs. People and
employers are fictional. The labels were written with each text, before the
extractor was run on it. Two labels were corrected before the first run, after
re-reading the job texts:

* `r08 × j2`: graduated in 2023, outside the posting's 2024–2026 batch, so it
  is critical.
* `r10`: a reader sees the Flask app was built.

**First run:**

| Metric | Value |
|---|---|
| Skill extraction precision / recall | 96.2 % / 100 % |
| Demonstrated-skill precision / recall | 98.5 % / 97.0 % |
| False-positive rate (over the 106-skill dictionary) | 0.3 % |
| Mandatory-skill extraction precision / recall | 92.6 % / 100 % |
| Mandatory-skill matching precision / recall | 97.5 % / 100 % |
| Eligibility classification accuracy | 100 % (24 / 24) |
| Evidence attribution accuracy | 96.8 % |
| Graduation year / employment months | 100 % / 100 % |
| Score consistency (repeat, reordered sections, an added required skill never lowers it) | 100 % (44 checks) |

The first run's errors were the words "react quickly", "swift decisions" and
"Java coffee" read as skills. Two general rules were fixed: the verb "react
to/quickly/…" and lowercase "swift" no longer count. The "Java coffee" error
was left as a known limitation.

**Second run** (the same set, so it is optimistic):

* Skill precision 98.1 %, false-positive rate 0.2 %.
* Mandatory matching precision 100 %.
* Everything else unchanged.

**Remaining failing cases:**

* `r07`: "Wrote unit tests with Jest" was extracted as *Test automation*, which
  the label didn't include. This is arguably a labelling choice.
* `r10`: a résumé without section headings. "I built a small app with Flask" is
  only *mentioned*, because demonstration needs a project or experience
  section.
* `r11`: "Java coffee" is extracted as Java.
* `j4`: "AI & ML" in an education line is flagged as an ambiguous AI skill. It
  is shown, but not scored. "PyTorch or TensorFlow" yields TensorFlow as an
  alternative, which the label didn't list.
* `r01/Java`: the strongest evidence is a certification ("…using Java"); the
  label expected the skills list.

**What these numbers don't show.** The set is small, synthetic, in English,
and written by the same team as the rules. It measures whether the rules do
what they say. It is **not** an independent benchmark, and it cannot support a
"99 % accuracy" claim. Real résumés with unusual layouts, other languages or
scanned pages will do worse. `tests/test_m6_eval.py` keeps these numbers from
regressing.

## Security controls

* **Who.** Every `ResumeLibrary` method calls the authorisation hook first: the
  same check as the Application Tracker. It requires an admin, re-verified
  against Google/Firebase, signed in as the very UID the library is bound to. A
  refusal or an error in the hook refuses the operation (fail closed). Reads,
  writes, exports and deletes are all covered.
* **Whose.** A library is bound to one UID at construction. No method takes a
  UID. Résumé IDs must match `^[0-9a-f]{20}$` before any lookup, and job and
  question keys are SHA-256 hashes, so a request can't name another admin's
  data or leave the admin's tree. The two admins' separation is tested at the
  storage layer and in the app.
* **Direct access.** The page is in `ADMIN_PAGES`: members and non-gated
  sessions are redirected. Even if they weren't, every operation is checked
  again at the storage layer.
* **Files.** Size, extension and signature checks; an isolated, resource-limited
  parser process with a timeout; ZIP-bomb, path-traversal and XML-entity
  defences; encrypted, corrupt and unsupported files refused with safe
  messages.
* **Output.** Every value from a résumé or a posting is HTML-escaped. There is
  a test for markup and script injection, and a static check that the M6 UI
  escapes every interpolated value.
* **Privacy.**
  * The file is never stored or committed, and contact details are masked.
  * Nothing from a résumé is logged: errors are logged by type only, and a test
    checks the logs.
  * No M6 module imports a network library (tested). Résumés are never sent to
    any third party.
* **Session isolation.** The session cache is tied to the UID. A cache left
  behind by another account in the same browser is ignored (tested).

## Known limitations

* **No OCR.** Scanned PDFs are refused honestly.
* **Rule-based extraction.** It can miss unusual headings or layouts, résumés
  without sections (see the evaluation), non-English text, and skills outside
  the 106-skill dictionary (e.g. COBOL, Haskell; these are not counted rather
  than guessed). Multi-column PDFs may be read out of order; this is flagged as
  an ATS risk.
* **Partial job text.** Jobs found before M6 keep only some posting lines, so
  their analysis is "partial" or "minimal" until a description is pasted. New
  jobs store their requirement lines from the next scan onwards.
* **Rough units.** Mark comparisons only work when the units match (a CGPA
  against a percentage is left to the admin). Backlogs, bonds, shifts and work
  authorisation can't be checked from a résumé.
* **Version history keeps metadata only.** A replaced version's profile is not
  kept.
* **Fixed question bank.** Interview questions are curated and generated from
  templates, not written by a model.
* **Partly verified layout.** The responsive layout is verified by rendering the
  page's real HTML with the app's stylesheet in Chromium at 360–1280 px. The
  Streamlit widgets themselves were not driven in a phone browser.

## Deployment and manual testing

**Configuration.** Nothing new is needed beyond what the Application Tracker
uses: the sign-in gate, `JT_ADMIN_EMAILS`, and the Firebase service account
(personal data). Streamlit Community Cloud installs `requirements.txt`,
including pypdf. The PDF reader starts `python -I src/resume_worker.py` as a
subprocess, which Community Cloud allows.

**Manual test, as an admin:**

1. Open **Resume & Job Intelligence** and go to **My Resumes**.
2. Upload a text-based PDF résumé and press **Read résumé**. Check the
   preview: education, experience (internship vs job), projects and skills
   with evidence. Save it with a name.
3. **Review & correct**: change one project's type, remove a wrong skill, then
   **Save and confirm profile**.
4. Upload a DOCX version as a second résumé, and a scanned PDF to confirm it
   is refused as "No text found".
5. **Job Match**: check the ranking, open **Details** on one job, and paste its
   full description from the company's site. Check the evidence table and the
   eligibility issues.
6. Open a job's own page: **Match with your résumé → Analyse against résumé**.
7. **Skill Gaps** (one job, and all jobs), **Interview Preparation** (all three
   levels, set a practice status), **Resume Improvement** (accept one, reject
   one, download).
8. **Analysis History**; **Export data** on a résumé; **Delete** a résumé.
9. Sign in as the **other admin** and confirm none of the above is visible.
   Sign in as a **member** and confirm there is no menu item, and that
   `?page=resume_intel` lands on Home.
