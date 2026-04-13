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
        recommended_message = self._recommended_message(current_turn)
        mission_phase_hint = self._mission_phase_hint(inventory)
        priority_targets = self._priority_targets(inventory)

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
        return self._sanitize_decision(
            decision=decision,
            legal_actions=legal_actions,
            recommended_message=recommended_message,
        )

    def _system_prompt(self) -> str:
        return (
            f"You are agent {self.agent_id} in a tiny cooperative dungeon. "
            f"Your teammate is {self.teammate_id}. {self.role_hint} "
            "Return one structured decision only. "
            "Your goal is not generic exploration. Your real goal is that both agents eventually exit. "
            "Use direct observation first, then recent messages, then older beliefs. Do not invent facts. "
            "You must choose only from the provided legal_actions. Never choose an action that is not explicitly legal. "
            "Decision priority order: "
            "1) If an immediate critical action is legal, do it: pick_up if standing on key; unlock if adjacent to locked door and holding key. "
            "2) If you have a newly discovered critical fact and teammate may not know it, strongly prefer send_message using the recommended_message when available. "
            "3) If you know where a critical target is, move to make progress toward it. "
            "4) Explore only when no higher-priority action is available. "
            "5) Avoid repeating non-progress actions and avoid wandering once key, door, and exit knowledge is sufficient. "
            "Keep rationale honest and concise. "
            "When choosing move, set direction and leave text null. "
            "When choosing send_message, use a short structured message exactly like KEY (x,y) turn=t or DOOR (x,y) locked turn=t or EXIT (x,y) turn=t. "
            "Do not include markdown."
        )

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

    def _recommended_message(self, current_turn: int) -> str | None:
        assert self.beliefs is not None

        candidates = [
            ("KEY", self.beliefs.key),
            ("DOOR", self.beliefs.door),
            ("EXIT", self.beliefs.exit),
        ]

        for label, obj in candidates:
            if obj.position is None:
                continue

            if label == "DOOR":
                status = obj.status or "locked"
                message = f"{label} ({obj.position[0]},{obj.position[1]}) {status} turn={current_turn}"
            else:
                message = f"{label} ({obj.position[0]},{obj.position[1]}) turn={current_turn}"

            fact_key = self._fact_key(label, obj.position, obj.status)
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
        if key_known and door_known:
            return "door_plan"
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

        if has_key and self.beliefs.door.position is not None:
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
                    "reason": "Exit is known and should become final objective once path is open.",
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

            fallback = self._fallback_decision(
                legal_actions=legal_actions,
                recommended_message=recommended_message,
                reason=f"Model chose illegal move direction: {chosen_dir}",
            )
            return fallback

        if action.action_type not in legal_types:
            fallback = self._fallback_decision(
                legal_actions=legal_actions,
                recommended_message=recommended_message,
                reason=f"Model chose illegal action type: {action.action_type}",
            )
            return fallback

        if action.action_type == "send_message":
            text = (action.text or "").strip()
            if text:
                parsed = MESSAGE_RE.match(text)
                if parsed:
                    kind, x, y, status, _sent_turn = parsed.groups()
                    fact_key = self._fact_key(kind, (int(x), int(y)), status)
                    assert self.beliefs is not None
                    self.beliefs.announced_facts.add(fact_key)
                    return decision

            if recommended_message:
                assert self.beliefs is not None
                parsed = MESSAGE_RE.match(recommended_message)
                if parsed:
                    kind, x, y, status, _sent_turn = parsed.groups()
                    self.beliefs.announced_facts.add(
                        self._fact_key(kind, (int(x), int(y)), status)
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
        if recommended_message and any(a["action_type"] == "send_message" for a in legal_actions):
            assert self.beliefs is not None
            parsed = MESSAGE_RE.match(recommended_message)
            if parsed:
                kind, x, y, status, _sent_turn = parsed.groups()
                self.beliefs.announced_facts.add(
                    self._fact_key(kind, (int(x), int(y)), status)
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

    @staticmethod
    def _fact_key(label: str, position: tuple[int, int], status: str | None) -> str:
        return f"{label}:{position[0]},{position[1]}:{status or ''}"

    @staticmethod
    def _pos_key(pos: tuple[int, int]) -> str:
        return f"{pos[0]},{pos[1]}"