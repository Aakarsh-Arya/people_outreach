# IIM Udaipur Alumni Outreach Pipeline

Automated, LLM-powered pipeline for researching IIM Udaipur alumni and generating personalized outreach emails — built by **Aakarsh Arya** (MBA Class of 2025).

---

## Overview

A 3-phase pipeline that resolves alumni contact information, researches their professional profiles via an LLM agent, generates personalized emails, and sends them through Gmail.

```
Phase 1 (Contact Resolution)     Phase 2 (Research + Email Gen)     Phase 3 (Sending)
┌─────────────────────────┐      ┌──────────────────────────────┐   ┌──────────────────────┐
│ AlmaConnect CSV          │      │ 2A: LLM Research Agent       │   │ Google Apps Script    │
│   ↓                     │      │   Qwen 3 Max + Web Search    │   │   GmailApp.sendEmail  │
│ Google People API        │ ──►  │   ↓                          │ ──►│   Batch of 5          │
│   ↓                     │      │ 2B: Email Generation          │   │   Random jitter       │
│ Google Sheet (populated) │      │   Confidence-gated hooks     │   │   Quota guard         │
└─────────────────────────┘      └──────────────────────────────┘   └──────────────────────┘
```

### Phase 1 — Contact Resolution
- Parses either the normalized Phase 0 CSV or the raw AlmaConnect export already in this repo.
- Looks up `@iimu.ac.in` emails via Google People API.
- Falls back to guessed email pattern (`firstname.lastname.YEAR@iimu.ac.in`).
- Writes resolved contacts to a Google Sheet.
- Rows returned by Google People API are marked as `Email_Source=people_api`; unresolved multi-match lookups are marked `ambiguous`; only non-`people_api` rows need manual review or retry.

### Phase 2 — LLM Research & Email Generation
- **2A (Research Agent):** Sends alumni details to Qwen 3 Max with a detailed "Alum Search Skill" system prompt. The LLM performs web searches, cross-verifies identity, and returns a structured profile with confidence scoring.
- **2B (Email Generation):** Generates personalized Subject + Body using confidence-gated hooks (High → full career details, Medium → alumni-only, Low → generic template). Strict anti-hallucination rules enforced.

### Phase 3 — Apps Script Sending
- Runs inside the Google Sheet's Apps Script editor (not Python).
# People Outreach — IIM Udaipur Alumni Pipeline

Automated alumni outreach pipeline built by Aakarsh Arya (MBA 2025, IIM Udaipur).

## What it does
1. **Phase 1** — Resolves alumni email addresses via Google People API from an AlmaConnect CSV export
2. **Phase 2** — Researches each alumnus via Tavily web search + Gemini 2.5 Flash, then generates a personalized outreach email
3. **Phase 3** — Sends emails in batches via Gmail using Google Apps Script

## Tech Stack
- Python 3, Google AI Studio (Gemini 2.5 Flash), Tavily Search API
- Google Sheets API, Google People API, Google Apps Script
- OpenAI-compatible client (for Gemini bridge)

## Setup
1. Clone the repo
2. Copy .env.local.example to .env.local and fill in your API keys
3. Place your credentials.json from Google Cloud in the project root
4. Run: pip install -r requirements.txt
5. Run: python main.py test

## Commands
| Command | What it does |
|---|---|
| python main.py test | Verify API connections |
| python main.py phase1 iimu_2025.csv | Resolve alumni emails → Google Sheet |
| python main.py phase2-sheet 3 | Generate emails for 3 rows (spot check) |
| python main.py phase2-sheet 80 | Full batch run |
| python main.py research "Name" | Spot-test research for one alumnus |

## Notes
- Never commit .env.local, credentials.json, token.json, or any alumni CSV files
- See _agent.md for full architecture and current status
3. Either rename it to `credentials.json` and place it in the project root, or keep the downloaded `client_secret_*.json` filename there.
