from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from dungeon_sim.analyzer import build_summary
from dungeon_sim.engine import DungeonEngine
from dungeon_sim.report import write_report


def run_once(output_dir: Path, seed: int | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = DungeonEngine()
    run_dir = output_dir / engine.run_id
    logger, world = engine.run(run_dir, seed=seed)
    events = logger.read_events()
    summary = build_summary(
        run_id=engine.run_id,
        model=engine.llm.model,
        turn_limit=engine.scenario.turn_limit,
        turns_executed=engine.turn,
        events=events,
        final_agent_states={
            agent_id: {
                "position": state.position,
                "inventory": list(state.inventory),
                "exited": state.exited,
            }
            for agent_id, state in world.items()
        },
        seed=seed,
    )
    logger.write_summary(summary)
    write_report(run_dir, summary, events)
    return run_dir


def run_batch(runs: int, output_dir: Path, base_seed: int) -> list[Path]:
    paths: list[Path] = []
    for index in range(runs):
        run_seed = base_seed + index
        paths.append(run_once(output_dir=output_dir, seed=run_seed))
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tiny 2-agent LLM dungeon simulation")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run one simulation")
    run_parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    run_parser.add_argument("--seed", type=int, default=None)

    batch_parser = sub.add_parser("batch", help="Run multiple simulations")
    batch_parser.add_argument("--runs", type=int, default=3)
    batch_parser.add_argument("--output-dir", type=Path, default=Path("outputs/batch"))
    batch_parser.add_argument("--base-seed", type=int, default=100)

    clean_parser = sub.add_parser("clean", help="Delete output directory")
    clean_parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "run":
        run_dir = run_once(output_dir=args.output_dir, seed=args.seed)
        print(f"Run written to: {run_dir}")
        return

    if args.command == "batch":
        paths = run_batch(runs=args.runs, output_dir=args.output_dir, base_seed=args.base_seed)
        print("Batch completed:")
        for path in paths:
            print(f"- {path}")
        return

    if args.command == "clean":
        if args.output_dir.exists():
            shutil.rmtree(args.output_dir)
            print(f"Deleted {args.output_dir}")
        else:
            print(f"Nothing to delete at {args.output_dir}")
        return


if __name__ == "__main__":
    main()
