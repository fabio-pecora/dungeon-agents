from __future__ import annotations

from dataclasses import dataclass

from .models import Direction


@dataclass(frozen=True)
class Scenario:
    width: int
    height: int
    walls: set[tuple[int, int]]
    key_pos: tuple[int, int]
    door_pos: tuple[int, int]
    exit_pos: tuple[int, int]
    agent_starts: dict[str, tuple[int, int]]
    turn_limit: int = 38


DIRECTION_DELTAS: dict[Direction, tuple[int, int]] = {
    Direction.UP: (0, -1),
    Direction.DOWN: (0, 1),
    Direction.LEFT: (-1, 0),
    Direction.RIGHT: (1, 0),
}


def build_default_scenario() -> Scenario:
    walls = {
        (3, 0), (3, 1), (3, 2),
        (1, 3), (2, 3),
        (5, 2), (5, 3), (5, 4),
        (6, 5),
    }
    return Scenario(
        width=8,
        height=8,
        walls=walls,
        key_pos=(1, 5),
        door_pos=(6, 1),
        exit_pos=(7, 1),
        agent_starts={"A": (0, 0), "B": (7, 7)},
        turn_limit=38,
    )


def in_bounds(pos: tuple[int, int], width: int, height: int) -> bool:
    x, y = pos
    return 0 <= x < width and 0 <= y < height


def add_pos(pos: tuple[int, int], delta: tuple[int, int]) -> tuple[int, int]:
    return pos[0] + delta[0], pos[1] + delta[1]
