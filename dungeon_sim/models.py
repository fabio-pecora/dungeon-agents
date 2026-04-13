from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"


class AgentAction(BaseModel):
    action_type: Literal["move", "pick_up", "unlock", "send_message", "wait"]
    direction: Direction | None = None
    text: str | None = None


class AgentDecision(BaseModel):
    intent_summary: str = Field(max_length=180)
    rationale: str = Field(max_length=500)
    action: AgentAction


class CellView(BaseModel):
    position: tuple[int, int]
    tile_type: str
    visible: bool = True
    contains: list[str] = Field(default_factory=list)


class Observation(BaseModel):
    self_position: tuple[int, int]
    visible_cells: list[CellView]
    adjacent_locked_door: bool = False
    standing_on_key: bool = False
    standing_on_exit: bool = False
    inventory: list[str] = Field(default_factory=list)


class BeliefObject(BaseModel):
    position: tuple[int, int] | None = None
    status: str | None = None
    source: str | None = None
    age: int | None = None


class BeliefState(BaseModel):
    believed_position: tuple[int, int]
    visited_cells: list[tuple[int, int]]
    known_cells: dict[str, str]
    key_belief: BeliefObject = Field(default_factory=BeliefObject)
    door_belief: BeliefObject = Field(default_factory=BeliefObject)
    exit_belief: BeliefObject = Field(default_factory=BeliefObject)
    recent_messages: list[str] = Field(default_factory=list)


class BeliefDiagnostics(BaseModel):
    stale_beliefs: list[str] = Field(default_factory=list)
    wrong_beliefs: list[str] = Field(default_factory=list)
    confidence_notes: list[str] = Field(default_factory=list)


class MessageEvent(BaseModel):
    delivered_this_turn: list[str] = Field(default_factory=list)
    sent_this_turn: list[str] = Field(default_factory=list)
    inbox_size_after_delivery: int = 0


class ActionExecution(BaseModel):
    requested_action: dict[str, Any]
    execution_status: Literal["executed", "blocked", "invalid"]
    result: str
    tool_output: dict[str, Any] = Field(default_factory=dict)


class OutcomeAssessment(BaseModel):
    progress: str
    local_reasonableness: str
    failure_or_risk: str | None = None


class Attribution(BaseModel):
    primary_source: Literal["agent", "communication", "system", "none"]
    note: str


class AgentStepEvent(BaseModel):
    event_type: Literal["agent_step"] = "agent_step"
    run_id: str
    turn: int
    agent_id: str
    observation: Observation
    belief_state: BeliefState
    belief_diagnostics: BeliefDiagnostics
    messages: MessageEvent
    decision: AgentDecision
    action_execution: ActionExecution
    outcome_assessment: OutcomeAssessment
    attribution: Attribution


class RunSummary(BaseModel):
    run_id: str
    seed: int | None = None
    model: str
    turn_limit: int
    turns_executed: int
    success: bool
    outcome: str
    both_agents_exited: bool
    incidents: list[str] = Field(default_factory=list)
    communication_findings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    final_agent_states: dict[str, Any] = Field(default_factory=dict)
