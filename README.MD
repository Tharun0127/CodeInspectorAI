<div align="center">

# 🔍 CodeInspectorAI

### An autonomous AI code reviewer that reads your pull requests so humans don't have to read them twice.

Fetches a GitHub PR, reviews the diff and hunts for vulnerabilities **in parallel**,
then posts one structured, actionable review comment back on the PR.

[![tests](https://github.com/Tharun0127/CodeInspectorAI/actions/workflows/tests.yml/badge.svg)](https://github.com/Tharun0127/CodeInspectorAI/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.11%20|%203.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2-1C3C3C?logo=langchain&logoColor=white)](https://langchain-ai.github.io/langgraph/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Groq](https://img.shields.io/badge/Groq-gpt--oss--120b-F55036?logo=groq&logoColor=white)](https://groq.com/)
[![tests](https://img.shields.io/badge/tests-101%20passing-brightgreen)](#-testing)

[Architecture](#-architecture) • [Sample Output](#-sample-output) • [Quick Start](#-quick-start) • [Deploy](#️-deploy) • [Engineering Notes](#-engineering-notes) • [Testing](#-testing)

</div>

---

## 💡 Why this exists

Code review is the highest-leverage quality gate in software — and the first thing that
gets skipped when a team is shipping fast. A stale `eval()` on user input or a
string-concatenated SQL query slips through not because reviewers are careless, but
because they're reviewing their fourth PR before lunch.

CodeInspectorAI is a **multi-agent reviewer** that never gets tired, never skips the
security pass, and always posts the same structured feedback — in under a minute,
automatically, on every push.

---

## ✨ Features

| | Feature | What it means |
| :-: | --- | --- |
| ⚡ | **Parallel agents** | Code-quality review and security scanning run concurrently in the same LangGraph superstep — roughly half the wall-clock of a sequential chain |
| 🛡️ | **Defence in depth** | A deterministic regex sweep for leaked credentials runs *alongside* the LLM, so an obvious secret is caught even if the model misses it |
| ⚖️ | **Deterministic verdicts** | The LLM finds problems; **code** decides the outcome. A critical finding always means `changes_requested` — never a function of the model's mood |
| 🩹 | **Fail-soft by design** | Any node can die without killing the run. Lose the security scan and you still get the review, flagged `partial` with the error disclosed |
| 🔁 | **Idempotent comments** | Pushing 10 commits updates **one** comment in place instead of spamming ten |
| 🎯 | **Smart diff budgeting** | Lockfiles, minified bundles and binaries are stripped; what's omitted is named in the prompt so the model knows its view is partial |
| 🔌 | **Three entry points** | GitHub webhook, REST API, or CLI — same graph behind all of them |
| 🔒 | **Verified webhooks** | HMAC-SHA256 signature checking, draft-PR filtering, background execution so GitHub never waits on an LLM |

---

## 🏗 Architecture

Built as a **stateful directed graph** rather than a linear chain, so the two analysis
agents genuinely run in parallel and the workflow can abort early without burning tokens.

```mermaid
flowchart TD
    A["🌐 GitHub Webhook<br/>· REST API · CLI"] --> B["📥 Fetch Context"]
    B -->|"PR fetch failed"| Z["🏁 END"]
    B --> C["📝 Code Review Agent"]
    B --> D["🛡️ Security Agent"]
    C --> E["🧩 Summary"]
    D --> E
    E --> F["💬 Post Review"]
    F --> Z

    style A fill:#1f2937,stroke:#4b5563,color:#f9fafb
    style B fill:#1e3a8a,stroke:#3b82f6,color:#eff6ff
    style C fill:#065f46,stroke:#10b981,color:#ecfdf5
    style D fill:#7f1d1d,stroke:#ef4444,color:#fef2f2
    style E fill:#78350f,stroke:#f59e0b,color:#fffbeb
    style F fill:#4c1d95,stroke:#8b5cf6,color:#f5f3ff
    style Z fill:#1f2937,stroke:#4b5563,color:#f9fafb
```

> **Why the abort edge matters:** if the PR can't be fetched, a naive graph would still
> invoke both LLM agents on an empty diff and post a meaningless review. The conditional
> route short-circuits straight to `END` — zero wasted tokens.

### Node contracts

Designing the state *before* writing the nodes kept each one's responsibility honest:

| Node | Consumes | Produces |
| --- | --- | --- |
| **Fetch Context** | `owner`, `repo_name`, `pr_number` | `raw_diff`, `changed_files`, `commit_messages`, `pr_metadata`, `pr_intent`, `parsed_diff`, `file_summaries`, `language_detected` |
| **Code Review** ⇄ | `parsed_diff`, `file_summaries`, `pr_intent` | `review_comments`, `code_quality_score`, `suggestions` |
| **Security** ⇄ | `parsed_diff`, `file_summaries`, `language_detected` | `security_issues`, `severity_levels`, `vulnerable_lines` |
| **Summary** | all findings | `final_summary`, `overall_verdict`, `formatted_comment`, `key_recommendations` |
| **Post Review** | `formatted_comment` | `posted_comment_url`, `review_status` |

<sub>⇄ = runs concurrently</sub>

---

## 📸 Sample output

Given a diff that introduces four classic vulnerabilities, here's what actually lands
on the PR:

> ## 🤖 CodeInspectorAI Review
>
> 🔴 **Changes requested** &nbsp;•&nbsp; Quality score **3.0/10** &nbsp;•&nbsp; 1 file reviewed &nbsp;•&nbsp; 4 security issues (**2 high/critical**)
>
> This change adds a user-lookup helper, but the query is assembled by concatenating the
> raw `username` into SQL, which is directly exploitable. The `run_cmd` helper hands
> unsanitised input to `os.system`, and the database password is committed in plaintext.
>
> ### 🔐 Security
>
> | | Severity | Location | Issue | Recommendation |
> |---|---|---|---|---|
> | 🔴 | Critical | `app/db.py:6` | SQL injection via string concatenation of user-supplied `username` | Use a parameterised query: `cur.execute("SELECT * FROM users WHERE name = ?", (username,))` |
> | 🔴 | Critical | `app/db.py:10` | Command injection: user-controlled `name` concatenated into `os.system` | Use `subprocess.run([...])` with an argument list and validate the host |
> | 🟠 | High | `app/db.py:3` | Hardcoded database password in source | Move to an environment variable and rotate the credential |
> | 🟡 | Medium | `app/db.py:13` | Reflected XSS: user comment inserted into HTML unescaped | Escape with `html.escape()` or use the template engine's autoescaping |
>
> ### 📝 Code review
>
> - 🔴 **Critical** `app/db.py:6` — the query concatenates the raw `username` value into the SQL string
>   - _Suggestion:_ switch to parameter binding
> - 🟡 **Warning** `app/db.py:13` — the `comment` string is inserted directly into HTML without escaping
>
> ---
> <sub>Generated by CodeInspectorAI — an automated reviewer. Treat its findings as suggestions, not gospel.</sub>

And on a clean PR, it says so and gets out of the way — `✅ Approved`, no invented nitpicks.

---

## 🚀 Quick Start

```bash
git clone https://github.com/Tharun0127/CodeInspectorAI.git
cd CodeInspectorAI

python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt
copy .env.example .env            # cp on macOS / Linux
```

Add your keys to `.env`, then review any public PR without posting anything:

```bash
python -m app.cli pallets/flask 6136 --dry-run
```

### Configuration

| Variable | Required | Default | Notes |
| --- | :-: | --- | --- |
| `GROQ_API_KEY` | ✅ | — | [console.groq.com/keys](https://console.groq.com/keys) |
| `GROQ_MODEL` | — | `openai/gpt-oss-120b` | Any Groq chat model |
| `GITHUB_TOKEN` | to post | — | Fine-grained PAT · `Pull requests: read & write` |
| `GITHUB_WEBHOOK_SECRET` | recommended | — | Verifies `X-Hub-Signature-256` |
| `POST_COMMENTS` | — | `true` | `false` = run everything, post nothing |
| `MAX_DIFF_CHARS` | — | `15000` | Diff budget per LLM node |

> ⚠️ Groq retires models regularly. If a run fails with a model error, list what your
> account can use:
> ```bash
> curl -H "Authorization: Bearer $GROQ_API_KEY" https://api.groq.com/openai/v1/models
> ```

---

## 🎮 Usage

### CLI

```bash
python -m app.cli owner/repo 42                 # review and post
python -m app.cli owner/repo 42 --dry-run       # review, print, post nothing
python -m app.cli owner/repo 42 --save out.md   # save the markdown
```

### API server

```bash
uvicorn app.main:app --reload --port 8000
```

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Readiness + which credentials are configured |
| `POST /review` | Manual trigger — `{"owner", "repo_name", "pr_number"}` |
| `POST /webhook` | GitHub `pull_request` webhook receiver |
| `GET /docs` | Interactive OpenAPI docs |

### GitHub webhook

Point a repository webhook at `https://<your-host>/webhook`:

- **Content type** → `application/json`
- **Secret** → same value as `GITHUB_WEBHOOK_SECRET`
- **Events** → *Pull requests* only

Reviews fire on `opened`, `reopened`, `synchronize` and `ready_for_review`. Drafts are
skipped. The endpoint returns immediately and the review runs in the background, so
GitHub's 10-second webhook timeout is never at risk.

---

## ☁️ Deploy

The service ships as a Docker image, so it runs on any container host. It needs a
**long-running** process: reviews run as background tasks after the webhook returns, so
serverless platforms that freeze the function after the response (Vercel, Lambda) will
silently drop them.

### Docker (anywhere)

```bash
docker build -t codeinspector-ai .
docker run -d -p 8000:8000 --env-file .env codeinspector-ai
curl localhost:8000/health
```

The container listens on `$PORT` (default `8000`), runs as a non-root user and has a
built-in `HEALTHCHECK` against `/health`.

### Render (one click)

`render.yaml` is a Render Blueprint. In the Render dashboard choose **New → Blueprint**,
pick this repo, and fill in `GROQ_API_KEY` and `GITHUB_TOKEN` when prompted.
`GITHUB_WEBHOOK_SECRET` is generated for you; copy it from the service's
**Environment** tab into the GitHub webhook settings. Then point the webhook at
`https://<service>.onrender.com/webhook`.

> Render's free plan sleeps after ~15 minutes idle. The first webhook after a sleep
> wakes it but can exceed GitHub's 10-second timeout, so GitHub marks that delivery as
> failed even though the review still runs. Use a paid instance for reliable delivery.

### Railway / Fly.io / a VPS

All of these detect the `Dockerfile` automatically. Set the same environment variables
as in `.env.example`, expose the port the platform assigns via `$PORT`, and use `/health`
as the health check path.

### Web UI & dashboard

The service also serves a landing page at `/`, with a live "paste a PR URL" demo, and a
review dashboard at `/dashboard`.

- **Demo reviews** are always dry runs (nothing is posted), only work on public repos,
  and are rate-limited per visitor (`DEMO_RATE_LIMIT`, default 5/hour). Set
  `DEMO_ENABLED=false` to turn the demo off.
- **The dashboard is public but only shows public-repo reviews.** Private-repo reviews
  appear only after you unlock the dashboard with `DASHBOARD_TOKEN`.
- **History** is stored in SQLite under `DATA_DIR` (`/app/data` in the container).
  Render's free plan has no persistent disk, so history resets on each deploy or restart.
  Attach a disk mounted at `/app/data` on a paid plan to keep it.

### Custom prompts in production

`prompts/` is git-ignored, so deploys built from the repo use the built-in fallback
prompts. To use your own, either build the image locally (a local `prompts/` folder is
copied in), or mount the files and set `PROMPTS_DIR`:

```bash
docker run -d -p 8000:8000 --env-file .env \
  -v "$PWD/prompts:/prompts:ro" -e PROMPTS_DIR=/prompts codeinspector-ai
```

---

## 🤖 GitHub App: let anyone install it

Run as a **GitHub App**, CodeInspectorAI works like any app from the Marketplace:

- A developer clicks **Install**, picks which repos it may see, and approves.
- GitHub then sends the app every pull request event on those repos. Nobody sets up a webhook.
- For each review, the server swaps the app's private key for an **installation token**
  that only works on that installation's repos and expires within an hour.
  Nobody shares a password or personal token.
- Comments appear from **`<your-app>[bot]`**.
- Uninstalling (GitHub → Settings → Applications) cuts access immediately, and the server
  forgets any cached token for that installation.

### Register the app (one time, ~5 minutes)

1. Deploy the server first (see [Deploy](#️-deploy)) so you have its URL.
2. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**
   (or under your organisation's settings to own it as an org) and fill in:

   | Field | Value |
   | --- | --- |
   | GitHub App name | e.g. `CodeInspectorAI` (its URL slug becomes `GITHUB_APP_SLUG`) |
   | Homepage URL | `https://<your-host>/` |
   | Setup URL | `https://<your-host>/` (tick **Redirect on update**) |
   | Webhook URL | `https://<your-host>/webhook` (**Active** ticked) |
   | Webhook secret | a long random string, the same value as `GITHUB_WEBHOOK_SECRET` |
   | Repository permissions | **Pull requests: Read & write** · **Contents: Read-only** · Metadata: Read-only (automatic) |
   | Subscribe to events | **Pull request** |
   | Where can it be installed? | **Any account** (or *Only this account* while testing) |

3. Create it, then on the app's page note the **App ID** and click
   **Generate a private key** (downloads a `.pem` file).
4. Set these on the server and redeploy:

   ```bash
   GITHUB_APP_ID=123456
   GITHUB_APP_SLUG=codeinspectorai          # from https://github.com/apps/<slug>
   GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----"
   GITHUB_WEBHOOK_SECRET=<the webhook secret from step 2>
   ```

   The key can be pasted with real newlines, on one line with `
` separators, or
   mounted as a file via `GITHUB_APP_PRIVATE_KEY_PATH` (e.g. a Render Secret File at
   `/etc/secrets/github-app.pem`).

5. Check `GET /health` shows `"github_app_configured": true`. The landing page now
   shows **Install on GitHub**, linking to `https://github.com/apps/<slug>/installations/new`.

`GITHUB_TOKEN` becomes optional: it's only used for repos where the app isn't installed,
for example the public "try it" demo. Setting one raises GitHub's rate limit for those
reads from 60 to 5,000 per hour.

---

## 🧠 Engineering notes

The interesting decisions weren't about prompting — they were about what happens when
things go wrong.

<details>
<summary><b>⚖️ The LLM finds problems; code decides the verdict</b></summary>

<br/>

An LLM asked to both find issues *and* rule on them will happily approve a PR it just
told you contains a critical vulnerability. So `decide_verdict()` in `graph/utils.py`
owns the outcome:

```python
if any(issue.severity in ("high", "critical") for issue in security_issues):
    return "changes_requested"
```

Same findings in, same verdict out — every time. The model's job is evidence, not judgement.

</details>

<details>
<summary><b>🩹 Fail-soft nodes: partial results beat no results</b></summary>

<br/>

Every node catches its own exceptions and appends to a reducer-backed `errors` list
instead of raising. If the security scan dies, the review still posts — with the failure
disclosed in a collapsible block and `review_status` downgraded to `partial`.

The one exception is a failed PR fetch, which routes straight to `END`: there is nothing
to review, so spending two LLM calls to find that out would be waste.

</details>

<details>
<summary><b>🔧 Surviving flaky structured output</b></summary>

<br/>

Open-weight models on Groq intermittently emit an invalid tool call
(`400 — attempted to call tool 'json'`), which killed an entire agent mid-run. This
surfaced during live testing, not code review.

`app/llm.invoke_structured()` now degrades gracefully: **tool calling → retry → JSON mode
with the schema rendered into the prompt**. Severity fields are normalised too, so a
model answering `"high"` where the schema wants `"critical"` gets mapped instead of
throwing away an otherwise valid finding.

</details>

<details>
<summary><b>🛡️ Regex and LLM, not regex or LLM</b></summary>

<br/>

Credential patterns are matched deterministically against **added lines only** — so
pre-existing secrets aren't re-reported on every push — then merged with the LLM's
findings and deduplicated.

The regexes are tested against real-world shapes (`DB_PASSWORD`, `apiKey`, AWS keys, PEM
blocks) *and* against false positives like `password = os.environ["DB_PASSWORD"]`, which
must **not** fire.

</details>

<details>
<summary><b>📏 Honest context budgeting</b></summary>

<br/>

Rather than silently truncating a diff at 15,000 characters and letting the model review
a fragment while believing it saw everything, `build_diff_context()` packs whole files
and appends an explicit manifest:

```
--- NOT SHOWN IN THIS DIFF ---
- vendor/bundle.js (too large for the context budget)
```

The model knows what it doesn't know.

</details>

<details>
<summary><b>🔁 One comment, not twenty</b></summary>

<br/>

Every posted review carries an invisible `<!-- codeinspector-ai-review -->` marker. On
the next push the bot finds its own previous comment and **edits it in place**, so a
20-commit branch has exactly one always-current review instead of a wall of noise.

</details>

---

## 🧪 Testing

```bash
python -m pytest          # 101 tests · ~4s · zero network calls
```

<div align="center">

| Suite | Covers |
| --- | --- |
| `test_utils.py` | Diff parsing, language detection, verdict rules, budgeting |
| `test_nodes.py` | Each node's success **and** failure path, secret regexes |
| `test_graph.py` | Compiled graph end-to-end, parallel fan-out, abort route |
| `test_llm.py` | Structured-output retry/fallback, severity normalisation |
| `test_api.py` | Endpoints, webhook filtering, HMAC signature verification |
| `test_github.py` | Diff fallback, metadata extraction, comment replacement |

</div>

GitHub and the LLM are both faked, so the suite is **fully offline, deterministic and
free to run** — it needs no API keys and burns no tokens. Failure paths are tested as
carefully as happy paths, because a reviewer that crashes on a flaky API is a reviewer
nobody trusts.

---

## 🗂 Project structure

```
├── app/
│   ├── config.py       Environment/settings, prompt loading
│   ├── llm.py          Groq factory + resilient structured-output helper
│   ├── github.py       PR fetch, diff retrieval, posting, webhook signatures
│   ├── main.py         FastAPI app (webhook + REST trigger)
│   └── cli.py          Command line entry point
├── graph/
│   ├── state.py        ReviewState + Pydantic output schemas
│   ├── utils.py        Diff parsing, language detection, verdict rules
│   ├── graph.py        LangGraph wiring
│   └── nodes/          context · review · security · summary · post_review
└── tests/              101 offline tests
```

> **Note:** the system prompts in `prompts/*.md` are loaded at runtime and are not
> published in this repository. Each node falls back to a built-in default prompt when
> the file is absent, so the project runs and the full test suite passes without them.

---

## 🛠 Tech stack

<div align="center">

| Layer | Choice | Why |
| --- | --- | --- |
| **Orchestration** | LangGraph | Stateful graphs give real parallelism and conditional routing, which a linear chain can't |
| **Inference** | Groq · `gpt-oss-120b` | Sub-second latency keeps a full review under a minute |
| **Validation** | Pydantic v2 | Schema-bound LLM output — no fragile regex parsing of prose |
| **API** | FastAPI | Async, background tasks, free OpenAPI docs |
| **Integration** | PyGithub + REST | PyGithub for objects, raw REST for the diff media type it doesn't expose |

</div>

---

## 🗺 Roadmap

- [ ] Inline review comments anchored to diff positions (not just a summary comment)
- [ ] Repository-aware context via embeddings, so the reviewer sees beyond the diff
- [ ] Configurable per-repo rules through a `.codeinspector.yml`
- [ ] Review-quality feedback loop from 👍/👎 reactions on posted comments
- [x] Dockerfile + one-click deploy

---

<div align="center">

**Built by [Tharun](https://github.com/Tharun0127)**

If this was useful, a ⭐ is appreciated.

</div>
