"""Interview preparation for one job and one résumé (M6, Nov 2026).

Two kinds of question, always labelled:

  * **Curated practice questions** — a fixed bank written for this app
    (BANK), chosen by the skills the job asks for and the résumé shows, and
    by the job family. They are common interview topics, *not* questions any
    employer is known to ask: nothing here claims to know a company's
    interview.
  * **Generated from your résumé** — built from the résumé's own projects,
    internships and skill evidence ("Walk me through <your project>"),
    quoting the line they come from.

Each question has: why it matters, the concepts an answer should cover, a
suggested answer structure, a short example answer and (for résumé
questions) the résumé evidence. Levels: basic, intermediate, advanced.
Deterministic; no AI service and no network.
"""

from __future__ import annotations

import hashlib

import skill_taxonomy as tax

LEVELS = ("basic", "intermediate", "advanced")
TRACKS = {"technical": "Technical topics", "programming": "Programming", "dsa": "Data structures & algorithms",
          "database": "Databases", "framework": "Frameworks", "cloud": "Cloud & DevOps", "project": "Project questions",
          "resume": "From your résumé", "behavioural": "Behavioural", "hr": "HR"}
PRACTICE = ("Not started", "Practising", "Confident")
DISCLAIMER = ("Practice questions written for this app and chosen from the posting's requirements and your "
              "résumé. They are not questions this employer is known to ask.")

# (id, track, skill, level, question, why it matters, concepts, answer structure, example answer)
_Q = [
    # Python
    ("py-b1", "programming", "Python", "basic", "What is the difference between a list, a tuple and a set in Python?",
     "Choosing the right collection is everyday Python; it shows you know mutability and lookup cost.",
     "mutability; ordering; duplicates; O(1) membership for sets; tuples as dict keys",
     "Define each → one key property → when you'd use it.",
     "A list is ordered and mutable; a tuple is ordered and immutable, so it can be a dict key; a set is unordered "
     "with unique items and O(1) membership tests — I use one to de-duplicate IDs."),
    ("py-i1", "programming", "Python", "intermediate", "What are generators, and when would you use one?",
     "Memory-efficient iteration matters for large files and streams.",
     "yield; lazy evaluation; iterator protocol; memory use; generator expressions",
     "Definition → how yield works → a concrete use → the trade-off.",
     "A generator produces values lazily with yield, keeping its state between calls. I used one to stream a 2 GB "
     "log file line by line instead of loading it; the trade-off is it can only be iterated once."),
    ("py-a1", "programming", "Python", "advanced", "Explain the GIL. When do threads help in Python and when don't they?",
     "Shows you can reason about concurrency and pick threads, processes or async correctly.",
     "Global Interpreter Lock; I/O-bound vs CPU-bound; multiprocessing; asyncio",
     "What the GIL is → effect on CPU-bound work → effect on I/O-bound work → what you'd choose.",
     "CPython's GIL lets one thread run Python bytecode at a time, so threads don't speed up CPU-bound work — I'd use "
     "processes for that. For I/O-bound work like HTTP calls, threads or asyncio help because the GIL is released "
     "while waiting."),
    # Java
    ("java-b1", "programming", "Java", "basic", "What is the difference between == and equals() in Java?",
     "A classic source of bugs with strings and objects.",
     "reference equality; value equality; String pool; overriding equals and hashCode",
     "Define both → a String example → the equals/hashCode contract.",
     "== compares references; equals() compares values when the class overrides it. Two new String(\"a\") objects are "
     "not == but are equal. If you override equals you must override hashCode so HashMap works."),
    ("java-i1", "programming", "Java", "intermediate", "How does a HashMap work internally?",
     "Tests data-structure knowledge behind the most used Java collection.",
     "hashing; buckets; collisions; load factor and resizing; treeification; equals/hashCode",
     "Put path → get path → collisions → resizing → complexity.",
     "Keys are hashed to a bucket index; collisions go into a linked list that becomes a balanced tree past a "
     "threshold. When size exceeds capacity × load factor (0.75) it doubles and rehashes. Average O(1) get/put."),
    ("java-a1", "programming", "Java", "advanced", "How would you make a shared counter thread-safe in Java? Compare the options.",
     "Concurrency correctness is a common backend interview topic.",
     "synchronized; volatile (and why it isn't enough); AtomicInteger; LongAdder; locks",
     "The race → three fixes → trade-offs → what you'd pick.",
     "count++ is read-modify-write, so it races. synchronized fixes it with a lock; volatile alone doesn't make ++ "
     "atomic; AtomicInteger uses CAS without locking, and LongAdder scales better under heavy contention."),
    # JavaScript
    ("js-b1", "programming", "JavaScript", "basic", "What is the difference between var, let and const?",
     "Scope bugs are common in JavaScript code.", "function vs block scope; hoisting; temporal dead zone; reassignment",
     "Scope → hoisting → reassignment → what you use by default.",
     "var is function-scoped and hoisted as undefined; let and const are block-scoped and can't be used before "
     "declaration. const prevents reassignment, not mutation. I default to const, then let."),
    ("js-i1", "programming", "JavaScript", "intermediate", "Explain the event loop, promises and async/await.",
     "Asynchronous code is central to front-end and Node.js work.",
     "call stack; task and microtask queues; promise states; await; error handling",
     "The loop → microtasks vs tasks → promises → async/await as syntax over promises.",
     "JavaScript runs one call stack; async callbacks wait in queues. Promise callbacks are microtasks and run before "
     "the next task. async/await is syntax over promises, and errors are caught with try/catch."),
    # SQL / databases
    ("sql-b1", "database", "SQL", "basic", "What is the difference between INNER JOIN and LEFT JOIN?",
     "Joins are the most asked SQL topic.", "matching rows; NULLs for non-matches; counting with LEFT JOIN",
     "Define both → a two-table example → when the difference matters.",
     "INNER JOIN keeps only rows that match in both tables; LEFT JOIN keeps every row of the left table, with NULLs "
     "where nothing matches — so to list customers with zero orders you need a LEFT JOIN."),
    ("sql-i1", "database", "SQL", "intermediate", "Write a query for the second-highest salary in each department.",
     "Tests window functions and handling ties.", "window functions; DENSE_RANK vs RANK vs ROW_NUMBER; PARTITION BY",
     "Clarify ties → write the query → explain the ranking choice.",
     "SELECT dept, salary FROM (SELECT dept, salary, DENSE_RANK() OVER (PARTITION BY dept ORDER BY salary DESC) r "
     "FROM emp) t WHERE r = 2; DENSE_RANK so equal top salaries don't skip the second value."),
    ("sql-a1", "database", "SQL", "advanced", "A query got slow as the table grew. How do you investigate and fix it?",
     "Performance work shows real database experience.",
     "EXPLAIN plans; indexes and selectivity; composite index order; N+1 queries; covering indexes",
     "Measure → read the plan → hypothesis → fix → measure again.",
     "I'd run EXPLAIN to see whether it scans the whole table, check the WHERE and JOIN columns for usable indexes, "
     "add a composite index in the order the filter uses, then compare timings before and after."),
    ("dbms-b1", "database", "DBMS", "basic", "What is normalisation? Explain 1NF, 2NF and 3NF.",
     "Schema design basics are asked in most fresher interviews.",
     "atomic values; partial dependency; transitive dependency; trade-offs of denormalising",
     "Why normalise → each form with an example → when you'd denormalise.",
     "Normalisation removes redundancy: 1NF has atomic columns, 2NF removes dependencies on part of a composite key, "
     "3NF removes dependencies between non-key columns. Reporting tables are sometimes denormalised for speed."),
    ("dbms-i1", "database", "DBMS", "intermediate", "What do the ACID properties mean? Give an example where each matters.",
     "Transactions are core to any system that stores money or orders.",
     "atomicity; consistency; isolation levels; durability; dirty reads",
     "Each letter → one concrete failure it prevents.",
     "In a bank transfer, atomicity means both the debit and credit happen or neither; consistency keeps balances "
     "valid; isolation stops two transfers seeing half-done states; durability keeps a committed transfer after a crash."),
    # DSA
    ("dsa-b1", "dsa", "Data structures & algorithms", "basic", "Array vs linked list: when would you choose each?",
     "The basis of most data-structure questions.", "random access; insertion cost; memory locality; Big-O",
     "Compare access, insertion and memory → pick for a scenario.",
     "Arrays give O(1) index access and good cache locality; linked lists give O(1) insertion when you hold the node "
     "but O(n) access. For a list mostly read by index I'd use an array."),
    ("dsa-b2", "dsa", "Data structures & algorithms", "basic", "Reverse a string / check if a string is a palindrome.",
     "A warm-up coding question that tests clean code and edge cases.", "two pointers; edge cases; O(n) time, O(1) space",
     "Clarify (case, spaces) → approach → code → test edge cases.",
     "Use two pointers from both ends moving inward, comparing characters; return false at the first mismatch. "
     "Empty and one-character strings are palindromes."),
    ("dsa-i1", "dsa", "Data structures & algorithms", "intermediate", "Find two numbers in an array that add up to a target.",
     "The most common hashing interview problem.", "brute force O(n²); hash map O(n); sorting + two pointers",
     "Brute force → improve with a hash map → complexity → edge cases.",
     "Walk the array keeping a map of value → index; for each x, check whether target − x is already in the map. "
     "One pass, O(n) time and O(n) space."),
    ("dsa-i2", "dsa", "Data structures & algorithms", "intermediate", "BFS vs DFS: how do they differ and when do you use each?",
     "Graph traversal underlies many problems (shortest path, islands, dependencies).",
     "queue vs stack/recursion; shortest path in unweighted graphs; visited set; complexity O(V+E)",
     "Define both → data structure each uses → a use case for each.",
     "BFS explores level by level with a queue and finds shortest paths in unweighted graphs; DFS goes deep with "
     "recursion or a stack and suits cycle detection and topological sort. Both are O(V+E)."),
    ("dsa-a1", "dsa", "Data structures & algorithms", "advanced", "Design an LRU cache with O(1) get and put.",
     "Combines two data structures; frequently asked for backend roles.", "hash map + doubly linked list; eviction; capacity",
     "Requirements → data structures → get/put steps → complexity.",
     "Keep a hash map from key to node and a doubly linked list ordered by recency. get moves the node to the front; "
     "put inserts at the front and evicts the tail when over capacity. Both O(1)."),
    # OOP / design / OS / networks
    ("oop-b1", "technical", "OOP", "basic", "Explain the four pillars of OOP with an example.",
     "Asked in nearly every fresher interview.", "encapsulation; inheritance; polymorphism; abstraction",
     "Each pillar → one line → one example from a project.",
     "In a payments app: a Payment class hides its fields (encapsulation); CardPayment extends it (inheritance); "
     "pay() behaves differently per type (polymorphism); callers use the Payment interface only (abstraction)."),
    ("oop-i1", "technical", "Design patterns", "intermediate", "Explain the SOLID principles and one you've applied.",
     "Shows design maturity beyond syntax.", "single responsibility; open/closed; Liskov; interface segregation; dependency inversion",
     "Name each briefly → go deep on one with your own example.",
     "I applied single responsibility when I split a 600-line controller into a service for business rules and a "
     "repository for data access, which made both testable."),
    ("os-b1", "technical", "Operating systems", "basic", "What is the difference between a process and a thread?",
     "OS fundamentals come up in most campus interviews.", "address space; context switch cost; shared memory; synchronization",
     "Define both → what they share → trade-offs.",
     "A process has its own address space; threads in a process share memory, so they're cheaper to create and "
     "switch but need synchronisation to avoid races."),
    ("os-i1", "technical", "Operating systems", "intermediate", "What is a deadlock and how can it be prevented?",
     "Concurrency bugs are costly; interviewers test the conditions.", "four Coffman conditions; lock ordering; timeouts",
     "Define → the four conditions → break one of them.",
     "A deadlock is threads waiting on each other forever. It needs mutual exclusion, hold-and-wait, no pre-emption "
     "and circular wait; acquiring locks in a fixed global order breaks the circular wait."),
    ("net-b1", "technical", "Networking", "basic", "What happens when you type a URL into a browser and press Enter?",
     "Tests how well you connect networking pieces end to end.", "DNS; TCP handshake; TLS; HTTP request/response; rendering",
     "Walk the steps in order, one line each.",
     "The browser resolves the domain with DNS, opens a TCP connection and a TLS handshake, sends an HTTP GET, the "
     "server responds with HTML, and the browser parses it and fetches CSS, JS and images."),
    ("net-i1", "technical", "Networking", "intermediate", "TCP vs UDP: differences and when to use each.",
     "Protocol choice matters for real-time systems.", "connection; reliability; ordering; overhead; examples",
     "Compare on reliability and speed → an example for each.",
     "TCP is connection-oriented with ordered, reliable delivery — used for web and APIs; UDP has no handshake or "
     "retransmission, so it suits live video and games where late data is useless."),
    ("sd-i1", "technical", "System design", "intermediate", "Design a URL shortener.",
     "A standard system-design opener for backend roles.", "API; ID generation; storage; redirects; caching; scale",
     "Requirements → API → data model → ID scheme → scaling.",
     "POST /shorten stores (code, url); GET /{code} redirects with 301/302. Codes are base62 of a counter or random "
     "with collision checks; reads dominate, so cache hot codes in Redis."),
    ("sd-a1", "technical", "System design", "advanced", "How would you design a rate limiter for an API?",
     "Tests distributed-systems reasoning.", "token bucket; sliding window; per-user keys; Redis; consistency",
     "Requirements → algorithm → where it runs → distributed concerns.",
     "A token bucket per API key, stored in Redis with atomic updates, refilled at the allowed rate; reject with 429 "
     "and Retry-After when empty. Gateways enforce it before requests reach services."),
    # Frameworks
    ("rest-b1", "framework", "REST APIs", "basic", "What makes an API RESTful? Explain HTTP methods and status codes.",
     "Every backend and full-stack role builds APIs.", "resources; GET/POST/PUT/PATCH/DELETE; idempotency; 2xx/4xx/5xx",
     "Principles → methods → status codes → idempotency.",
     "Resources are nouns at URLs, methods say what to do: GET reads, POST creates, PUT replaces, DELETE removes. "
     "201 for created, 400 for bad input, 404 not found, 500 server error. GET, PUT and DELETE are idempotent."),
    ("rest-i1", "framework", "REST APIs", "intermediate", "How would you secure and version a REST API?",
     "APIs exposed to clients need auth and stable contracts.", "authentication vs authorization; tokens; HTTPS; versioning; rate limits",
     "Auth → transport → input validation → versioning strategy.",
     "Use HTTPS, token-based authentication (e.g. OAuth/JWT) with authorisation checks per resource, validate input, "
     "rate-limit, and version in the path (/v1/) so changes don't break clients."),
    ("react-b1", "framework", "React", "basic", "What are props and state in React?",
     "The core of React's data flow.", "one-way data flow; re-rendering; lifting state up",
     "Define both → who owns what → an example.",
     "Props are inputs passed from a parent and are read-only; state is owned by a component and changing it "
     "re-renders it. Shared state is lifted to the nearest common parent."),
    ("react-i1", "framework", "React", "intermediate", "When does useEffect run, and what are its dependencies for?",
     "Effect bugs (loops, stale data) are common.", "dependency array; cleanup; stale closures; data fetching",
     "When it runs → dependencies → cleanup → a pitfall you avoided.",
     "It runs after render; with [] once, with deps when they change. The cleanup runs before the next effect and on "
     "unmount — I used it to cancel a fetch when the user navigated away."),
    ("django-b1", "framework", "Django", "basic", "Explain Django's MVT architecture and the ORM.",
     "Django roles expect you to know how a request flows.", "URLs → views → templates; models; migrations; querysets",
     "Request path → each layer → the ORM's role.",
     "A URL maps to a view; the view uses models through the ORM and renders a template. Models define tables, "
     "migrations apply schema changes, and querysets are lazy until evaluated."),
    ("flask-b1", "framework", "Flask", "basic", "How is a Flask app structured, and how do you handle errors?",
     "Flask is a common first backend framework.", "routes; blueprints; request context; error handlers; config",
     "App and routes → blueprints → error handling → config.",
     "Routes are functions bound with @app.route; blueprints group them by feature; @app.errorhandler returns JSON "
     "errors; config comes from environment variables, never hard-coded secrets."),
    ("spring-b1", "framework", "Spring Boot", "basic", "What is dependency injection in Spring Boot?",
     "DI is the foundation of every Spring application.", "IoC container; @Component/@Service; constructor injection; testing",
     "Concept → how Spring does it → why it helps testing.",
     "Spring creates beans and passes their dependencies in, usually through the constructor, so classes don't "
     "construct their own collaborators — which lets tests pass mocks instead."),
    ("node-b1", "framework", "Node.js", "basic", "How does Node.js handle many requests with a single thread?",
     "Explains Node's performance model.", "event loop; non-blocking I/O; libuv thread pool; CPU-bound work",
     "Event loop → non-blocking I/O → what blocks it.",
     "Node runs JavaScript on one thread and hands I/O to the OS and libuv; callbacks run when I/O completes. "
     "CPU-heavy work blocks every request, so it goes to worker threads."),
    ("git-b1", "cloud", "Git", "basic", "What is the difference between git merge and git rebase?",
     "Daily collaboration skill.", "merge commits; linear history; rewriting history; shared branches",
     "Define both → the history each produces → the rule for shared branches.",
     "Merge joins histories with a merge commit; rebase replays your commits on top for a linear history. Never "
     "rebase commits others already pulled."),
    ("linux-b1", "cloud", "Linux", "basic", "How do Linux file permissions work?",
     "Needed for any server or DevOps work.", "rwx; user/group/other; chmod numeric; chown; execute bit on directories",
     "The model → numeric form → an example.",
     "Each file has read, write and execute bits for user, group and others; chmod 640 gives the owner read-write, "
     "the group read and others nothing. Execute on a directory lets you enter it."),
    ("docker-b1", "cloud", "Docker", "basic", "What is the difference between a Docker image and a container?",
     "Containers are standard in modern deployments.", "image layers; container as running instance; Dockerfile; registries",
     "Define both → how one becomes the other → a Dockerfile example.",
     "An image is a read-only template built from a Dockerfile in layers; a container is a running instance of it "
     "with its own writable layer. Images are pushed to and pulled from a registry."),
    ("docker-i1", "cloud", "Docker", "intermediate", "How do you keep Docker images small and builds fast?",
     "Shows practical container experience.", "layer caching; multi-stage builds; .dockerignore; slim base images",
     "Caching → multi-stage → base image → what you measured.",
     "Copy dependency files and install before copying source so the layer caches; use a multi-stage build so only "
     "the runtime artefacts ship; start from a slim base and add a .dockerignore."),
    ("k8s-i1", "cloud", "Kubernetes", "intermediate", "What are Pods, Deployments and Services in Kubernetes?",
     "The core objects of any Kubernetes cluster.", "pods; replica sets; rolling updates; service discovery; load balancing",
     "Each object → how they work together.",
     "A Pod runs one or more containers; a Deployment keeps N Pods running and rolls out updates; a Service gives "
     "them a stable address and load-balances across them."),
    ("aws-b1", "cloud", "AWS", "basic", "Explain EC2, S3 and IAM in one sentence each, and how you'd deploy a web app.",
     "Cloud basics are expected even for fresher roles.", "compute; object storage; identities and policies; least privilege",
     "One line each → a simple deployment.",
     "EC2 is virtual servers, S3 is object storage and IAM controls who may do what. I'd run the app on EC2 (or "
     "Elastic Beanstalk), keep static files in S3 and give the instance an IAM role with only the permissions it needs."),
    ("cicd-i1", "cloud", "CI/CD", "intermediate", "Describe a CI/CD pipeline you would set up for a web service.",
     "Teams expect engineers to ship safely and often.", "build; tests; artefacts; environments; approvals; rollback",
     "Trigger → stages → gates → deployment → rollback.",
     "On every push: install, lint, run tests, build an image; on main, deploy to staging, run smoke tests, then "
     "production behind an approval, with a one-click rollback to the previous image."),
    ("c-b1", "programming", "C", "basic", "What is a pointer, and what is the difference between malloc and calloc?",
     "C fundamentals for embedded and systems roles.", "addresses; dereferencing; heap allocation; zero-initialisation; free",
     "Pointer → allocation functions → freeing memory.",
     "A pointer stores an address; *p reads the value there. malloc allocates uninitialised memory, calloc allocates "
     "and zeroes it; both must be released with free to avoid leaks."),
    ("cpp-i1", "programming", "C++", "intermediate", "What are virtual functions and how does dynamic dispatch work in C++?",
     "Core to C++ object-oriented design.", "vtable; override; virtual destructors; object slicing",
     "Concept → vtable → why destructors must be virtual.",
     "A virtual function is resolved at run time through the object's vtable, so a base pointer calls the derived "
     "override. Base classes need a virtual destructor so deleting through a base pointer runs the derived one."),
    # Data
    ("ml-b1", "technical", "Machine learning", "basic", "What is overfitting and how do you detect and prevent it?",
     "The most asked ML fundamentals question.", "train vs validation error; cross-validation; regularisation; more data",
     "Define → detect → three fixes.",
     "Overfitting is when a model learns noise: training error is low but validation error is high. Cross-validation "
     "shows it; regularisation, simpler models, early stopping and more data reduce it."),
    ("ml-i1", "technical", "Machine learning", "intermediate", "Precision vs recall: when would you optimise for each?",
     "Choosing metrics is a core ML judgment.", "confusion matrix; F1; thresholds; class imbalance",
     "Define both → a case for each → how you'd tune the threshold.",
     "Precision is how many predicted positives are right; recall is how many real positives you catch. For fraud "
     "alerts reviewed by people I'd favour recall; for auto-blocking payments, precision."),
    ("ml-a1", "technical", "Machine learning", "advanced", "How would you deploy and monitor an ML model in production?",
     "Distinguishes coursework from production experience.", "serving; versioning; data drift; monitoring; retraining",
     "Packaging → serving → monitoring → retraining loop.",
     "Version the model and features, serve it behind an API, log inputs and predictions, watch for data and "
     "prediction drift against the training distribution, and retrain on a schedule or when drift crosses a threshold."),
    ("stat-b1", "technical", "Statistics", "basic", "What is the difference between mean and median, and when is each misleading?",
     "Basic statistics for any data role.", "outliers; skewed distributions; robust statistics",
     "Define → skewed example → which to report.",
     "The mean is pulled by outliers; the median isn't. For salaries, where a few very high values skew the data, "
     "the median describes a typical person better."),
    ("stat-i1", "technical", "Statistics", "intermediate", "Explain hypothesis testing and p-values with an A/B test.",
     "A/B testing is everyday work for analysts.", "null hypothesis; significance level; p-value; sample size; errors",
     "Set-up → test → interpret → common mistakes.",
     "The null hypothesis is that both versions convert equally; the p-value is the probability of a difference this "
     "large if the null were true. Below 0.05 we reject it — but only with a sample size fixed in advance."),
    ("da-b1", "technical", "Data analysis", "basic", "How do you handle missing values in a dataset?",
     "Data cleaning is most of an analyst's job.", "why data is missing; drop vs impute; mean/median/mode; flags",
     "Understand why → options → what you chose and why.",
     "First check why it's missing. If few rows, drop them; for numeric columns impute the median; sometimes add an "
     "'is_missing' flag because missingness itself carries information."),
    ("excel-b1", "technical", "Excel", "basic", "How do VLOOKUP/XLOOKUP and pivot tables help in reporting?",
     "Excel is still the most used analysis tool.", "lookups; exact match; pivot summarisation; refreshing data",
     "Each tool → a reporting example.",
     "XLOOKUP pulls a value from another table by key, like a customer's region; a pivot table then summarises sales "
     "by region and month in a few clicks, and refreshes when the data changes."),
    ("pbi-i1", "technical", "Power BI", "intermediate", "What is a data model in Power BI, and why use measures instead of calculated columns?",
     "Shows real BI tool understanding.", "star schema; relationships; DAX measures; filter context",
     "Data model → measures vs columns → an example.",
     "A star schema with fact and dimension tables related by keys; measures calculate at query time in the current "
     "filter context, so one 'Total Sales' measure works in every visual, unlike a stored column."),
    # Testing
    ("qa-b1", "technical", "Software testing", "basic", "What is the difference between verification and validation, and between smoke and regression testing?",
     "Testing vocabulary is asked in every QA interview.", "verification vs validation; smoke; sanity; regression",
     "Define each pair → an example.",
     "Verification checks we built it right (reviews, specs), validation checks we built the right thing (user "
     "needs). Smoke tests check a build is stable enough to test; regression tests check old features still work."),
    ("qa-i1", "technical", "Test automation", "intermediate", "What would you automate, and what would you keep manual?",
     "Shows judgement about test cost and value.", "test pyramid; stable vs changing features; ROI; exploratory testing",
     "The pyramid → criteria → examples of each.",
     "Automate stable, repeated, high-value checks — unit and API tests first, a few UI flows; keep exploratory and "
     "usability testing manual, as well as features still changing every sprint."),
    ("sel-i1", "technical", "Selenium", "intermediate", "How do you make Selenium tests reliable?",
     "Flaky UI tests are the main automation problem.", "explicit waits; stable locators; page-object model; test data",
     "Causes of flakiness → fixes → structure.",
     "Use explicit waits instead of sleeps, stable locators like data-testid, the page-object model for reuse, and "
     "independent test data so tests don't depend on each other."),
    ("js-a1", "programming", "JavaScript", "advanced", "How can a JavaScript single-page app leak memory, and how do you find it?",
     "Long-running front ends degrade when memory leaks.", "detached DOM nodes; listeners; closures; timers; heap snapshots",
     "Common causes → how to find them → how to fix them.",
     "Typical leaks are event listeners or timers never removed and closures holding large objects. I'd take heap "
     "snapshots in DevTools before and after a repeated action, look for growing detached nodes, and clean up in the "
     "component's teardown."),
    ("dsa-a2", "dsa", "Data structures & algorithms", "advanced", "Merge k sorted lists efficiently.",
     "Tests heaps and complexity reasoning.", "min-heap; O(N log k); divide and conquer alternative",
     "Brute force → heap solution → complexity → edge cases.",
     "Push the head of each list into a min-heap keyed by value; pop the smallest, append it and push its next node. "
     "Each of the N nodes costs O(log k), so O(N log k) time and O(k) extra space."),
    ("k8s-a1", "cloud", "Kubernetes", "advanced", "How do you roll out a new version with zero downtime on Kubernetes?",
     "Safe deployments are a core platform skill.", "rolling updates; readiness probes; maxSurge/maxUnavailable; rollback",
     "Strategy → probes → settings → rollback.",
     "Use a Deployment's rolling update with readiness probes so traffic only reaches ready pods, maxUnavailable 0 and "
     "a small maxSurge; watch the rollout and use kubectl rollout undo if error rates rise."),
    ("qa-a1", "technical", "Software testing", "advanced", "How would you design a test strategy for a system of microservices?",
     "Distributed systems need more than end-to-end tests.", "test pyramid; contract tests; test environments; observability",
     "Levels of testing → contracts between services → environments → monitoring in production.",
     "Unit tests inside each service, contract tests between consumers and providers so changes don't break callers, "
     "a few end-to-end journeys in a staging environment, and production monitoring with alerts as the last net."),
    ("stat-a1", "technical", "Statistics", "advanced", "What can go wrong when you stop an A/B test as soon as it looks significant?",
     "Peeking inflates false positives — a common analytics mistake.", "optional stopping; false positives; sequential testing; power",
     "The problem → why it happens → how to do it properly.",
     "Checking repeatedly and stopping at the first p < 0.05 raises the false-positive rate well above 5 %. Fix the "
     "sample size in advance from a power calculation, or use a sequential method designed for peeking."),
    # Behavioural
    ("beh-b1", "behavioural", "", "basic", "Tell me about yourself.",
     "Almost every interview opens with it; it sets the frame for the rest.",
     "present → past → future; relevance to the role; 60–90 seconds",
     "Who you are now → two relevant highlights → why this role.",
     "I'm a final-year Computer Science student. In my internship I built REST APIs for a payments dashboard, and my "
     "final-year project is a placement portal used by my department. I'm looking for a backend role where I can "
     "keep building APIs that people depend on."),
    ("beh-b2", "behavioural", "", "basic", "Tell me about a time you worked in a team.",
     "Most work is team work; they want your specific part.", "STAR; your own actions; outcome; what you learnt",
     "Situation → Task → Action (yours) → Result.",
     "In a three-person project the deadline moved forward a week. I split the work by feature, took the API "
     "myself and set up daily 15-minute check-ins; we shipped on time, and I learnt to agree interfaces early."),
    ("beh-i1", "behavioural", "", "intermediate", "Describe a difficult bug you fixed.",
     "Shows how you debug under uncertainty.", "reproduce; isolate; hypothesis; fix; prevent recurrence",
     "STAR, with the debugging steps in the Action.",
     "Reports were randomly wrong. I reproduced it with fixed data, found two requests updating the same row, fixed "
     "it with a transaction and added a test that runs the requests concurrently."),
    ("beh-i2", "behavioural", "", "intermediate", "Tell me about a time you disagreed with someone.",
     "Tests maturity and communication.", "listening; data over opinion; outcome; relationship",
     "Situation → how you raised it → how it was resolved → what you learnt.",
     "A teammate wanted to skip tests to meet a demo. I suggested testing only the payment path, which we did in an "
     "hour; the demo went fine and we added the rest after."),
    ("beh-a1", "behavioural", "", "advanced", "Tell me about a decision you made with incomplete information.",
     "Shows judgement and ownership.", "risk assessment; reversibility; communication; follow-up",
     "Context → options → why you chose → result → what you'd change.",
     "Choosing a database for a project without load data, I picked PostgreSQL because it was reversible behind a "
     "repository layer, told the team the assumption and set a load test as the checkpoint to revisit it."),
    # HR
    ("hr-b1", "hr", "", "basic", "Why do you want to work here?",
     "Shows you researched the company and the role.", "company's product; role fit; your goals",
     "What you know about them → why the role fits → what you'll bring.",
     "Base the answer only on what you have actually read about the company and the role; connect it to a skill from "
     "your résumé."),
    ("hr-b2", "hr", "", "basic", "What are your strengths and weaknesses?",
     "Tests self-awareness.", "a real strength with evidence; a real weakness with what you're doing about it",
     "Strength + example → weakness + improvement plan.",
     "A strength: I break problems down — my internship tickets were rarely reopened. A weakness: I used to take on "
     "too much; I now estimate tasks and say early when something won't fit."),
    ("hr-b3", "hr", "", "basic", "Where do you see yourself in five years?",
     "Checks ambition and fit with the role's path.", "growth; depth in the role; realistic goals",
     "Short-term learning → medium-term contribution → long-term direction.",
     "I want to become a strong backend engineer here: first owning features end to end, then designing services and "
     "mentoring newer engineers."),
    ("hr-i1", "hr", "", "intermediate", "Are you open to relocation, shifts or a service agreement?",
     "Many fresher roles have such conditions; answer honestly.", "honesty; constraints; questions to ask back",
     "Your honest position → any constraint → a question about the details.",
     "Answer truthfully for your own situation; ask for the specifics (location, duration, terms) before agreeing."),
    ("hr-i2", "hr", "", "intermediate", "What are your salary expectations?",
     "A common HR round question for experienced and fresher hires alike.", "market research; range; flexibility",
     "Research → a range → openness to the full package.",
     "Give a range based on your own research for this role and city, and say you're open to discussing the whole "
     "package."),
]

BANK: list[dict] = [
    {"id": q[0], "track": q[1], "skill": q[2], "level": q[3], "question": q[4], "why": q[5],
     "concepts": [c.strip() for c in q[6].split(";")], "structure": q[7], "example": q[8], "source": "bank"}
    for q in _Q
]
_IDS = [q["id"] for q in BANK]
assert len(set(_IDS)) == len(_IDS), "duplicate question ids"
assert all(q["skill"] == "" or tax.known(q["skill"]) for q in BANK), "unknown skill in the bank"
assert all(q["level"] in LEVELS and q["track"] in TRACKS for q in BANK)

FAMILY_SKILLS = {
    "Software engineering": ["Data structures & algorithms", "OOP", "DBMS", "Operating systems", "SQL", "REST APIs"],
    "Front-end & mobile": ["JavaScript", "React", "REST APIs", "Data structures & algorithms"],
    "Data & analytics": ["SQL", "Excel", "Statistics", "Data analysis", "Power BI"],
    "Data & ML": ["Machine learning", "Statistics", "Python", "SQL"],
    "Testing & QA": ["Software testing", "Test automation", "Selenium", "SQL"],
    "Cloud & DevOps": ["Linux", "Docker", "Kubernetes", "AWS", "CI/CD", "Networking"],
    "Embedded & hardware": ["C", "C++", "Operating systems"],
    "Business & finance": ["Excel", "Communication"],
    "General": ["Communication", "Data structures & algorithms"],
}


def _qid(text: str) -> str:
    return "gen-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def resume_questions(profile: dict, job_skills: list[str], level: str) -> list[dict]:
    """Questions built from the résumé itself — each quotes its evidence."""
    out: list[dict] = []
    for p in (profile.get("projects") or [])[:3]:
        title = p.get("title") or "your project"
        ev = "; ".join(p.get("bullets") or [])[:300]
        kind = {"academic_project": "academic project", "personal_project": "personal project"}.get(p.get("kind"), "project")
        qs = {"basic": f"Walk me through {title}. What problem did it solve and what was your part?",
              "intermediate": f"What was the hardest technical problem in {title}, and how did you solve it?",
              "advanced": f"If {title} had 100× more users, what would break first and how would you redesign it?"}
        out.append({"id": _qid(level + title), "track": "project", "skill": "", "level": level, "question": qs[level],
                    "why": f"Interviewers go deep on the projects you list; this is your {kind}.",
                    "concepts": ["the problem and who it was for", "your own contribution", "the technology choices "
                                 "and why", "a result you can verify"] + (["scaling", "bottlenecks"] if level == "advanced" else []),
                    "structure": "Context → your part → one technical decision → result → what you'd improve.",
                    "example": "Answer from what you really did; the evidence below is what your résumé says.",
                    "evidence": ev, "source": "resume"})
    for x in [e for e in profile.get("experience") or [] if e.get("bullets")][:2]:
        org = x.get("organisation") or x.get("title") or "your last role"
        first = x["bullets"][0]
        out.append({"id": _qid(level + org + first), "track": "resume", "skill": "", "level": level,
                    "question": f"At {org} you wrote: “{first[:140]}”. How did you approach it, and what did you learn?",
                    "why": "Every line of your résumé can be asked about; this one is from your "
                           + ("internship." if x.get("kind") == "internship" else "work experience."),
                    "concepts": ["the situation", "your actions", "measurable result (only real numbers)", "learning"],
                    "structure": "STAR: Situation → Task → Action → Result.",
                    "example": "Use your own facts; don't add numbers you can't back up.",
                    "evidence": first, "source": "resume"})
    skills = profile.get("skills") or {}
    for s in [s for s in job_skills if (skills.get(s) or {}).get("status") in ("demonstrated", "mentioned")][:4]:
        info = skills[s]
        ev = (info.get("evidence") or [{}])[0].get("text", "")
        shown = info.get("status") == "demonstrated"
        out.append({"id": _qid(level + "skill" + s), "track": "resume", "skill": s, "level": level,
                    "question": (f"Your résumé shows {s}: “{ev[:140]}”. What exactly did you build with it, and why "
                                 f"{s} rather than an alternative?") if shown else
                                (f"You list {s}. Describe something you have done with {s}."),
                    "why": "The posting asks for this skill and your résumé claims it — expect a follow-up."
                           + ("" if shown else " Your résumé lists it but doesn't show it in a project or job."),
                    "concepts": [f"how {s} was used", "a trade-off or limitation", "what you'd do differently"],
                    "structure": "Where you used it → what you did → a decision you made → result.",
                    "example": "Answer from your own experience.", "evidence": ev, "source": "resume"})
    return out


def question_set(profile: dict | None, reqs: dict | None, level: str = "basic", family: str = "") -> dict:
    """Questions for one résumé × one job at one level, grouped by track.
    Works with an empty profile (curated questions only) and with a job that
    names no skills (the job family's core topics)."""
    level = level if level in LEVELS else "basic"
    profile = profile or {}
    job_skills = list((reqs or {}).get("skills") or {})
    if not family:
        from job_requirements import role_family
        family = role_family((reqs or {}).get("title", ""))
    wanted = list(dict.fromkeys(job_skills + FAMILY_SKILLS.get(family, FAMILY_SKILLS["General"])
                                + [s for s, i in (profile.get("skills") or {}).items() if i.get("status") == "demonstrated"]))
    chosen: list[dict] = []
    seen: set[str] = set()
    for s in wanted:
        for q in BANK:
            if q["skill"] == s and q["level"] == level and q["id"] not in seen:
                reason = ("The posting asks for " + s) if s in job_skills else \
                         ("A core topic for " + family.lower() + " roles") if s in FAMILY_SKILLS.get(family, []) else \
                         ("Your résumé shows " + s)
                chosen.append({**q, "reason": reason, "requirement": ((reqs or {}).get("skills") or {}).get(s, {}).get("quote", "")})
                seen.add(q["id"])
    for q in BANK:
        if q["track"] in ("behavioural", "hr") and q["level"] == level and q["id"] not in seen:
            chosen.append({**q, "reason": "Asked in most interviews", "requirement": ""})
            seen.add(q["id"])
    gen = [q for q in resume_questions(profile, job_skills, level) if q["id"] not in seen]
    chosen += gen
    tracks: dict[str, list[dict]] = {}
    for q in chosen:
        tracks.setdefault(q["track"], []).append(q)
    ordered = {t: tracks[t] for t in TRACKS if t in tracks}
    return {"level": level, "family": family, "tracks": ordered, "count": len(chosen), "disclaimer": DISCLAIMER}
