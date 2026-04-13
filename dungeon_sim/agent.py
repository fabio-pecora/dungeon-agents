from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMDecisionClient
from .models import (
    AgentAction,
    AgentDecision,
    BeliefDiagnostics,
    BeliefObject,
    BeliefState,
    Direction,
    Observation,
)
from .scenario import DIRECTION_DELTAS, add_pos, in_bounds

MESSAGE_RE = re.compile(
    r"^(KEY|DOOR|EXIT) \((\d+),(\d+)\)(?: ([a-zA-Z_]+))? turn=(\d+)$"
)


@dataclass
class LocalBeliefs:
    position: tuple[int, int]
    visited: set[tuple[int, int]] = field(default_factory=set)
    known_cells: dict[tuple[int, int], str] = field(default_factory=dict)
    key: BeliefObject = field(default_factory=BeliefObject)
    door: BeliefObject = field(default_factory=BeliefObject)
    exit: BeliefObject = field(default_factory=BeliefObject)
    recent_messages: list[str] = field(default_factory=list)
    announced_facts: set[str] = field(default_factory=set)
    last_action_signature: str | None = None
    repeated_no_progress_count: int = 0


class LLMGridAgent:
    def __init__(
        self,
        agent_id: str,
        teammate_id: str,
        role_hint: str,
        llm: LLMDecisionClient,
        *,
        grid_width: int = 8,
        grid_height: int = 8,
    ) -> None:
        self.agent_id = agent_id
        self.teammate_id = teammate_id
        self.role_hint = role_hint
        self.llm = llm
        self.grid_width = grid_width
        self.grid_height = grid_height
        self.beliefs: LocalBeliefs | None = None

    def initialize(self, start_pos: tuple[int, int]) -> None:
        self.beliefs = LocalBeliefs(position=start_pos)
        self.beliefs.visited.add(start_pos)
        self.beliefs.known_cells[start_pos] = "floor"

    def apply_incoming_messages(self, messages: list[str], current_turn: int) -> list[str]:
        assert self.beliefs is not None
        delivered: list[str] = []

        for message in messages:
            delivered.append(message)
            self.beliefs.recent_messages.append(message)
            self.beliefs.recent_messages = self.beliefs.recent_messages[-8:]

            parsed = MESSAGE_RE.match(message.strip())
            if not parsed:
                continue

            kind, x, y, status, sent_turn = parsed.groups()
            pos = (int(x), int(y))
            age = max(0, current_turn - int(sent_turn))
            status = status or "visible"
            source = f"message:{self.teammate_id}"

            if kind == "KEY":
                if status == "taken":
                    self.beliefs.key = BeliefObject(
                        position=pos,
                        status="held_by_teammate",
                        source=source,
                        age=age,
                    )
                else:
                    self.beliefs.key = BeliefObject(
                        position=pos,
                        status=status,
                        source=source,
                        age=age,
                    )

            elif kind == "DOOR":
                self.beliefs.door = BeliefObject(
                    position=pos,
                    status=status,
                    source=source,
                    age=age,
                )
                if status == "unlocked":
                    self.beliefs.known_cells[pos] = "unlocked_door"

            elif kind == "EXIT":
                self.beliefs.exit = BeliefObject(
                    position=pos,
                    status=status,
                    source=source,
                    age=age,
                )

        return delivered

    def observe(self, observation: Observation, current_turn: int) -> BeliefDiagnostics:
        assert self.beliefs is not None

        self.beliefs.position = observation.self_position
        self.beliefs.visited.add(observation.self_position)

        stale: list[str] = []
        wrong: list[str] = []
        notes: list[str] = []

        refreshed = {"key": False, "door": False, "exit": False}
        visible_lookup: dict[tuple[int, int], Any] = {}

        for cell in observation.visible_cells:
            pos = tuple(cell.position)
            visible_lookup[pos] = cell
            self.beliefs.known_cells[pos] = cell.tile_type
            contains = set(cell.contains)

            if "key" in contains:
                self.beliefs.key = BeliefObject(
                    position=pos,
                    status="visible",
                    source="observation",
                    age=0,
                )
                refreshed["key"] = True

            if "door" in contains:
                door_status = "locked" if cell.tile_type == "locked_door" else "unlocked"
                self.beliefs.door = BeliefObject(
                    position=pos,
                    status=door_status,
                    source="observation",
                    age=0,
                )
                refreshed["door"] = True

            if "exit" in contains:
                self.beliefs.exit = BeliefObject(
                    position=pos,
                    status="visible",
                    source="observation",
                    age=0,
                )
                refreshed["exit"] = True

        if observation.standing_on_key:
            self.beliefs.key = BeliefObject(
                position=observation.self_position,
                status="held_here",
                source="observation",
                age=0,
            )
            refreshed["key"] = True

        if "key" in observation.inventory:
            self.beliefs.key = BeliefObject(
                position=self.beliefs.key.position or observation.self_position,
                status="held_by_self",
                source="observation",
                age=0,
            )
            refreshed["key"] = True

        if observation.standing_on_exit:
            self.beliefs.exit = BeliefObject(
                position=observation.self_position,
                status="at_exit",
                source="observation",
                age=0,
            )
            refreshed["exit"] = True

        if (
            self.beliefs.key.position is not None
            and self.beliefs.key.position in visible_lookup
            and "key" not in observation.inventory
            and "key" not in set(visible_lookup[self.beliefs.key.position].contains)
            and self.beliefs.key.status not in {"held_by_self", "held_by_teammate"}
        ):
            wrong.append(
                f"Key belief at {self.beliefs.key.position} is outdated; key is not visible there now."
            )
            self.beliefs.key = BeliefObject(
                position=self.beliefs.key.position,
                status="taken",
                source="observation",
                age=0,
            )
            refreshed["key"] = True

        for name, obj in (
            ("key", self.beliefs.key),
            ("door", self.beliefs.door),
            ("exit", self.beliefs.exit),
        ):
            if obj.position is None:
                continue
            if not refreshed[name]:
                obj.age = (obj.age or 0) + 1
            if obj.source and obj.source.startswith("message") and (obj.age or 0) >= 4:
                stale.append(
                    f"{name.upper()} belief at {obj.position} is aging (age={obj.age}) and only message-backed."
                )

        notes.append(f"Visited {len(self.beliefs.visited)} cells so far.")
        notes.append("Navigation policy is deterministic; LLM is used only for short intent/rationale.")
        if observation.standing_on_key:
            notes.append("Immediate critical action available: pick_up.")
        if observation.adjacent_locked_door and "key" in observation.inventory:
            notes.append("Immediate critical action available: unlock.")
        if self.beliefs.key.source == "observation":
            notes.append("Key belief is directly observed this turn or from inventory.")
        if self.beliefs.door.source == "observation":
            notes.append("Door belief is directly observed this turn.")
        if self.beliefs.exit.source == "observation":
            notes.append("Exit belief is directly observed this turn.")

        return BeliefDiagnostics(
            stale_beliefs=stale,
            wrong_beliefs=wrong,
            confidence_notes=notes,
        )

    def belief_state_model(self) -> BeliefState:
        assert self.beliefs is not None
        return BeliefState(
            believed_position=self.beliefs.position,
            visited_cells=sorted(self.beliefs.visited),
            known_cells={
                self._pos_key(pos): tile
                for pos, tile in sorted(self.beliefs.known_cells.items())
            },
            key_belief=self.beliefs.key,
            door_belief=self.beliefs.door,
            exit_belief=self.beliefs.exit,
            recent_messages=list(self.beliefs.recent_messages),
        )

    def choose_action(
        self,
        *,
        observation: Observation,
        current_turn: int,
        turn_limit: int,
        inventory: list[str],
    ) -> AgentDecision:
        legal_actions = self._legal_actions(observation)

        action, fallback_intent, fallback_rationale = self._deterministic_policy(
            observation=observation,
            current_turn=current_turn,
            inventory=inventory,
            legal_actions=legal_actions,
        )

        decision = self.llm.build_decision(
            action=action,
            fallback_intent=fallback_intent,
            fallback_rationale=fallback_rationale,
            context={
                "agent_id": self.agent_id,
                "teammate_id": self.teammate_id,
                "role_hint": self.role_hint,
                "turn": current_turn,
                "turn_limit": turn_limit,
                "inventory": inventory,
                "observation": observation.model_dump(),
                "belief_state": self.belief_state_model().model_dump(),
                "legal_actions": legal_actions,
            },
        )

        self._record_action_signature(action)
        return decision

    def _deterministic_policy(
        self,
        *,
        observation: Observation,
        current_turn: int,
        inventory: list[str],
        legal_actions: list[dict[str, Any]],
    ) -> tuple[AgentAction, str, str]:
        assert self.beliefs is not None
        legal_types = {item["action_type"] for item in legal_actions}

        if "pick_up" in legal_types:
            return (
                AgentAction(action_type="pick_up"),
                "Pick up the key now.",
                "Standing on the key, so the rescue policy takes the mission-critical action immediately.",
            )

        if "unlock" in legal_types:
            return (
                AgentAction(action_type="unlock"),
                "Unlock the door now.",
                "Adjacent to the locked door while holding the key, so unlocking has highest priority.",
            )

        target = self._select_target(inventory=inventory)
        if self._door_usable() and self.beliefs.exit.position is not None and target is not None:
            move = self._best_move_toward_target(legal_actions, target)
            if move is not None:
                return (
                    AgentAction(action_type="move", direction=Direction(move["direction"])),
                    "Move toward the exit.",
                    "The door is usable and the exit is known, so the rescue policy stops chatting and finishes the run.",
                )

        recommended_message = self._recommended_message(current_turn=current_turn, inventory=inventory)
        if recommended_message and "send_message" in legal_types and self._should_send_now(recommended_message):
            return (
                AgentAction(action_type="send_message", text=recommended_message),
                "Send a critical update.",
                "A newly observed or materially changed fact should be shared before it becomes stale.",
            )

        if target is not None:
            move = self._best_move_toward_target(legal_actions, target)
            if move is not None:
                label = self._target_label(target, inventory)
                return (
                    AgentAction(action_type="move", direction=Direction(move["direction"])),
                    f"Move toward the known {label}.",
                    f"The {label} is already known, so deterministic navigation prioritizes direct progress over generic exploration.",
                )

        move = self._best_exploration_move(legal_actions)
        if move is not None:
            return (
                AgentAction(action_type="move", direction=Direction(move["direction"])),
                "Explore the next deterministic frontier.",
                "No higher-priority target is actionable yet, so take the best frontier step based on role bias and unseen coverage.",
            )

        if "wait" in legal_types:
            return (
                AgentAction(action_type="wait"),
                "Wait.",
                "No legal move gives useful progress this turn.",
            )

        return (
            AgentAction(action_type="wait"),
            "Wait.",
            "Fallback action after exhausting legal deterministic options.",
        )

    def _legal_actions(self, observation: Observation) -> list[dict[str, Any]]:
        assert self.beliefs is not None
        legal: list[dict[str, Any]] = []

        for direction, delta in DIRECTION_DELTAS.items():
            target = add_pos(observation.self_position, delta)
            if not in_bounds(target, self.grid_width, self.grid_height):
                continue

            tile_type = self.beliefs.known_cells.get(target)
            if tile_type == "wall":
                continue
            if tile_type == "locked_door":
                continue

            legal.append(
                {
                    "action_type": "move",
                    "direction": direction.value,
                    "target": list(target),
                    "visited": target in self.beliefs.visited,
                }
            )

        if observation.standing_on_key:
            legal.append({"action_type": "pick_up"})

        if observation.adjacent_locked_door and "key" in observation.inventory:
            legal.append({"action_type": "unlock"})

        legal.append({"action_type": "send_message"})
        legal.append({"action_type": "wait"})
        return legal

    def _recommended_message(self, current_turn: int, inventory: list[str]) -> str | None:
        assert self.beliefs is not None

        if "key" in inventory and self.beliefs.key.position is not None:
            fact = self._fact_key("KEY", self.beliefs.key.position, "taken")
            if fact not in self.beliefs.announced_facts:
                return (
                    f"KEY ({self.beliefs.key.position[0]},{self.beliefs.key.position[1]}) "
                    f"taken turn={current_turn}"
                )

        if self.beliefs.door.position is not None and self.beliefs.door.status in {"locked", "unlocked"}:
            fact = self._fact_key("DOOR", self.beliefs.door.position, self.beliefs.door.status)
            if fact not in self.beliefs.announced_facts and self.beliefs.door.source == "observation":
                return (
                    f"DOOR ({self.beliefs.door.position[0]},{self.beliefs.door.position[1]}) "
                    f"{self.beliefs.door.status} turn={current_turn}"
                )

        if self.beliefs.exit.position is not None and self.beliefs.exit.source == "observation":
            fact = self._fact_key("EXIT", self.beliefs.exit.position)
            if fact not in self.beliefs.announced_facts:
                return (
                    f"EXIT ({self.beliefs.exit.position[0]},{self.beliefs.exit.position[1]}) "
                    f"turn={current_turn}"
                )

        if (
            self.beliefs.key.position is not None
            and self.beliefs.key.source == "observation"
            and self.beliefs.key.status not in {"held_by_self", "held_by_teammate", "taken"}
        ):
            fact = self._fact_key("KEY", self.beliefs.key.position)
            if fact not in self.beliefs.announced_facts:
                return (
                    f"KEY ({self.beliefs.key.position[0]},{self.beliefs.key.position[1]}) "
                    f"turn={current_turn}"
                )

        return None

    def _should_send_now(self, message: str) -> bool:
        assert self.beliefs is not None
        parsed = MESSAGE_RE.match(message)
        if not parsed:
            return False
        kind, x, y, status, _turn = parsed.groups()
        return self._fact_key(kind, (int(x), int(y)), status) not in self.beliefs.announced_facts

    def _select_target(self, inventory: list[str]) -> tuple[int, int] | None:
        assert self.beliefs is not None
        has_key = "key" in inventory
        door_known = self.beliefs.door.position is not None
        exit_known = self.beliefs.exit.position is not None
        key_known = self.beliefs.key.position is not None

        if self._door_usable() and exit_known:
            return self.beliefs.exit.position

        if has_key and door_known and self.beliefs.door.status != "unlocked":
            return self.beliefs.door.position

        if (not has_key) and key_known and self.beliefs.key.status not in {"taken", "held_by_teammate"}:
            return self.beliefs.key.position

        if (
            not has_key
            and self.beliefs.key.status == "held_by_teammate"
            and door_known
            and self.beliefs.door.status != "unlocked"
        ):
            return self._best_door_staging_target()

        if exit_known and self._door_usable():
            return self.beliefs.exit.position

        return None

    def _best_door_staging_target(self) -> tuple[int, int] | None:
        assert self.beliefs is not None
        if self.beliefs.door.position is None:
            return None

        candidates = [
            add_pos(self.beliefs.door.position, delta)
            for delta in DIRECTION_DELTAS.values()
        ]
        candidates = [
            pos
            for pos in candidates
            if in_bounds(pos, self.grid_width, self.grid_height)
            and self.beliefs.known_cells.get(pos) != "wall"
        ]

        if not candidates:
            return self.beliefs.door.position

        current = self.beliefs.position
        candidates.sort(key=lambda pos: (self._manhattan(current, pos), pos[1], pos[0]))
        return candidates[0]

    def _best_move_toward_target(
        self,
        legal_actions: list[dict[str, Any]],
        target: tuple[int, int],
    ) -> dict[str, Any] | None:
        assert self.beliefs is not None
        moves = [item for item in legal_actions if item["action_type"] == "move"]
        if not moves:
            return None

        scored: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []
        for move in moves:
            nxt = tuple(move["target"])
            path_len = self._shortest_path_length(nxt, target)
            if path_len is None:
                path_len = 999

            score = (
                path_len,
                0 if not move.get("visited", True) else 1,
                -self._unknown_neighbor_count(nxt),
                self._direction_rank(move["direction"]),
            )
            scored.append((score, move))

        scored.sort(key=lambda item: item[0])
        return scored[0][1]

    def _best_exploration_move(self, legal_actions: list[dict[str, Any]]) -> dict[str, Any] | None:
        assert self.beliefs is not None
        moves = [item for item in legal_actions if item["action_type"] == "move"]
        if not moves:
            return None

        exploration_target = self._next_exploration_target()
        scored: list[tuple[tuple[int, int, int, int], dict[str, Any]]] = []

        for move in moves:
            nxt = tuple(move["target"])
            if exploration_target is None:
                path_len = 999
            else:
                path_len = self._shortest_path_length(nxt, exploration_target)
                if path_len is None:
                    path_len = 999

            score = (
                0 if not move.get("visited", True) else 1,
                path_len,
                -self._unknown_neighbor_count(nxt),
                self._direction_rank(move["direction"]),
            )
            scored.append((score, move))

        scored.sort(key=lambda item: item[0])
        return scored[0][1]

    def _next_exploration_target(self) -> tuple[int, int] | None:
        assert self.beliefs is not None
        ordered = self._preferred_scan_order()
        for pos in ordered:
            if pos in self.beliefs.visited:
                continue
            if self.beliefs.known_cells.get(pos) == "wall":
                continue
            if self.beliefs.known_cells.get(pos) == "locked_door":
                continue
            return pos
        return None

    def _preferred_scan_order(self) -> list[tuple[int, int]]:
        cells = [(x, y) for y in range(self.grid_height) for x in range(self.grid_width)]
        if self.agent_id == "A":
            cells.sort(key=lambda pos: (pos[1], pos[0]))
        else:
            cells.sort(key=lambda pos: (-pos[1], -pos[0]))
        return cells

    def _shortest_path_length(self, start: tuple[int, int], goal: tuple[int, int]) -> int | None:
        if start == goal:
            return 0

        queue = deque([(start, 0)])
        seen = {start}

        while queue:
            pos, dist = queue.popleft()
            for delta in DIRECTION_DELTAS.values():
                nxt = add_pos(pos, delta)
                if nxt in seen:
                    continue
                if not in_bounds(nxt, self.grid_width, self.grid_height):
                    continue

                tile = self.beliefs.known_cells.get(nxt)
                if tile == "wall":
                    continue
                if tile == "locked_door":
                    continue

                if nxt == goal:
                    return dist + 1

                seen.add(nxt)
                queue.append((nxt, dist + 1))

        return None

    def _unknown_neighbor_count(self, pos: tuple[int, int]) -> int:
        count = 0
        for delta in DIRECTION_DELTAS.values():
            nxt = add_pos(pos, delta)
            if not in_bounds(nxt, self.grid_width, self.grid_height):
                continue
            if nxt not in self.beliefs.known_cells:
                count += 1
        return count

    def _door_usable(self) -> bool:
        assert self.beliefs is not None
        return self.beliefs.door.status == "unlocked"

    def _target_label(self, target: tuple[int, int], inventory: list[str]) -> str:
        assert self.beliefs is not None
        if self.beliefs.exit.position == target and self._door_usable():
            return "exit"
        if self.beliefs.door.position == target and "key" in inventory:
            return "door"
        if self.beliefs.key.position == target and "key" not in inventory:
            return "key"
        if self.beliefs.door.position is not None and self._best_door_staging_target() == target:
            return "door staging cell"
        return "target"

    def _record_action_signature(self, action: AgentAction) -> None:
        assert self.beliefs is not None
        signature = self._action_signature(action)
        if signature == self.beliefs.last_action_signature:
            self.beliefs.repeated_no_progress_count += 1
        else:
            self.beliefs.repeated_no_progress_count = 0

        self.beliefs.last_action_signature = signature

        if action.action_type == "send_message" and action.text:
            parsed = MESSAGE_RE.match(action.text)
            if parsed:
                kind, x, y, status, _turn = parsed.groups()
                self.beliefs.announced_facts.add(
                    self._fact_key(kind, (int(x), int(y)), status)
                )

    @staticmethod
    def _action_signature(action: AgentAction) -> str:
        direction = action.direction.value if action.direction else ""
        text = action.text or ""
        return f"{action.action_type}:{direction}:{text}"

    @staticmethod
    def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    @staticmethod
    def _pos_key(pos: tuple[int, int]) -> str:
        return f"{pos[0]},{pos[1]}"

    @staticmethod
    def _fact_key(kind: str, pos: tuple[int, int], status: str | None = None) -> str:
        suffix = f":{status}" if status else ""
        return f"{kind}:{pos[0]},{pos[1]}{suffix}"

    @staticmethod
    def _direction_rank(direction: str) -> int:
        order = {"up": 0, "left": 1, "right": 2, "down": 3}
        return order.get(direction, 99)