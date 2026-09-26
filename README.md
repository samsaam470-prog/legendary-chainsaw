# Multi-Demo Chatbot Backend

A single FastAPI backend for five isolated customer-service demos:

- `dentist`
- `real_estate`
- `restaurant`
- `hvac`
- `lawyer`

The backend is interface-agnostic, so the same API can serve an Android app, website, or future clients.

## Architecture

Each chat request follows this retrieval pipeline:

1. Receive the customer message.
2. Normalize spelling, typos, abbreviations, chat expressions, and common variants.
3. Validate and identify the requested `demo_id`.
4. Check exact FAQ.
5. Check similar FAQ.
6. Check previously cached answers.
7. Check demo-specific structured business data.
8. Check demo-specific fact sheets.
9. Check relevant demo-specific knowledge paragraphs/custom knowledge.
10. Build a factual context packet.
11. Send only the current message plus relevant context/instructions and a small recent conversational window to Agnes 2.5 Flash through NaraRouter.
12. Store the final response in the conversation history.
13. Cache the response only when it is grounded in a retrieved source; generated answers are never automatically promoted to permanent factual knowledge.
14. Return the response.

The full conversation history is stored internally in SQLite, separated by `demo_id` and `session_id`. The complete history is never sent to the LLM on every request.

## Demo isolation

All bundled knowledge is stored below `data/<demo_id>/`. Runtime tables also carry `demo_id` and all retrieval/cache/conversation queries are scoped to it.

The five demos have their requested approximate dataset sizes:

- Dentist: 20 services, 120 FAQs, 120 fact sheets, 1,200 knowledge paragraphs, 3 instructions.
- Real estate: 30 listings, 120 FAQs, 120 fact sheets, 1,200 knowledge paragraphs, 3 instructions.
- Restaurant: 40 menu items, 120 FAQs, 120 fact sheets, 1,200 knowledge paragraphs, 3 instructions.
- HVAC: 20 services, 120 FAQs, 120 fact sheets, 1,200 knowledge paragraphs, 3 instructions.
- Lawyer: 20 legal services, 120 FAQs, 120 fact sheets, 1,200 knowledge paragraphs, 3 instructions.

A reusable normalization dictionary contains about 5,000 entries in `data/normalization.json`.

## Lawyer safety behavior

The lawyer demo provides general legal information only. It does not claim to be a lawyer, does not create an attorney-client relationship, and is instructed not to invent jurisdiction-specific rules, deadlines, or outcomes. For case-specific legal advice, it recommends consulting a qualified local attorney.

## Install and run locally

Python 3.11+ is recommended.

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env
```

Set real environment variables in your shell or your local environment. Never commit secrets.

For local development:

```bash
uvicorn main:app --reload
```

The API will be available at the local Uvicorn address.

## Required environment variables

```text
BYNARA_API_KEY=
ADMIN_USER=admin
ADMIN_PASS=
JWT_SECRET=
```

Optional:

```text
CORS_ORIGINS=*
VERCEL=
```

`BYNARA_API_KEY` must contain your real NaraRouter API key. `ADMIN_PASS` should be a strong password you choose. `JWT_SECRET` should be a long random secret.

## NaraRouter

The backend calls:

`https://router.bynara.id/v1/chat/completions`

with model:

`agnes-2.5-flash`

The API key is read only from `BYNARA_API_KEY`; no credential is hardcoded in the project.

## Health check

`GET /health`

Example response:

```json
{"status":"ok","api":"running","database":true}
```

## Chat

`POST /chat`

Example body:

```json
{
  "demo_id": "dentist",
  "session_id": "demo-user-001",
  "message": "how much is a cleaning?"
}
```

The response includes the final answer, normalized query, and the retrieval source type.

## Admin

1. `POST /admin/login`
2. Send the returned JWT as `Authorization: Bearer <token>`.
3. Use the admin endpoints for demo selection, FAQs, knowledge, fact sheets, structured records, conversations, cache, and settings.

Knowledge can be added by direct text, website URL, or PDF. Every custom record is stored with a `demo_id`.

## Vercel

`main.py` exposes:

```python
app = FastAPI(...)
```

There is no mandatory server startup block and no hardcoded `PORT`. The included `vercel.json` uses Vercel's Python runtime.

Important persistence note: Vercel serverless storage is not a durable database. When `VERCEL` is set, this demo uses `/tmp/chatbot.db`. **Vercel /tmp SQLite storage is ephemeral and is not suitable for permanent production data.** This is only the temporary storage solution for the demo deployment. Local execution continues to use `storage/chatbot.db`. The retrieval/data files remain bundled with the deployment.

In Vercel Project Settings, add:

- `BYNARA_API_KEY` = your real NaraRouter API key
- `ADMIN_USER` = your chosen admin username
- `ADMIN_PASS` = your chosen strong admin password
- `JWT_SECRET` = your chosen long random signing secret
- optionally `CORS_ORIGINS` = your frontend origin(s)

Do not put real secrets into the ZIP or Git repository.

## Project structure

```text
backend/
├── main.py
├── requirements.txt
├── README.md
├── vercel.json
├── .env.example
├── data/
│   ├── normalization.json
│   ├── dentist/
│   ├── real_estate/
│   ├── restaurant/
│   ├── hvac/
│   └── lawyer/
└── storage/
```

The `storage/` directory is intentionally kept empty in the packaged project; local SQLite is created there on first run.

## Security notes

- No API key, production password, or private token is bundled.
- Admin authentication uses a JWT signed with `JWT_SECRET`.
- Password comparison uses constant-time comparison.
- Demo data and runtime records are always scoped by `demo_id`.
- CORS is configurable.
- LLM prompts do not include the full conversation history.
- LLM-generated answers are not automatically written into the permanent knowledge library.

## Voice endpoints

The backend includes simple placeholders:

- `POST /voice/transcribe`
- `POST /voice/synthesize`

They are intentionally provider-neutral and return a placeholder response until a speech provider is connected.
