from __future__ import annotations

import re
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
from .scenario import DIRECTION_DELTAS

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
    def __init__(self, agent_id: str, teammate_id: str, role_hint: str, llm: LLMDecisionClient) -> None:
        self.agent_id = agent_id
        self.teammate_id = teammate_id
        self.role_hint = role_hint
        self.llm = llm
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
            self.beliefs.recent_messages = self.beliefs.recent_messages[-6:]

            parsed = MESSAGE_RE.match(message.strip())
            if not parsed:
                continue

            kind, x, y, status, sent_turn = parsed.groups()
            age = current_turn - int(sent_turn)
            obj = BeliefObject(
                position=(int(x), int(y)),
                status=status,
                source=f"message:{self.teammate_id}",
                age=age,
            )

            if kind == "KEY":
                self.beliefs.key = obj
            elif kind == "DOOR":
                self.beliefs.door = obj
            elif kind == "EXIT":
                self.beliefs.exit = obj

        return delivered

    def observe(self, observation: Observation, current_turn: int) -> BeliefDiagnostics:
        assert self.beliefs is not None
        self.beliefs.position = observation.self_position
        self.beliefs.visited.add(observation.self_position)

        stale: list[str] = []
        wrong: list[str] = []
        notes: list[str] = []

        for cell in observation.visible_cells:
            pos = tuple(cell.position)
            self.beliefs.known_cells[pos] = cell.tile_type
            contains = set(cell.contains)

            if "key" in contains:
                self.beliefs.key = BeliefObject(
                    position=pos,
                    status="visible",
                    source="observation",
                    age=0,
                )
            elif (
                self.beliefs.key.position == pos
                and self.beliefs.key.source
                and self.beliefs.key.source.startswith("message")
            ):
                stale.append(f"Key message may be stale at {pos}; key not visible now.")

            if "door" in contains:
                door_status = "locked" if cell.tile_type == "locked_door" else "unlocked"
                self.beliefs.door = BeliefObject(
                    position=pos,
                    status=door_status,
                    source="observation",
                    age=0,
                )
            elif (
                self.beliefs.door.position == pos
                and self.beliefs.door.source
                and self.beliefs.door.source.startswith("message")
            ):
                stale.append(f"Door message may be stale at {pos}; status not confirmed this turn.")

            if "exit" in contains:
                self.beliefs.exit = BeliefObject(
                    position=pos,
                    status="visible",
                    source="observation",
                    age=0,
                )

        if observation.standing_on_key:
            self.beliefs.key = BeliefObject(
                position=observation.self_position,
                status="held_here",
                source="observation",
                age=0,
            )

        if observation.standing_on_exit:
            self.beliefs.exit = BeliefObject(
                position=observation.self_position,
                status="at_exit",
                source="observation",
                age=0,
            )

        for obj_name, obj in (
            ("key", self.beliefs.key),
            ("door", self.beliefs.door),
            ("exit", self.beliefs.exit),
        ):
            if obj.position is not None and obj.age is not None:
                obj.age += 1
                if obj.age >= 4 and obj.source and obj.source.startswith("message"):
                    stale.append(
                        f"{obj_name.capitalize()} belief from message is getting old (age={obj.age})."
                    )

        notes.append(f"Visited {len(self.beliefs.visited)} cells so far.")

        if self.beliefs.key.position and self.beliefs.key.source == "observation":
            notes.append("Key location is directly observed and reliable.")
        if self.beliefs.door.position and self.beliefs.door.source == "observation":
            notes.append("Door location/status is directly observed and reliable.")
        if self.beliefs.exit.position and self.beliefs.exit.source == "observation":
            notes.append("Exit location is directly observed and reliable.")

        if observation.adjacent_locked_door and "key" in observation.inventory:
            notes.append("Immediate critical action available: unlock is possible now.")
        if observation.standing_on_key:
            notes.append("Immediate critical action available: pick_up is possible now.")

        return BeliefDiagnostics(
            stale_beliefs=stale,
            wrong_beliefs=wrong,
            confidence_notes=notes,
        )

    def belief_state_model(self) -> BeliefState:
        assert self.beliefs is not None
        known_cells = {
            self._pos_key(pos): tile
            for pos, tile in sorted(self.beliefs.known_cells.items())
        }
        return BeliefState(
            believed_position=self.beliefs.position,
            visited_cells=sorted(self.beliefs.visited),
            known_cells=known_cells,
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
        assert self.beliefs is not None

        legal_actions = self._legal_actions(observation)
        frontier_moves = self._frontier_moves(observation)
        recommended_message = self._recommended_message(current_turn, inventory)
        mission_phase_hint = self._mission_phase_hint(inventory)
        priority_targets = self._priority_targets(inventory)

        forced = self._forced_or_planned_decision(
            observation=observation,
            current_turn=current_turn,
            inventory=inventory,
            legal_actions=legal_actions,
            recommended_message=recommended_message,
        )
        if forced is not None:
            self._record_action_signature(forced.action)
            return forced

        payload = {
            "agent_id": self.agent_id,
            "teammate_id": self.teammate_id,
            "role_hint": self.role_hint,
            "turn": current_turn,
            "turn_limit": turn_limit,
            "inventory": inventory,
            "mission_phase_hint": mission_phase_hint,
            "observation": observation.model_dump(),
            "belief_state": self.belief_state_model().model_dump(),
            "legal_actions": legal_actions,
            "suggested_frontier_moves": frontier_moves,
            "priority_targets": priority_targets,
            "recommended_message": recommended_message,
            "message_format_examples": [
                "KEY (1,5) turn=5",
                "DOOR (6,1) locked turn=7",
                "EXIT (7,1) turn=8",
            ],
            "rules": {
                "one_action": True,
                "exact_actions": ["move", "pick_up", "unlock", "send_message", "wait"],
                "messages_arrive_next_turn": True,
                "only_choose_from_legal_actions": True,
                "only_move_to_adjacent_cardinal_cell": True,
                "unlock_requires_adjacent_locked_door_and_key": True,
            },
        }

        decision = self.llm.decide(system_prompt=self._system_prompt(), user_payload=payload)
        decision = self._sanitize_decision(
            decision=decision,
            legal_actions=legal_actions,
            recommended_message=recommended_message,
        )
        self._record_action_signature(decision.action)
        return decision

    def _system_prompt(self) -> str:
        return (
            f"You are agent {self.agent_id} in a tiny cooperative dungeon. "
            f"Your teammate is {self.teammate_id}. {self.role_hint} "
            "Return one structured decision only. "
            "Your goal is that both agents eventually exit. "
            "Do not behave like a generic explorer. "
            "Use direct observation first, then recent messages, then older beliefs. Do not invent facts. "
            "You must choose only from the provided legal_actions. Never choose an action that is not explicitly legal. "
            "Decision priority order: "
            "1) If pick_up is legal, choose pick_up immediately. "
            "2) If unlock is legal, choose unlock immediately. "
            "3) If you have a newly discovered critical fact and teammate may not know it, strongly prefer send_message using the recommended_message. "
            "4) If a priority target is known, choose a move that reduces distance to that target. "
            "5) Explore only when no higher-priority action is available. "
            "6) Avoid repeated non-progress actions. "
            "7) Once key, door, and exit knowledge is sufficient, stop generic scouting and converge on completion. "
            "Keep rationale short and honest. "
            "When choosing move, set direction and leave text null. "
            "When choosing send_message, use a short structured message exactly like KEY (x,y) turn=t or DOOR (x,y) locked turn=t or EXIT (x,y) turn=t. "
            "Do not include markdown."
        )

    def _forced_or_planned_decision(
        self,
        *,
        observation: Observation,
        current_turn: int,
        inventory: list[str],
        legal_actions: list[dict[str, Any]],
        recommended_message: str | None,
    ) -> AgentDecision | None:
        assert self.beliefs is not None

        legal_types = {a["action_type"] for a in legal_actions}

        if "pick_up" in legal_types:
            return AgentDecision(
                intent_summary="Pick up the key immediately.",
                rationale="Standing on the key, so immediate mission-critical action takes priority.",
                action=AgentAction(action_type="pick_up", direction=None, text=None),
            )

        if "unlock" in legal_types:
            return AgentDecision(
                intent_summary="Unlock the door immediately.",
                rationale="Adjacent locked door and key is held, so immediate mission-critical action takes priority.",
                action=AgentAction(action_type="unlock", direction=None, text=None),
            )

        if recommended_message and self._should_send_now(observation, inventory):
            parsed = MESSAGE_RE.match(recommended_message)
            if parsed:
                kind, x, y, status, _sent_turn = parsed.groups()
                self.beliefs.announced_facts.add(
                    self._fact_key(kind, (int(x), int(y)))
                )
            return AgentDecision(
                intent_summary="Send critical fact to teammate.",
                rationale="A newly learned critical fact should be communicated before it becomes stale.",
                action=AgentAction(action_type="send_message", direction=None, text=recommended_message),
            )

        target = self._select_target(inventory)
        if target is not None:
            move = self._best_move_toward_target(legal_actions, target)
            if move is not None:
                target_name = self._target_name(inventory)
                return AgentDecision(
                    intent_summary=f"Move toward known {target_name}.",
                    rationale=f"A critical target is known, so progress toward it is better than more generic exploration.",
                    action=AgentAction(
                        action_type="move",
                        direction=Direction(move["direction"]),
                        text=None,
                    ),
                )

        if self.beliefs.repeated_no_progress_count >= 2:
            frontier_move = self._best_frontier_move(legal_actions)
            if frontier_move is not None:
                return AgentDecision(
                    intent_summary="Break local repetition by taking a fresh frontier step.",
                    rationale="Recent behavior showed low progress, so choose a new frontier move instead of repeating a stale pattern.",
                    action=AgentAction(
                        action_type="move",
                        direction=Direction(frontier_move["direction"]),
                        text=None,
                    ),
                )

        return None
    
    def _should_send_now(self, observation: Observation, inventory: list[str]) -> bool:
        assert self.beliefs is not None

        if observation.standing_on_key and self._fact_not_announced("KEY", self.beliefs.key):
            return True

        if observation.adjacent_locked_door and self._fact_not_announced("DOOR", self.beliefs.door):
            return True

        if observation.standing_on_exit and self._fact_not_announced("EXIT", self.beliefs.exit):
            return True

        if self.beliefs.key.source == "observation" and self._fact_not_announced("KEY", self.beliefs.key):
            return True

        if self.beliefs.door.source == "observation" and self._fact_not_announced("DOOR", self.beliefs.door):
            return True

        if self.beliefs.exit.source == "observation" and self._fact_not_announced("EXIT", self.beliefs.exit):
            return True

        return False

    def _select_target(self, inventory: list[str]) -> tuple[int, int] | None:
        assert self.beliefs is not None
        has_key = "key" in inventory

        if not has_key and self.beliefs.key.position is not None:
            return self.beliefs.key.position

        if has_key and self.beliefs.door.position is not None and self.beliefs.door.status != "unlocked":
            return self.beliefs.door.position

        if self.beliefs.exit.position is not None:
            return self.beliefs.exit.position

        return None

    def _target_name(self, inventory: list[str]) -> str:
        assert self.beliefs is not None
        has_key = "key" in inventory

        if not has_key and self.beliefs.key.position is not None:
            return "key"
        if has_key and self.beliefs.door.position is not None and self.beliefs.door.status != "unlocked":
            return "door"
        if self.beliefs.exit.position is not None:
            return "exit"
        return "objective"

    def _best_move_toward_target(
        self,
        legal_actions: list[dict[str, Any]],
        target: tuple[int, int],
    ) -> dict[str, Any] | None:
        assert self.beliefs is not None
        current = self.beliefs.position

        move_actions = [a for a in legal_actions if a["action_type"] == "move"]
        if not move_actions:
            return None

        current_dist = self._manhattan(current, target)
        improving: list[dict[str, Any]] = []

        for action in move_actions:
            nxt = tuple(action["target"])
            dist = self._manhattan(nxt, target)
            if dist < current_dist:
                enriched = dict(action)
                enriched["distance_after"] = dist
                improving.append(enriched)

        if improving:
            improving.sort(
                key=lambda a: (
                    a["distance_after"],
                    a.get("visited", True),
                    a["direction"],
                )
            )
            return improving[0]

        move_actions.sort(
            key=lambda a: (
                self._manhattan(tuple(a["target"]), target),
                a.get("visited", True),
                a["direction"],
            )
        )
        return move_actions[0]

    def _best_frontier_move(self, legal_actions: list[dict[str, Any]]) -> dict[str, Any] | None:
        move_actions = [a for a in legal_actions if a["action_type"] == "move"]
        if not move_actions:
            return None
        move_actions.sort(key=lambda a: (a.get("visited", True), a["direction"]))
        return move_actions[0]

    def _legal_actions(self, observation: Observation) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        visible = {tuple(cell.position): cell for cell in observation.visible_cells}

        for direction, delta in DIRECTION_DELTAS.items():
            nxt = (
                observation.self_position[0] + delta[0],
                observation.self_position[1] + delta[1],
            )
            cell = visible.get(nxt)
            if cell is None:
                continue
            if cell.tile_type in {"wall", "locked_door"}:
                continue
            actions.append(
                {
                    "action_type": "move",
                    "direction": direction.value,
                    "text": None,
                    "target": nxt,
                    "visited": nxt in self.beliefs.visited,
                    "tile_type": cell.tile_type,
                }
            )

        if observation.standing_on_key:
            actions.append(
                {
                    "action_type": "pick_up",
                    "direction": None,
                    "text": None,
                }
            )

        if observation.adjacent_locked_door and "key" in observation.inventory:
            actions.append(
                {
                    "action_type": "unlock",
                    "direction": None,
                    "text": None,
                }
            )

        actions.append(
            {
                "action_type": "send_message",
                "direction": None,
                "text": None,
            }
        )
        actions.append(
            {
                "action_type": "wait",
                "direction": None,
                "text": None,
            }
        )

        return actions

    def _frontier_moves(self, observation: Observation) -> list[dict[str, Any]]:
        visible = {tuple(cell.position): cell for cell in observation.visible_cells}
        options: list[dict[str, Any]] = []

        for direction, delta in DIRECTION_DELTAS.items():
            nxt = (
                observation.self_position[0] + delta[0],
                observation.self_position[1] + delta[1],
            )
            cell = visible.get(nxt)
            if cell is None:
                continue
            if cell.tile_type in {"wall", "locked_door"}:
                continue

            options.append(
                {
                    "direction": direction.value,
                    "target": nxt,
                    "visited": nxt in self.beliefs.visited,
                    "tile_type": cell.tile_type,
                }
            )

        options.sort(key=lambda item: (item["visited"], item["direction"]))
        return options
    
    def _recommended_message(self, current_turn: int, inventory: list[str]) -> str | None:
        assert self.beliefs is not None

        has_key = "key" in inventory

        candidates: list[tuple[str, BeliefObject]] = []

        if not has_key:
            candidates.append(("KEY", self.beliefs.key))

        candidates.append(("DOOR", self.beliefs.door))
        candidates.append(("EXIT", self.beliefs.exit))

        for label, obj in candidates:
            if obj.position is None:
                continue
            if obj.source != "observation":
                continue

            if label == "DOOR":
                status = obj.status or "locked"
                message = f"{label} ({obj.position[0]},{obj.position[1]}) {status} turn={current_turn}"
            else:
                message = f"{label} ({obj.position[0]},{obj.position[1]}) turn={current_turn}"

            fact_key = self._fact_key(label, obj.position)
            if fact_key not in self.beliefs.announced_facts:
                return message

        return None

    def _mission_phase_hint(self, inventory: list[str]) -> str:
        assert self.beliefs is not None

        key_known = self.beliefs.key.position is not None or "key" in inventory
        door_known = self.beliefs.door.position is not None
        exit_known = self.beliefs.exit.position is not None
        has_key = "key" in inventory

        if has_key and door_known and exit_known:
            return "exit_plan"
        if has_key and door_known:
            return "door_plan"
        if key_known and door_known and exit_known:
            return "full_objective_known"
        if key_known or door_known or exit_known:
            return "critical_fact_known"
        return "explore"

    def _priority_targets(self, inventory: list[str]) -> list[dict[str, Any]]:
        assert self.beliefs is not None
        targets: list[dict[str, Any]] = []
        has_key = "key" in inventory

        if not has_key and self.beliefs.key.position is not None:
            targets.append(
                {
                    "type": "key",
                    "position": self.beliefs.key.position,
                    "reason": "Need key before unlocking door.",
                }
            )

        if has_key and self.beliefs.door.position is not None and self.beliefs.door.status != "unlocked":
            targets.append(
                {
                    "type": "door",
                    "position": self.beliefs.door.position,
                    "reason": "Have key and should unlock door.",
                }
            )

        if self.beliefs.exit.position is not None:
            targets.append(
                {
                    "type": "exit",
                    "position": self.beliefs.exit.position,
                    "reason": "Exit is the final objective once path is open.",
                }
            )

        return targets

    def _sanitize_decision(
        self,
        *,
        decision: AgentDecision,
        legal_actions: list[dict[str, Any]],
        recommended_message: str | None,
    ) -> AgentDecision:
        action = decision.action

        legal_move_dirs = {
            item["direction"]
            for item in legal_actions
            if item["action_type"] == "move" and item.get("direction")
        }
        legal_types = {item["action_type"] for item in legal_actions}

        if action.action_type == "move":
            chosen_dir = action.direction.value if action.direction else None
            if chosen_dir in legal_move_dirs:
                return decision

            return self._fallback_decision(
                legal_actions=legal_actions,
                recommended_message=recommended_message,
                reason=f"Model chose illegal move direction: {chosen_dir}",
            )

        if action.action_type not in legal_types:
            return self._fallback_decision(
                legal_actions=legal_actions,
                recommended_message=recommended_message,
                reason=f"Model chose illegal action type: {action.action_type}",
            )

        if action.action_type == "send_message":
            text = (action.text or "").strip()
            if text:
                parsed = MESSAGE_RE.match(text)
                if parsed:
                    kind, x, y, _status, _sent_turn = parsed.groups()
                    fact_key = self._fact_key(kind, (int(x), int(y)))
                    assert self.beliefs is not None
                    self.beliefs.announced_facts.add(fact_key)
                    return decision

            if recommended_message:
                assert self.beliefs is not None
                parsed = MESSAGE_RE.match(recommended_message)
                if parsed:
                    kind, x, y, _status, _sent_turn = parsed.groups()
                    self.beliefs.announced_facts.add(
                        self._fact_key(kind, (int(x), int(y)))
                    )

                return AgentDecision(
                    intent_summary="Send newly learned critical fact to teammate.",
                    rationale="Model selected send_message without a valid structured text, so fallback used the recommended message.",
                    action=AgentAction(
                        action_type="send_message",
                        direction=None,
                        text=recommended_message,
                    ),
                )

            return self._fallback_decision(
                legal_actions=legal_actions,
                recommended_message=None,
                reason="Model selected send_message without valid text and no recommended message was available.",
            )

        return decision

    def _fallback_decision(
        self,
        *,
        legal_actions: list[dict[str, Any]],
        recommended_message: str | None,
        reason: str,
    ) -> AgentDecision:
        if any(a["action_type"] == "pick_up" for a in legal_actions):
            return AgentDecision(
                intent_summary="Take immediate critical action.",
                rationale=f"{reason}. Fallback chose pick_up because it is a valid critical action.",
                action=AgentAction(
                    action_type="pick_up",
                    direction=None,
                    text=None,
                ),
            )

        if any(a["action_type"] == "unlock" for a in legal_actions):
            return AgentDecision(
                intent_summary="Take immediate critical action.",
                rationale=f"{reason}. Fallback chose unlock because it is a valid critical action.",
                action=AgentAction(
                    action_type="unlock",
                    direction=None,
                    text=None,
                ),
            )

        if recommended_message and any(a["action_type"] == "send_message" for a in legal_actions):
            assert self.beliefs is not None
            parsed = MESSAGE_RE.match(recommended_message)
            if parsed:
                kind, x, y, _status, _sent_turn = parsed.groups()
                self.beliefs.announced_facts.add(
                    self._fact_key(kind, (int(x), int(y)))
                )

            return AgentDecision(
                intent_summary="Communicate critical fact safely.",
                rationale=f"{reason}. Fallback chose a valid structured message.",
                action=AgentAction(
                    action_type="send_message",
                    direction=None,
                    text=recommended_message,
                ),
            )

        move_candidates = [
            a for a in legal_actions
            if a["action_type"] == "move"
        ]
        if move_candidates:
            move_candidates.sort(key=lambda item: (item.get("visited", True), item["direction"]))
            best = move_candidates[0]
            return AgentDecision(
                intent_summary="Advance using safest valid move.",
                rationale=f"{reason}. Fallback chose the first valid frontier-biased move.",
                action=AgentAction(
                    action_type="move",
                    direction=Direction(best["direction"]),
                    text=None,
                ),
            )

        return AgentDecision(
            intent_summary="Avoid invalid action.",
            rationale=f"{reason}. No productive legal action was available, so fallback chose wait.",
            action=AgentAction(
                action_type="wait",
                direction=None,
                text=None,
            ),
        )

    def _record_action_signature(self, action: AgentAction) -> None:
        assert self.beliefs is not None
        signature = f"{action.action_type}:{action.direction.value if action.direction else ''}:{action.text or ''}"
        if self.beliefs.last_action_signature == signature:
            self.beliefs.repeated_no_progress_count += 1
        else:
            self.beliefs.repeated_no_progress_count = 0
        self.beliefs.last_action_signature = signature

    def _fact_not_announced(self, label: str, obj: BeliefObject) -> bool:
        assert self.beliefs is not None
        if obj.position is None:
            return False
        return self._fact_key(label, obj.position) not in self.beliefs.announced_facts

    @staticmethod
    def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    @staticmethod
    def _fact_key(label: str, position: tuple[int, int]) -> str:
        return f"{label}:{position[0]},{position[1]}"

    @staticmethod
    def _pos_key(pos: tuple[int, int]) -> str:
        return f"{pos[0]},{pos[1]}"