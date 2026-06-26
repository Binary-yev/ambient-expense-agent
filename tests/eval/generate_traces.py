import asyncio
import json
import os
import sys

from dotenv import load_dotenv

# Load environment variables
load_dotenv(
    dotenv_path="c:/Users/yyguy/OneDrive/Desktop/yev/ambient_expense_agent/.env"
)

from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from expense_agent.agent import root_agent

sys.stdout.reconfigure(encoding="utf-8")

# Ensure output directory exists
os.makedirs("artifacts/traces", exist_ok=True)


def build_turns(events, user_id):
    turns = []
    current_turn_events = []
    turn_index = 0

    for e in events:
        if not getattr(e, "content", None):
            continue

        # Serialize the Content object
        content_dict = json.loads(e.content.model_dump_json())
        if not content_dict.get("parts"):
            continue

        role = content_dict.get("role", "model")
        author = getattr(e, "author", "ambient_expense_agent")

        # Map user role/author to "user"
        if role == "user" or author == user_id:
            author = "user"

        event_dict = {"author": author, "content": content_dict}

        # Start a new turn if author is 'user' and we have prior events
        if author == "user" and current_turn_events:
            turns.append({"turn_index": turn_index, "events": current_turn_events})
            turn_index += 1
            current_turn_events = []

        current_turn_events.append(event_dict)

    if current_turn_events:
        turns.append({"turn_index": turn_index, "events": current_turn_events})

    return turns


async def run_case(case):
    case_id = case["eval_case_id"]
    payload_text = case["prompt"]["parts"][0]["text"]

    print(f"\n--- Running Case: {case_id} ---")
    app = App(name="expense_agent", root_agent=root_agent)
    runner = InMemoryRunner(app=app)

    user_id = "eval_user"
    session_id = f"sess_{case_id}"

    # Try to delete session if it exists from a previous run to avoid AlreadyExistsError
    try:
        await runner.session_service.delete_session(
            app_name="expense_agent", user_id=user_id, session_id=session_id
        )
    except Exception:
        pass

    sess = await runner.session_service.create_session(
        app_name="expense_agent", user_id=user_id, session_id=session_id
    )
    message = types.Content(
        role="user", parts=[types.Part.from_text(text=payload_text)]
    )

    interrupted_id = None
    async for event in runner.run_async(
        user_id=user_id, session_id=sess.id, new_message=message
    ):
        if event.long_running_tool_ids:
            interrupted_id = list(event.long_running_tool_ids)[0]

    # If the workflow paused for human input, automate the decision
    if interrupted_id:
        # Determine decision: reject prompt injections, approve others
        # We can look at the case_id or check if the prompt description has injection keywords
        is_injection = (
            "injection" in case_id
            or "IGNORE PREVIOUS RULES" in payload_text
            or "Bypass all rules" in payload_text
        )
        decision = "reject" if is_injection else "approve"
        print(f"Workflow paused. Automated human decision: '{decision.upper()}'")

        resume_msg = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="adk_request_input",
                        id=interrupted_id,
                        response={"decision": decision},
                    )
                )
            ],
        )
        async for resume_event in runner.run_async(
            user_id=user_id, session_id=sess.id, new_message=resume_msg
        ):
            pass

    # Retrieve updated session events
    sess_updated = await runner.session_service.get_session(
        app_name="expense_agent", user_id=user_id, session_id=sess.id
    )
    turns = build_turns(sess_updated.events, user_id)

    agent_data = {
        "agents": {
            "ambient_expense_agent": {"agent_id": "ambient_expense_agent"},
            "llm_review": {"agent_id": "llm_review"},
        },
        "turns": turns,
    }

    # Extract final text response for the 'response' field in EvalCase
    response_candidate = None
    for turn in reversed(turns):
        for event in reversed(turn["events"]):
            if (
                event["author"] == "ambient_expense_agent"
                and "text" in event["content"]["parts"][0]
            ):
                response_candidate = event["content"]
                break
        if response_candidate:
            break

    case_trace = {
        "eval_case_id": case_id,
        "prompt": case["prompt"],
        "agent_data": agent_data,
    }

    if response_candidate:
        case_trace["responses"] = [{"response": response_candidate}]

    return case_trace


async def main():
    # Load basic dataset
    with open("tests/eval/datasets/basic-dataset.json", encoding="utf-8") as f:
        dataset = json.load(f)

    traces = []
    for case in dataset["eval_cases"]:
        trace = await run_case(case)
        traces.append(trace)

    output_path = "artifacts/traces/generated_traces.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"eval_cases": traces}, f, indent=2)

    print(f"\nSuccessfully generated traces and saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
