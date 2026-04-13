# Dungeon Agents Take-Home

A small 2-agent dungeon simulation built for the **Dungeon Agents** take-home. The project is intentionally scoped around trace quality and legibility, not game complexity.

The core idea is simple:

- two agents explore a small dungeon under partial visibility
- they communicate through delayed messages
- one agent must get the key and unlock the door
- both agents must eventually reach the exit
- every step is logged in structured form so a human can understand what happened, why it happened, and what should change next

## What this project includes

### Simulation
- 8x8 grid world
- 2 agents
- partial visibility
- delayed messaging
- 1 key
- 1 locked door
- 1 exit
- interior walls
- turn-based loop
- one action per turn

### Actions
Each agent can take exactly one of these actions per turn:
- `move(direction)`
- `pick_up()`
- `unlock()`
- `send_message(text)`
- `wait()`

### Trace / logging
Each agent step exports a structured event with:
- observation
- belief state
- belief diagnostics
- messages delivered and sent
- decision
- action execution
- outcome assessment
- attribution

### Legibility layer
Each run produces:
- `events.jsonl`
- `summary.json`
- `report.md`

The markdown report is the human-facing diagnosis layer and is built to answer:
1. what happened
2. why it happened
3. what should change next

## Design choices

### 1. Kept the world intentionally small
The assignment says to keep the simulation simple and spend effort on traces and diagnosis instead of game design. This project follows that directly by using a minimal grid, a single key, a single locked door, a single exit, and a small wall layout.

### 2. Real LLM usage, but deterministic movement policy
One important lesson from development was that letting the LLM freely control navigation made the run fragile. For the final rescue version:

- the LLM is still used for short intent summaries and rationales
- movement policy is deterministic and finish-oriented
- communication remains explicit and structured
- the system prioritizes reliability and legibility over smart-looking wandering

This was a deliberate tradeoff to make the run more stable under the time limit while preserving meaningful AI behavior.

### 3. Structured short messages
Messages are intentionally short and machine-readable, for example:
- `KEY (2,6) turn=10`
- `KEY (2,6) taken turn=18`
- `DOOR (6,1) locked turn=11`
- `DOOR (6,1) unlocked turn=27`
- `EXIT (7,1) turn=28`

This helps delayed communication show up clearly in traces and makes stale-belief analysis easier.

### 4. Diagnosis over replay
The analyzer and report focus on:
- important milestones
- stale or wrong beliefs
- communication timing
- blocked or invalid actions
- recommendations for the next iteration

## Project structure

```text
dungeon-agents/
├─ main.py
├─ dungeon_sim/
│  ├─ agent.py
│  ├─ analyzer.py
│  ├─ engine.py
│  ├─ llm.py
│  ├─ logger.py
│  ├─ models.py
│  ├─ report.py
│  └─ scenario.py
└─ outputs/
   └─ run_<id>/
      ├─ events.jsonl
      ├─ summary.json
      └─ report.md
```

## Requirements

- Python 3.11+
- an OpenAI API key for LLM-generated intent/rationale text
- light dependencies only

## Installation

Create a virtual environment and install dependencies.

```bash
python -m venv .venv
```

### Windows PowerShell
```bash
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### macOS / Linux
```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Environment variables

Create a `.env` file in the project root:

```env
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-5.2
OPENAI_TIMEOUT_SECONDS=45
OPENAI_MAX_RETRIES=2
```

## How to run

### Run one simulation
```bash
python main.py run
```

### Run one simulation with a custom output folder
```bash
python main.py run --output-dir outputs
```

### Run a batch
```bash
python main.py batch --runs 3 --output-dir outputs/batch --base-seed 100
```

### Clean outputs
```bash
python main.py clean --output-dir outputs
```

## Output files

Each run creates a folder like:

```text
outputs/run_<id>/
```

Inside it:

### `events.jsonl`
Line-by-line structured event log for every agent turn.

### `summary.json`
Compact machine-readable summary of the run:
- success / failure
- outcome
- incidents
- communication findings
- recommendations
- final agent states

### `report.md`
Human-readable diagnosis report organized around:
- what happened
- why it happened
- what should change next

## How the policy works

The final rescue policy is intentionally simple and reliable:

1. if standing on the key, pick it up
2. if adjacent to the locked door and holding the key, unlock it
3. if the door is usable and the exit is known, move toward the exit
4. if holding the key and the door is known, move toward the door
5. if the key is known and not yet held, move toward the key
6. otherwise, do deterministic exploration

This keeps the agents purposeful without turning the project into a pathfinding-heavy game.

## Why this approach fits the prompt

This implementation is aligned with the prompt in a few specific ways:

- it avoids overbuilding the dungeon itself
- it keeps outputs structured and inspectable
- it uses AI where it adds value to traces
- it deliberately constrains AI where freeform behavior would hurt reliability
- it exports the exact run artifacts needed for submission and review

## Notes on traces

This project focuses on the structured event record layer first:
- every turn is logged
- belief source and age are surfaced
- delayed messages are visible
- local action reasoning is preserved
- the analyzer summarizes incidents and next-step recommendations

That choice was intentional for the 3 to 4 hour time budget described in the assignment.

## Example run artifacts

A successful run should typically show:
- early exploration under partial visibility
- one agent discovering the key
- the other discovering the door
- delayed communication between agents
- key pickup
- door unlock
- both agents converging to the exit
- a final report explaining the run in plain language

## Tradeoffs

This project intentionally does not try to be:
- a game engine
- a fancy dashboard
- a highly optimized planner
- a general multi-agent framework

It is a focused take-home solution built to emphasize:
- readable traces
- inspectable beliefs
- clean output artifacts
- realistic delivery under deadline

## If I had more time

The next improvements would be:
- richer multi-run comparison tooling
- optional trace viewer / timeline UI
- stronger stale-belief incident clustering
- optional observability integration for latency / LLM I/O
- more scenario variation with the same event schema

## Assignment alignment

The prompt asks for:
- a small simulation where two AI agents explore together
- structured traces of every decision
- a legibility layer that helps diagnose what happened in a run
- a lightweight, intentional presentation rather than a fancy dashboard
- submission artifacts including code, structured run outputs, a short Loom, and full AI chat history

This README and the project structure are written to match those expectations closely.
