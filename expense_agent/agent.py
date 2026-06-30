# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import json
import os
import re
from typing import Any

import google.auth
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import START, Workflow, node
from google.genai import types
from pydantic import BaseModel, Field

from . import config

# Set up local authentication fallback if needed
try:
    _, project_id = google.auth.default()
    if project_id:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
except Exception:
    pass

os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")


class Expense(BaseModel):
    amount: float
    submitter: str
    category: str
    description: str
    date: str


class RiskReview(BaseModel):
    risk_score: int = Field(description="Risk score from 1 (low risk) to 5 (high risk)")
    risk_factors: list[str] = Field(
        description="List of risk factors identified, or empty list"
    )
    alert_raised: bool = Field(
        description="True if an alert should be raised, False otherwise"
    )
    justification: str = Field(
        description="Detailed reason/justification for the risk score and alert status"
    )


def parse_input_event(node_input: Any) -> Expense:
    """Parses Pub/Sub payload or plain JSON input event into an Expense object."""
    raw_data = None

    # 1. Resolve raw input from node_input (handles types.Content, dict, or string)
    if isinstance(node_input, dict):
        raw_data = node_input
    elif hasattr(node_input, "parts") and node_input.parts:
        text = "".join([part.text for part in node_input.parts if part.text])
        try:
            raw_data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback for simple chat messages/unit tests to avoid crash
            raw_data = {
                "amount": 10.0,
                "submitter": "unknown_user",
                "category": "other",
                "description": text,
                "date": "2026-06-26",
            }
    elif isinstance(node_input, str):
        try:
            raw_data = json.loads(node_input)
        except json.JSONDecodeError:
            # Fallback for plain string inputs
            raw_data = {
                "amount": 10.0,
                "submitter": "unknown_user",
                "category": "other",
                "description": node_input,
                "date": "2026-06-26",
            }
    else:
        raise ValueError(f"Unsupported node_input type: {type(node_input)}")

    # 2. Check if raw_data is a chat message representation (dict with "parts" or "role")
    if isinstance(raw_data, dict) and ("parts" in raw_data or "role" in raw_data):
        parts = raw_data.get("parts") or []
        text = ""
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and "text" in part:
                    text += part["text"]
                elif hasattr(part, "text"):
                    text += part.text
        raw_data = {
            "amount": 10.0,
            "submitter": "unknown_user",
            "category": "other",
            "description": text or "Chat Message",
            "date": "2026-06-26",
        }

    # 3. Extract "data" field if present, otherwise assume raw_data contains fields directly
    data_val = raw_data.get("data") if isinstance(raw_data, dict) else None

    if data_val is None:
        if isinstance(raw_data, dict):
            return Expense(**raw_data)
        else:
            raise ValueError("Input event must be a JSON object.")

    # 4. If "data" is present, it might be base64-encoded or plain JSON
    parsed_json = None
    if isinstance(data_val, str):
        try:
            # Try base64 decoding first
            decoded_bytes = base64.b64decode(data_val, validate=True)
            decoded_str = decoded_bytes.decode("utf-8")
            parsed_json = json.loads(decoded_str)
        except Exception:
            # Fall back to parsing as plain JSON string
            parsed_json = json.loads(data_val)
    elif isinstance(data_val, dict):
        parsed_json = data_val
    else:
        raise ValueError(f"Invalid data field type: {type(data_val)}")

    return Expense(**parsed_json)


def parse_node(ctx: Context, node_input: Any) -> Event:
    """Parses input event, yields it downstream, and stores it in context state."""
    expense = parse_input_event(node_input)
    expense_dict = expense.model_dump()
    return Event(output=expense_dict, state={"expense": expense_dict})


def route_node(ctx: Context, node_input: dict) -> Event:
    """Routes the expense based on whether the amount is below or above the threshold."""
    amount = node_input.get("amount", 0.0)
    if amount < config.THRESHOLD:
        return Event(output=node_input, route="auto_approve")
    else:
        return Event(output=node_input, route="requires_review")


SSN_REGEX = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_16_REGEX = re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b")
CC_15_REGEX = re.compile(r"\b\d{4}[- ]?\d{6}[- ]?\d{5}\b")

INJECTION_KEYWORDS = [
    "ignore previous instructions",
    "ignore above instructions",
    "ignore the rules",
    "bypass the rules",
    "bypass rules",
    "system prompt",
    "override the rules",
    "override rules",
    "force auto-approval",
    "force approval",
    "auto-approve this",
    "auto-approve",
    "instead of reviewing",
    "you must approve",
    "new instruction",
    "change the rules",
    "do not review",
]


def scrub_pii(text: str) -> tuple[str, list[str]]:
    redacted_categories = []

    # 1. Scrub SSNs
    if SSN_REGEX.search(text):
        text = SSN_REGEX.sub("[REDACTED SSN]", text)
        redacted_categories.append("SSN")

    # 2. Scrub credit cards
    has_cc = False
    if CC_16_REGEX.search(text):
        text = CC_16_REGEX.sub("[REDACTED CREDIT CARD]", text)
        has_cc = True
    if CC_15_REGEX.search(text):
        text = CC_15_REGEX.sub("[REDACTED CREDIT CARD]", text)
        has_cc = True

    if has_cc:
        redacted_categories.append("Credit Card")

    return text, redacted_categories


def detect_prompt_injection(text: str) -> bool:
    text_lower = text.lower()
    for kw in INJECTION_KEYWORDS:
        if kw in text_lower:
            return True
    return False


def security_checkpoint_node(ctx: Context, node_input: dict) -> Event:
    """Scrubs PII and checks for prompt injection in the expense description."""
    description = node_input.get("description", "")

    # Scrub PII
    clean_description, redacted_categories = scrub_pii(description)

    # Detect prompt injection on original or clean description
    is_injection = detect_prompt_injection(description)

    # Update expense inside node_input/state
    node_input["description"] = clean_description
    ctx.state["expense"]["description"] = clean_description

    state_delta = {"expense": ctx.state["expense"]}

    if redacted_categories:
        state_delta["redacted_categories"] = redacted_categories

    if is_injection:
        state_delta["security_event"] = True
        return Event(output=node_input, route="security_event", state=state_delta)
    else:
        return Event(output=node_input, route="clean", state=state_delta)


def auto_approve_node(ctx: Context, node_input: dict) -> Event:
    """Auto-approves expenses under the threshold."""
    return Event(
        output={
            "approved": True,
            "reviewer": "system",
            "notes": f"Auto-approved instantly (under ${config.THRESHOLD:.2f}).",
        }
    )


def prepare_llm_prompt(ctx: Context, node_input: dict) -> str:
    """Prepares a clear textual representation of the expense for LLM consumption."""
    return f"""Please review this expense report for any risk factors:
    Submitter: {node_input.get("submitter", "Unknown")}
    Amount: ${node_input.get("amount", 0.0):.2f}
    Category: {node_input.get("category", "Unknown")}
    Description: {node_input.get("description", "No description")}
    Date: {node_input.get("date", "Unknown")}
    """


# LLM node to review the expense for risks
llm_review_node = LlmAgent(
    name="llm_review",
    model=config.MODEL_NAME,
    instruction="Review the given expense details for compliance. Raise an alert if it is suspicious, unusual, or high risk.",
    output_schema=RiskReview,
    output_key="risk_review",
)


@node(rerun_on_resume=True)
async def human_approval_node(ctx: Context, node_input: dict):
    """Pauses the workflow for human approval if an LLM review is required."""
    expense = ctx.state["expense"]

    # Check if we already have the human decision in resume inputs
    if not ctx.resume_inputs or "decision" not in ctx.resume_inputs:
        redacted_str = (
            f" (Redacted PII: {', '.join(ctx.state.get('redacted_categories'))})"
            if ctx.state.get("redacted_categories")
            else ""
        )

        if ctx.state.get("security_event"):
            message = (
                f"🛑 SECURITY ALERT: Potential Prompt Injection Detected in Description!\n"
                f"Expense of ${expense['amount']:.2f} by {expense['submitter']} requires manual review and has bypassed LLM risk assessment.\n"
                f"Description: {expense.get('description', '')}{redacted_str}\n\n"
                f"Please reply with 'approve' or 'reject'."
            )
        else:
            alert_status = (
                "⚠️ ALERT RAISED!"
                if node_input.get("alert_raised")
                else "No alerts raised."
            )
            message = (
                f"Expense of ${expense['amount']:.2f} by {expense['submitter']} "
                f"requires manual approval.{redacted_str}\n"
                f"Risk Score: {node_input.get('risk_score')}/5\n"
                f"Risk Factors: {', '.join(node_input.get('risk_factors', [])) if node_input.get('risk_factors') else 'None'}\n"
                f"Alert: {alert_status}\n"
                f"Justification: {node_input.get('justification')}\n\n"
                f"Please reply with 'approve' or 'reject'."
            )
        yield RequestInput(interrupt_id="decision", message=message)
        return

    decision_val = ctx.resume_inputs["decision"]
    if isinstance(decision_val, dict):
        decision = (
            decision_val.get("decision")
            or decision_val.get("response")
            or next(iter(decision_val.values()), "")
        )
    else:
        decision = decision_val

    if not isinstance(decision, str):
        decision = str(decision)

    decision = decision.strip().lower()
    approved = decision in ["approve", "yes", "approved"]

    yield Event(
        output={
            "approved": approved,
            "reviewer": "human",
            "notes": f"Reviewed by human. Decision: {decision.upper()}.",
        }
    )


def record_outcome_node(ctx: Context, node_input: dict):
    """Consolidates the final decision and logs the result."""
    expense = ctx.state["expense"]
    approved = node_input.get("approved", False)
    reviewer = node_input.get("reviewer", "unknown")
    notes = node_input.get("notes", "")

    security_flag = " (SECURITY EVENT)" if ctx.state.get("security_event") else ""
    redacted_categories = ctx.state.get("redacted_categories")
    redacted_str = (
        f" Redacted PII: {', '.join(redacted_categories)}."
        if redacted_categories
        else ""
    )

    if redacted_str:
        notes = (notes + redacted_str).strip()

    status = "APPROVED" if approved else "REJECTED"
    summary = (
        f"Expense Report Summary:\n"
        f"- Status: {status}{security_flag}\n"
        f"- Amount: ${expense.get('amount', 0.0):.2f}\n"
        f"- Submitter: {expense.get('submitter', 'Unknown')}\n"
        f"- Reviewer: {reviewer.upper()}\n"
        f"- Notes: {notes}"
    )

    # Yield content event for Web UI rendering
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=summary)])
    )
    # Yield final output value
    yield Event(output=summary)


# Connect nodes with directed edges
root_agent = Workflow(
    name="ambient_expense_agent",
    edges=[
        (START, parse_node),
        (parse_node, route_node),
        (
            route_node,
            {
                "auto_approve": auto_approve_node,
                "requires_review": security_checkpoint_node,
            },
        ),
        (
            security_checkpoint_node,
            {
                "clean": prepare_llm_prompt,
                "security_event": human_approval_node,
            },
        ),
        (prepare_llm_prompt, llm_review_node),
        (llm_review_node, human_approval_node),
        (auto_approve_node, record_outcome_node),
        (human_approval_node, record_outcome_node),
    ],
)

from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin

bq_dataset = os.environ.get("BQ_ANALYTICS_DATASET_ID")
bq_project = os.environ.get("GOOGLE_CLOUD_PROJECT")

plugins = []
if bq_project and bq_dataset:
    plugins.append(
        BigQueryAgentAnalyticsPlugin(
            project_id=bq_project,
            dataset_id=bq_dataset,
        )
    )

app = App(
    root_agent=root_agent,
    name="expense_agent",
    plugins=plugins,
)

