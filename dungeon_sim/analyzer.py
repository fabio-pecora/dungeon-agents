from __future__ import annotations

from collections import Counter, defaultdict

from .models import RunSummary


def build_summary(
    *,
    run_id: str,
    model: str,
    turn_limit: int,
    turns_executed: int,
    events: list[dict],
    final_agent_states: dict,
    seed: int | None = None,
) -> RunSummary:
    both_agents_exited = all(state.get("exited", False) for state in final_agent_states.values())

    incidents: list[str] = []
    communication_findings: list[str] = []
    recommendations: list[str] = []

    blocked_moves = 0
    invalid_actions = 0
    stale_events = 0
    send_events = 0
    critical_messages_sent = 0
    repeated_failure_counter: Counter[str] = Counter()
    blocked_by_agent: dict[str, int] = defaultdict(int)
    explore_after_critical_known = 0

    for event in events:
        turn = event["turn"]
        agent_id = event["agent_id"]
        diagnostics = event["belief_diagnostics"]
        execution = event["action_execution"]
        messages = event["messages"]
        decision = event["decision"]
        belief_state = event["belief_state"]

        key_known = belief_state["key_belief"]["position"] is not None
        door_known = belief_state["door_belief"]["position"] is not None
        exit_known = belief_state["exit_belief"]["position"] is not None
        knows_multiple_critical_facts = sum([key_known, door_known, exit_known]) >= 2

        intent_text = (
            (decision.get("intent_summary") or "") + " " + (decision.get("rationale") or "")
        ).lower()

        if diagnostics["stale_beliefs"]:
            stale_events += 1
            for item in diagnostics["stale_beliefs"][:2]:
                incidents.append(f"Turn {turn} {agent_id}: {item}")

        if execution["execution_status"] == "blocked":
            blocked_moves += 1
            blocked_by_agent[agent_id] += 1
            repeated_failure_counter[execution["result"]] += 1
            incidents.append(f"Turn {turn} {agent_id}: {execution['result']}")

        if execution["execution_status"] == "invalid":
            invalid_actions += 1
            incidents.append(f"Turn {turn} {agent_id}: {execution['result']}")

        if messages["sent_this_turn"]:
            send_events += 1
            for msg in messages["sent_this_turn"]:
                if msg.startswith(("KEY ", "DOOR ", "EXIT ")):
                    critical_messages_sent += 1

        if messages["delivered_this_turn"]:
            communication_findings.append(
                f"Turn {turn} {agent_id} received {len(messages['delivered_this_turn'])} delayed message(s)."
            )

        if knows_multiple_critical_facts and any(
            word in intent_text for word in ["scout", "explore", "frontier", "expand map", "expand"]
        ):
            explore_after_critical_known += 1

    repeated_failures = [
        (result, count)
        for result, count in repeated_failure_counter.items()
        if count >= 2
    ]

    for result, count in repeated_failures[:4]:
        incidents.append(f"Repeated failure x{count}: {result}")

    if explore_after_critical_known >= 3:
        incidents.append(
            f"Agents stayed in exploration mode for {explore_after_critical_known} turns even after multiple critical facts were known."
        )

    if blocked_by_agent:
        for agent_id, count in blocked_by_agent.items():
            if count >= 2:
                incidents.append(
                    f"Agent {agent_id} accumulated {count} blocked actions, suggesting weak action selection or poor correction after failure."
                )

    if critical_messages_sent == 0:
        communication_findings.append(
            "Critical objects were discovered but structured communication appears too weak or too late."
        )
    elif critical_messages_sent < 3:
        communication_findings.append(
            "Some structured communication happened, but critical facts were not shared aggressively enough."
        )

    if not both_agents_exited:
        recommendations.append(
            "Tighten the prompt so agents switch from exploration to completion once key, door, and exit knowledge is sufficient."
        )

    if blocked_moves or invalid_actions:
        recommendations.append(
            "Keep legal-action selection strict and discourage repeating blocked or invalid actions after a failure."
        )

    if critical_messages_sent < 3:
        recommendations.append(
            "Make communication of newly discovered key, door, and exit facts a stronger default priority."
        )

    if explore_after_critical_known >= 3:
        recommendations.append(
            "Reduce generic scouting language in the prompt and explicitly prioritize progress toward known critical targets."
        )

    if stale_events:
        recommendations.append(
            "Emphasize message age in the prompt so agents distrust stale message-only beliefs."
        )

    if not recommendations:
        recommendations.append("Keep the design simple and preserve the trace-first structure.")

    if both_agents_exited:
        outcome = "Both agents reached the exit."
    else:
        outcome = "Run ended before both agents exited."

    return RunSummary(
        run_id=run_id,
        seed=seed,
        model=model,
        turn_limit=turn_limit,
        turns_executed=turns_executed,
        success=both_agents_exited,
        outcome=outcome,
        both_agents_exited=both_agents_exited,
        incidents=incidents[:12],
        communication_findings=communication_findings[:10],
        recommendations=recommendations[:6],
        final_agent_states=final_agent_states,
    )