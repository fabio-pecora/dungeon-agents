from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .agent import LLMGridAgent
from .llm import LLMDecisionClient
from .logger import RunLogger
from .models import (
    ActionExecution,
    AgentDecision,
    AgentStepEvent,
    Attribution,
    CellView,
    MessageEvent,
    Observation,
    OutcomeAssessment,
)
from .scenario import DIRECTION_DELTAS, Scenario, add_pos, build_default_scenario, in_bounds


@dataclass
class AgentWorldState:
    position: tuple[int, int]
    inventory: list[str] = field(default_factory=list)
    exited: bool = False


class DungeonEngine:
    def __init__(self, scenario: Scenario | None = None, llm_client: LLMDecisionClient | None = None) -> None:
        self.scenario = scenario or build_default_scenario()
        self.llm = llm_client or LLMDecisionClient()
        self.run_id = f"run_{uuid.uuid4().hex[:8]}"
        self.door_locked = True
        self.key_picked = False
        self.key_holder: str | None = None
        self.turn = 0
        self.message_queue: list[dict] = []
        self.world: dict[str, AgentWorldState] = {
            agent_id: AgentWorldState(position=pos)
            for agent_id, pos in self.scenario.agent_starts.items()
        }
        self.agents = {
            "A": LLMGridAgent(
                "A",
                "B",
                "Prefer systematic coverage from the upper-left side, then converge on the door and exit.",
                self.llm,
                grid_width=self.scenario.width,
                grid_height=self.scenario.height,
            ),
            "B": LLMGridAgent(
                "B",
                "A",
                "Prefer systematic coverage from the lower-right side, then carry the key to the door.",
                self.llm,
                grid_width=self.scenario.width,
                grid_height=self.scenario.height,
            ),
        }
        for agent_id, agent in self.agents.items():
            agent.initialize(self.world[agent_id].position)

    def run(self, output_dir: Path, seed: int | None = None) -> tuple[RunLogger, dict[str, AgentWorldState]]:
        logger = RunLogger(output_dir)
        agent_order = ["A", "B"]

        while self.turn < self.scenario.turn_limit and not self._all_exited():
            for agent_id in agent_order:
                if self.turn >= self.scenario.turn_limit or self._all_exited():
                    break
                if self.world[agent_id].exited:
                    continue

                self.turn += 1
                self._execute_agent_turn(agent_id=agent_id, logger=logger)

        return logger, self.world

    def _execute_agent_turn(self, agent_id: str, logger: RunLogger) -> None:
        agent = self.agents[agent_id]

        delivered = self._deliver_messages(agent_id)
        agent.apply_incoming_messages(delivered, self.turn)

        observation = self._build_observation(agent_id)
        diagnostics = agent.observe(observation, self.turn)

        decision = agent.choose_action(
            observation=observation,
            current_turn=self.turn,
            turn_limit=self.scenario.turn_limit,
            inventory=list(self.world[agent_id].inventory),
        )

        execution = self._apply_action(agent_id, decision)
        assessment = self._assess(agent_id, execution)
        attribution = self._attribute(diagnostics, execution)

        event = AgentStepEvent(
            run_id=self.run_id,
            turn=self.turn,
            agent_id=agent_id,
            observation=observation,
            belief_state=agent.belief_state_model(),
            belief_diagnostics=diagnostics,
            messages=MessageEvent(
                delivered_this_turn=delivered,
                sent_this_turn=[decision.action.text]
                if decision.action.action_type == "send_message" and decision.action.text
                else [],
                inbox_size_after_delivery=len(agent.beliefs.recent_messages if agent.beliefs else []),
            ),
            decision=decision,
            action_execution=execution,
            outcome_assessment=assessment,
            attribution=attribution,
        )
        logger.write_event(event)

    def _deliver_messages(self, agent_id: str) -> list[str]:
        delivered: list[str] = []
        remaining: list[dict] = []

        for item in self.message_queue:
            if item["recipient"] == agent_id and item["deliver_turn"] <= self.turn:
                delivered.append(item["text"])
            else:
                remaining.append(item)

        self.message_queue = remaining
        return delivered

    def _build_observation(self, agent_id: str) -> Observation:
        state = self.world[agent_id]
        visible_cells: list[CellView] = []
        x0, y0 = state.position

        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                pos = (x0 + dx, y0 + dy)
                if not in_bounds(pos, self.scenario.width, self.scenario.height):
                    continue

                tile_type = self._tile_type(pos)
                contains: list[str] = []

                if not self.key_picked and pos == self.scenario.key_pos:
                    contains.append("key")
                if pos == self.scenario.door_pos:
                    contains.append("door")
                if pos == self.scenario.exit_pos:
                    contains.append("exit")

                visible_cells.append(
                    CellView(position=pos, tile_type=tile_type, contains=contains)
                )

        adjacent_locked_door = any(
            add_pos(state.position, delta) == self.scenario.door_pos and self.door_locked
            for delta in DIRECTION_DELTAS.values()
        )

        return Observation(
            self_position=state.position,
            visible_cells=visible_cells,
            adjacent_locked_door=adjacent_locked_door,
            standing_on_key=(not self.key_picked and state.position == self.scenario.key_pos),
            standing_on_exit=(state.position == self.scenario.exit_pos),
            inventory=list(state.inventory),
        )

    def _tile_type(self, pos: tuple[int, int]) -> str:
        if pos in self.scenario.walls:
            return "wall"
        if pos == self.scenario.door_pos:
            return "locked_door" if self.door_locked else "unlocked_door"
        if pos == self.scenario.exit_pos:
            return "exit"
        return "floor"

    def _apply_action(self, agent_id: str, decision: AgentDecision) -> ActionExecution:
        state = self.world[agent_id]
        action = decision.action
        tool_output = {
            "position_before": state.position,
            "inventory_before": list(state.inventory),
        }

        if action.action_type == "move":
            if action.direction is None:
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="invalid",
                    result="Move action omitted direction.",
                    tool_output=tool_output,
                )

            nxt = add_pos(state.position, DIRECTION_DELTAS[action.direction])

            if not in_bounds(nxt, self.scenario.width, self.scenario.height):
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="blocked",
                    result=f"Move blocked: {nxt} is out of bounds.",
                    tool_output=tool_output,
                )

            if nxt in self.scenario.walls:
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="blocked",
                    result=f"Move blocked by wall at {nxt}.",
                    tool_output=tool_output,
                )

            if nxt == self.scenario.door_pos and self.door_locked:
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="blocked",
                    result=f"Move blocked by locked door at {nxt}.",
                    tool_output=tool_output,
                )

            state.position = nxt
            if state.position == self.scenario.exit_pos:
                state.exited = True

            tool_output.update(
                {
                    "position_after": state.position,
                    "exited": state.exited,
                }
            )
            return ActionExecution(
                requested_action=action.model_dump(),
                execution_status="executed",
                result=f"Moved {action.direction.value} to {state.position}.",
                tool_output=tool_output,
            )

        if action.action_type == "pick_up":
            if self.key_picked or state.position != self.scenario.key_pos:
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="blocked",
                    result="Pick up failed: no key on current cell.",
                    tool_output=tool_output,
                )

            self.key_picked = True
            self.key_holder = agent_id
            if "key" not in state.inventory:
                state.inventory.append("key")

            tool_output.update(
                {
                    "inventory_after": list(state.inventory),
                    "key_holder": agent_id,
                }
            )
            return ActionExecution(
                requested_action=action.model_dump(),
                execution_status="executed",
                result="Picked up the key.",
                tool_output=tool_output,
            )

        if action.action_type == "unlock":
            adjacent = any(
                add_pos(state.position, delta) == self.scenario.door_pos
                for delta in DIRECTION_DELTAS.values()
            )
            if not adjacent:
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="blocked",
                    result="Unlock failed: no adjacent door.",
                    tool_output=tool_output,
                )

            if "key" not in state.inventory:
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="blocked",
                    result="Unlock failed: agent does not hold the key.",
                    tool_output=tool_output,
                )

            if not self.door_locked:
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="blocked",
                    result="Unlock skipped: door already unlocked.",
                    tool_output=tool_output,
                )

            self.door_locked = False
            tool_output.update(
                {
                    "door_position": self.scenario.door_pos,
                    "door_locked_after": self.door_locked,
                }
            )
            return ActionExecution(
                requested_action=action.model_dump(),
                execution_status="executed",
                result=f"Unlocked the door at {self.scenario.door_pos}.",
                tool_output=tool_output,
            )

        if action.action_type == "send_message":
            text = (action.text or "").strip()
            if not text:
                return ActionExecution(
                    requested_action=action.model_dump(),
                    execution_status="invalid",
                    result="Send message omitted text.",
                    tool_output=tool_output,
                )

            recipient = "B" if agent_id == "A" else "A"
            self.message_queue.append(
                {
                    "recipient": recipient,
                    "text": text,
                    "deliver_turn": self.turn + 1,
                }
            )
            tool_output.update(
                {
                    "recipient": recipient,
                    "deliver_turn": self.turn + 1,
                }
            )
            return ActionExecution(
                requested_action=action.model_dump(),
                execution_status="executed",
                result=f"Queued message for {recipient}: {text}",
                tool_output=tool_output,
            )

        if action.action_type == "wait":
            return ActionExecution(
                requested_action=action.model_dump(),
                execution_status="executed",
                result="Waited for one turn.",
                tool_output=tool_output,
            )

        return ActionExecution(
            requested_action=action.model_dump(),
            execution_status="invalid",
            result="Unknown action.",
            tool_output=tool_output,
        )

    def _assess(self, agent_id: str, execution: ActionExecution) -> OutcomeAssessment:
        state = self.world[agent_id]

        if state.exited:
            return OutcomeAssessment(
                progress="Reached the exit.",
                local_reasonableness="Reasonable because exiting is the terminal goal.",
                failure_or_risk=None,
            )

        if execution.execution_status == "blocked":
            return OutcomeAssessment(
                progress="No world progress this turn.",
                local_reasonableness=(
                    "The local choice may still be understandable under delayed communication or partial visibility."
                ),
                failure_or_risk=execution.result,
            )

        if execution.execution_status == "invalid":
            return OutcomeAssessment(
                progress="No world progress this turn.",
                local_reasonableness="This action should not have been selected by the rescue policy.",
                failure_or_risk=execution.result,
            )

        return OutcomeAssessment(
            progress=execution.result,
            local_reasonableness="Action fits the deterministic rescue policy under partial observability.",
            failure_or_risk=None,
        )

    def _attribute(self, diagnostics, execution: ActionExecution) -> Attribution:
        if execution.execution_status in {"blocked", "invalid"}:
            if diagnostics.stale_beliefs:
                return Attribution(
                    primary_source="communication",
                    note="Failure likely came from stale or delayed communicated beliefs.",
                )
            return Attribution(
                primary_source="agent",
                note="Failure came from the local policy choice on this turn.",
            )

        if diagnostics.stale_beliefs:
            return Attribution(
                primary_source="communication",
                note="Delayed communication shaped the turn but did not break it.",
            )

        return Attribution(
            primary_source="system",
            note="Deterministic rescue policy executed as intended.",
        )

    def _all_exited(self) -> bool:
        return all(state.exited for state in self.world.values())