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
import logging
import os
import uuid
from contextlib import aclosing

# Define AGENT_DIR early to locate files properly
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(AGENT_DIR, ".env"), override=True)

import google.auth
from fastapi import FastAPI, HTTPException
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.service_factory import create_session_service_from_options
from google.adk.runners import Runner
from google.genai import types
from pydantic import BaseModel

from expense_agent.agent import root_agent
from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback

# Configure standard Python logging for console logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info(
    "Loaded Environment - GOOGLE_CLOUD_PROJECT: %s", os.getenv("GOOGLE_CLOUD_PROJECT")
)
logger.info(
    "Loaded Environment - GOOGLE_CLOUD_LOCATION: %s", os.getenv("GOOGLE_CLOUD_LOCATION")
)
logger.info(
    "Loaded Environment - GOOGLE_GENAI_USE_VERTEXAI: %s",
    os.getenv("GOOGLE_GENAI_USE_VERTEXAI"),
)

setup_telemetry()
_, project_id = google.auth.default()

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Artifact bucket for ADK (created by Terraform, passed via env var)
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
# In-memory session configuration - no persistent storage
session_service_uri = None

artifact_service_uri = f"gs://{logs_bucket_name}" if logs_bucket_name else None

# Initialize session service and runner for the custom trigger endpoint
session_service = create_session_service_from_options(
    base_dir=AGENT_DIR,
    session_service_uri=session_service_uri,
)
runner = Runner(
    agent=root_agent, session_service=session_service, app_name="expense_agent"
)

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,  # Expose Dev UI for manual human-in-the-loop reviews
    artifact_service_uri=artifact_service_uri,
    allow_origins=allow_origins,
    session_service_uri=session_service_uri,
    otel_to_cloud=False,  # Telemetry checklist requirement
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"


class PubSubMessage(BaseModel):
    data: str | None = None
    attributes: dict[str, str] | None = None
    messageId: str | None = None


class PubSubTriggerRequest(BaseModel):
    message: PubSubMessage
    subscription: str | None = None


@app.post("/apps/{app_name}/trigger/pubsub")
async def trigger_pubsub(app_name: str, req: PubSubTriggerRequest):
    """Processes messages from Pub/Sub and feeds them into the workflow."""
    if app_name != "expense_agent":
        raise HTTPException(status_code=404, detail="App not found")

    # Normalize subscription name
    subscription = req.subscription or "pubsub-caller"
    if "/" in subscription:
        subscription = subscription.split("/")[-1]
    user_id = subscription

    logger.info(
        "Pub/Sub trigger: subscription=%s (normalized user_id=%s)",
        req.subscription,
        user_id,
    )

    # Decode Pub/Sub message data
    data_payload = None
    if req.message.data:
        try:
            decoded_bytes = base64.b64decode(req.message.data)
            decoded_str = decoded_bytes.decode("utf-8")
            try:
                data_payload = json.loads(decoded_str)
            except json.JSONDecodeError:
                data_payload = decoded_str
        except Exception as e:
            logger.exception("Failed to decode Pub/Sub message data")
            raise HTTPException(
                status_code=400,
                detail=f"Invalid base64 message data: {e}",
            ) from e

    # Format trigger payload for the workflow
    message_text = json.dumps({"data": data_payload})
    session_id = str(uuid.uuid4())

    # Create the session
    session = await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )

    new_message = types.Content(
        role="user", parts=[types.Part.from_text(text=message_text)]
    )

    logger.info("Running agent workflow in ambient mode for session=%s", session_id)
    events = []
    async with aclosing(
        runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=new_message,
        )
    ) as agen:
        async for event in agen:
            events.append(event)

    return {"status": "success"}


@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    """Collect and log feedback.

    Args:
        feedback: The feedback data to log

    Returns:
        Success message
    """
    logger.info("Received feedback: %s", feedback.model_dump())
    return {"status": "success"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
