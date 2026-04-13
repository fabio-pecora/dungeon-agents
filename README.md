# Tiny 2-Agent LLM Dungeon

A small CLI-first take-home project built around **real LLM-backed agents**.

The goal is not game complexity. The goal is trace quality, legibility, and lightweight architecture:
- 8x8 grid world
- 2 API-backed agents
- partial visibility
- delayed messaging
- 1 key
- 1 locked door
- 1 exit
- walls
- both agents must eventually exit
- strong structured traces

## What changed

This version uses the OpenAI API for actual turn-by-turn agent decisions.

Each acted turn makes one model call and expects a strict structured decision with:
- intent summary
- rationale
- one action only

The exact action set is:
- `move(direction)`
- `pick_up()`
- `unlock()`
- `send_message(text)`
- `wait()`

## Tech

- Python 3.11+
- OpenAI Python SDK
- Pydantic
- python-dotenv
- JSON / JSONL
- Markdown reports

## Setup

Create and activate a virtual environment.

Windows PowerShell:

```bash
python -m venv .venv
.venv\Scripts\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Create your environment file:

```bash
copy .env.example .env
```

Then edit `.env` and add your real API key:

```env
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5.2
OPENAI_TIMEOUT_SECONDS=45
OPENAI_MAX_RETRIES=2
```

## Run

Run one simulation:

```bash
python main.py run --output-dir outputs --seed 7
```

Run multiple simulations:

```bash
python main.py batch --runs 3 --output-dir outputs/batch --base-seed 100
```

Clean outputs:

```bash
python main.py clean --output-dir outputs
```

## Output files

Each run writes a folder under the output directory containing:
- `events.jsonl`
- `summary.json`
- `report.md`

## Trace design

The main trace record is one rich `agent_step` event per acted turn.

Each event includes:
- `observation`
- `belief_state`
- `belief_diagnostics`
- `messages`
- `decision`
- `action_execution`
- `outcome_assessment`
- `attribution`

This is meant to answer:
- what the agent saw
- what it believed
- whether that belief was stale or message-based
- what it heard or sent
- what it chose
- why it chose it
- what the engine actually did
- whether the issue came from the agent, communication, or the system

## Important note

This is intentionally small and submission-oriented.

It does **not** add:
- monsters
- combat
- health
- advanced pathfinding
- a web frontend
- a dashboard

The legibility layer is the JSONL trace plus a simple markdown report.
