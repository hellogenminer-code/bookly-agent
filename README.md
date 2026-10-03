# Bookly AI Support Agent — Paige

A customer support AI agent for **Bookly**, a fictional bookstore. Built for Decagon's take-home: *"Design an AI Agent for Customer Support."*

**Thesis:** a great support agent resolves the routine on its own from tool-backed facts, and hands the ambiguous to a human with full context. Every judgment call lives in code, never in the model's mood.

## The demo

Open `/split`: the bookstore chat on the right, the live agent dashboard on the left.

**The Kim journey** — one continuous conversation:

1. Public questions (hours, cafe, inventory) are answered with **no login** (step-up authentication)
2. Kim browses journals and Princess Bride stock via catalog search
3. She places an order and is asked for her email *only at that moment* — the order is written to Supabase
4. She checks order status — no repeated login
5. She requests a return → clarifying question ("why?") → $166.99 ≥ $150 → a **real escalation ticket** is filed and the order is marked `awaiting_review`

The bot never promises an outcome a human must decide: the order goes "on hold," not "canceled," and the refund is never pre-approved.

**What the dashboard shows, live:** turn-by-turn memory with CSAT badges, a rolling conversation summary, topic volume reporting, and the escalation ticket — all updating as the chat happens.

## Architecture

```
Browser (bookstore site + Paige chat widget)
  │  HTTP
Flask app (web.py) — serves pages, runs the agent loop
  │  chat completions (OpenAI gpt-4o-mini)
  │  MCP over stdio (one subprocess per turn)
MCP server (server.py) — the ONLY thing that touches the database
  │  15 narrow tools; business rules (e.g. the $150 threshold) enforced here
Supabase (Postgres)
  ├── customers     — profile columns + one jsonb `details`
  │                     (orders, preferences, interactions)
  └── escalations   — human work queue
```

The agent never receives database credentials. The MCP tool layer is the entire security and business-rule boundary. No LangChain/LangGraph — the agent loop is hand-written so every API call and orchestration step is visible.

## Always-on machinery

Deterministic, in code, never at the model's discretion:

- Every turn is appended to the customer's history with a **CSAT score (1–5)** and **topic tags** (two-level taxonomy, e.g. `Order › return_request`)
- A **living conversation summary** rewrites every turn from the full turn log, with a deterministic state line (latest topic, current CSAT, auth status) — visible on the dashboard whether or not a ticket is ever filed
- Pre-login turns are tracked as an anonymous **guest session**, then attached to the customer record at login (tagged `before login`)
- Past visits load into the prompt on login — the agent remembers
- **Three escalation triggers, one ticket:** the $150 rule, CSAT dropping to ≤ 2, or the customer asking for a human. Ticket **priority follows CSAT** (1–2 → high, 3+ → normal), the score is recorded on the ticket, and a later dip **bumps the open ticket** instead of filing a second one. Dedupe works both directions: if a low-CSAT ticket is already open when the $150 rule fires, the policy escalation is **folded into it** — the trigger accumulates (`low_csat,policy_threshold`), the order and amount are attached, and the handoff note is rebuilt to cover both reasons.

## Project structure

```
bookly-agent/
├── server.py          # MCP tool server — the only DB access
├── web.py             # Flask app + hand-written agent loop
├── agent.py           # CLI version of the agent loop
├── test_server.py     # MCP tool smoke tests
├── index.html         # Bookly site + Paige chat widget
├── dashboard.html     # Live "show my work" dashboard
├── split.html         # Side-by-side demo layout (/split)
├── bookstore-bg.png   # Chat page backdrop
├── demo-prep.sql      # Escalations table + Kim reset for recording
├── requirements.txt
├── .env               # your secrets — never committed
├── .env.example       # template
└── easter-egg/        # the Zork easter egg (see below)
```

## Setup & run

Prerequisites: Python 3.13, a Supabase project, an OpenAI API key.

```powershell
# 1. Clone and enter the folder
git clone <your-repo-url>
cd bookly-agent

# 2. Virtual environment
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3. Install
pip install -r requirements.txt

# 4. Secrets — copy .env.example to .env and fill in:
#    SUPABASE_URL=https://your-project.supabase.co   (root URL, no /rest/v1/)
#    SUPABASE_SERVICE_KEY=sb_secret_...               (API settings page)
#    OPENAI_API_KEY=sk-...
```

**Database** — in the Supabase SQL Editor:

```sql
-- customers: identity columns + one jsonb details column
create table customers (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email text unique not null,
  phone text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz default now()
);
```

Then run `demo-prep.sql` (creates the `escalations` table and resets the demo customer). Seed your demo customers; Kim Johnson (`kimjohnson3183@gmail.com`) starts with:

```json
{"orders": [], "preferences": {"format": "paperback", "notifications": "email", "genres": ["fantasy", "adventure"]}, "interactions": []}
```

```powershell
# 5. Smoke-test the tools (optional)
python test_server.py

# 6. Run
python web.py
```

| Page | URL |
|---|---|
| Chat | http://127.0.0.1:5000 |
| Dashboard | http://127.0.0.1:5000/dashboard |
| Split-screen (for recording) | http://127.0.0.1:5000/split |

**Before recording:** run `demo-prep.sql` in the SQL Editor to reset Kim and clear rehearsal tickets.

## Key decisions

1. **Real database + real tool layer, not mocks.** The brief allows mocking; the differentiator is live Supabase behind a narrow MCP API.
2. **Rules in code, not prompts.** The $150 threshold, the CSAT trigger, memory writes, and topic tagging are deterministic — the model can't forget, skip, or renegotiate them.
3. **Step-up authentication.** Public questions need no login; account actions trigger it once per conversation, never again.
4. **Escalation is a structured handoff, not a dead end.** A real ticket — narrative, escalation reason, recommended next step, current CSAT — lands in the work queue, with priority driven by the customer's satisfaction and one ticket per conversation. That's the seam where Zendesk plugs in.
5. **Every conversation becomes reportable data.** Per-turn topics feed volume reporting: turns and CSAT by topic, escalation rate, disposition — the analytics a CX team actually wants.

## What I'd do differently (production)

Server-side session/auth enforcement instead of prompt-driven step-up, RLS tenant isolation, a real ticketing integration (Zendesk/ServiceNow) behind the escalations queue, an eval harness for the topic/CSAT classifier, and PII redaction before any text leaves the trust boundary.

## The easter egg

A tiny "zork" link hides in the bottom-right corner of the chat. Click it and the bookstore becomes a playable Return-to-Zork-style text adventure: a deterministic parser for everything the game must get right, with an LLM narrator covering the long tail of everything a player might type. See `easter-egg/README_ZORK.md` for the story behind it.
