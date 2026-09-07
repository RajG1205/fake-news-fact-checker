# VerifyAI — AI Fact-Checking Platform

VerifyAI is an evidence-assisted fact-checking web app. Users submit a claim and the system retrieves live web evidence, analyzes it with an LLM, and returns a transparent verdict with supporting sources.

## Product decisions
- No user accounts or authentication.
- 5 fact-check questions per IP address per day.
- Additional burst protection: 12 requests/minute on `/chat`.
- Breaking news endpoint is limited to 30 requests/minute.
- No file upload, file analysis, developer IDs, or unnecessary dashboard features.
- Local browser history is intentionally not stored; the product stays privacy-light.
- Results show evidence and uncertainty rather than pretending AI is an absolute source of truth.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open `http://localhost:5000`.

## Render
Create the service from `render.yaml` and add `GROQ_API_KEY`, `TAVILY_API_KEY`, and optionally `NEWS_API_KEY` in Render's Environment settings.

`RATELIMIT_STORAGE_URI=memory://` is appropriate for one Render instance. If you scale to multiple instances, use a shared Redis-compatible storage URL so the daily quota is global.

## Security
The application never receives API keys from the browser. Keep all provider keys server-side.

If an API key was ever committed to a repository or shared publicly, rotate it at the provider and replace it in Render.
