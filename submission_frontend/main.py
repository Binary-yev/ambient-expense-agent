import os
import json
import asyncio
import logging
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import vertexai
from vertexai.preview.reasoning_engines import ReasoningEngine
from google.cloud.aiplatform_v1beta1 import types as aip_types
from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Manager Expense Approval Dashboard")

# Read deployment metadata with local, parent, and env fallback
metadata_path = os.path.join(os.path.dirname(__file__), "deployment_metadata.json")
if not os.path.exists(metadata_path):
    metadata_path = os.path.join(os.path.dirname(__file__), "..", "deployment_metadata.json")

if not os.path.exists(metadata_path):
    PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
    AGENT_RUNTIME_ID = os.environ.get("AGENT_RUNTIME_ID")
    LOCATION = os.environ.get("LOCATION", "us-east1")
    if not PROJECT_ID or not AGENT_RUNTIME_ID:
        raise ValueError("Neither deployment_metadata.json nor GOOGLE_CLOUD_PROJECT/AGENT_RUNTIME_ID environment variables are set.")
    runtime_id_str = f"projects/{PROJECT_ID}/locations/{LOCATION}/reasoningEngines/{AGENT_RUNTIME_ID}"
else:
    with open(metadata_path, "r") as f:
        metadata = json.load(f)
    runtime_id_str = metadata["remote_agent_runtime_id"]
    parts = runtime_id_str.split("/")
    PROJECT_ID = parts[1]
    LOCATION = parts[3]
    AGENT_RUNTIME_ID = parts[5]

logger.info(f"Loaded config: Project={PROJECT_ID}, Location={LOCATION}, AgentID={AGENT_RUNTIME_ID}")

# Initialize Vertex AI
vertexai.init(project=PROJECT_ID, location=LOCATION)
engine = ReasoningEngine(runtime_id_str)
session_service = VertexAiSessionService(
    project=PROJECT_ID,
    location=LOCATION,
    agent_engine_id=AGENT_RUNTIME_ID
)

class ActionPayload(BaseModel):
    approved: bool
    interrupt_id: str
    user_id: str

@app.get("/api/pending")
async def get_pending_approvals():
    try:
        logger.info("Listing sessions from VertexAiSessionService...")
        response = await session_service.list_sessions(app_name=runtime_id_str)
        
        pending_approvals = []
        for s in response.sessions:
            logger.info(f"Checking session {s.id} (user: {s.user_id})...")
            # Fetch the full session to retrieve history
            full_session = await session_service.get_session(
                app_name="ambient_expense_agent",
                user_id=s.user_id,
                session_id=s.id
            )
            
            # Find unresolved adk_request_input events
            unresolved_call = None
            for event in full_session.events:
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        # Check for request input call
                        if getattr(part, "function_call", None) and part.function_call.name == "adk_request_input":
                            unresolved_call = {
                                "id": part.function_call.id,
                                "args": part.function_call.args
                            }
                        # Check for response
                        if getattr(part, "function_response", None) and part.function_response.name == "adk_request_input":
                            if unresolved_call and unresolved_call["id"] == part.function_response.id:
                                unresolved_call = None
            
            if unresolved_call:
                expense = full_session.state.get("expense", {})
                risk_review = full_session.state.get("risk_review", {})
                
                # Format a friendly summary message if not in args
                message = unresolved_call["args"].get("message", "")
                if not message:
                    message = f"Expense of ${expense.get('amount', 0.0):.2f} by {expense.get('submitter', 'Unknown')} requires manual approval."
                
                pending_approvals.append({
                    "session_id": s.id,
                    "user_id": s.user_id,
                    "interrupt_id": unresolved_call["id"],
                    "message": message,
                    "expense": expense,
                    "risk_review": risk_review
                })
        
        logger.info(f"Found {len(pending_approvals)} pending approvals.")
        return pending_approvals
    except Exception as e:
        logger.error(f"Error fetching pending approvals: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/action/{session_id}")
async def action_approval(session_id: str, payload: ActionPayload):
    try:
        logger.info(f"Resuming session {session_id} for user {payload.user_id} with approved={payload.approved}...")
        
        resume_payload = {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "id": payload.interrupt_id,
                        "name": "adk_request_input",
                        "response": {"approved": payload.approved}
                    }
                }
            ]
        }
        
        req = aip_types.StreamQueryReasoningEngineRequest(
            name=engine.resource_name,
            input={
                "user_id": payload.user_id,
                "session_id": session_id,
                "message": resume_payload
            },
            class_method="stream_query"
        )
        
        # Define synchronous execution generator worker
        def run_stream():
            res = engine.execution_api_client.stream_query_reasoning_engine(request=req)
            events = []
            for r in res:
                if r.data:
                    event_str = r.data.decode("utf-8").strip()
                    if event_str:
                        events.append(json.loads(event_str))
            return events

        # Run stream query in worker thread to prevent blocking
        events = await asyncio.to_thread(run_stream)
        logger.info(f"Session {session_id} resumed successfully. Received {len(events)} events.")
        
        return {"status": "success", "events": events}
    except Exception as e:
        logger.error(f"Error resuming session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Manager Approval Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: rgba(255, 255, 255, 0.03);
            --card-border: rgba(255, 255, 255, 0.07);
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            --accent-glow: radial-gradient(circle, rgba(99, 102, 241, 0.15) 0%, rgba(0, 0, 0, 0) 70%);
            --accent-color: #6366f1;
            --emerald-glow: 0 0 20px rgba(16, 185, 129, 0.4);
            --rose-glow: 0 0 20px rgba(244, 63, 94, 0.4);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-primary);
            font-family: 'Outfit', sans-serif;
            min-height: 100vh;
            overflow-x: hidden;
            background-image: 
                radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(244, 63, 94, 0.12) 0px, transparent 50%);
            background-attachment: fixed;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 40px 20px;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 40px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 20px;
        }

        .logo-area h1 {
            font-size: 28px;
            font-weight: 700;
            background: linear-gradient(135deg, #a5b4fc 0%, #6366f1 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.5px;
        }

        .logo-area p {
            font-size: 14px;
            color: var(--text-secondary);
            margin-top: 4px;
        }

        .refresh-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--card-border);
            color: var(--text-primary);
            padding: 10px 20px;
            border-radius: 30px;
            cursor: pointer;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
            backdrop-filter: blur(5px);
        }

        .refresh-btn:hover {
            background: rgba(255, 255, 255, 0.1);
            transform: translateY(-2px);
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
            gap: 30px;
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 24px;
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            position: relative;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .card:hover {
            transform: translateY(-6px);
            border-color: rgba(255, 255, 255, 0.15);
            box-shadow: 0 20px 40px rgba(0,0,0,0.4), 0 0 20px rgba(99, 102, 241, 0.1);
        }

        .card::before {
            content: '';
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: var(--accent-glow);
            pointer-events: none;
            opacity: 0.5;
            transition: opacity 0.3s ease;
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
            position: relative;
            z-index: 1;
        }

        .submitter-info {
            display: flex;
            flex-direction: column;
        }

        .submitter-name {
            font-size: 18px;
            font-weight: 600;
            color: var(--text-primary);
        }

        .submitter-role {
            font-size: 12px;
            color: var(--text-secondary);
        }

        .amount-tag {
            font-size: 22px;
            font-weight: 700;
            color: #10b981;
            text-shadow: 0 0 10px rgba(16, 185, 129, 0.2);
        }

        .card-body {
            position: relative;
            z-index: 1;
            margin-bottom: 24px;
        }

        .detail-row {
            display: flex;
            justify-content: space-between;
            margin-bottom: 8px;
            font-size: 14px;
        }

        .detail-label {
            color: var(--text-secondary);
        }

        .detail-value {
            font-weight: 500;
            color: var(--text-primary);
        }

        .description {
            font-size: 14px;
            color: var(--text-secondary);
            background: rgba(255, 255, 255, 0.02);
            padding: 12px;
            border-radius: 10px;
            margin-top: 12px;
            border: 1px solid rgba(255,255,255,0.03);
            line-height: 1.4;
        }

        .risk-badge {
            display: inline-flex;
            align-items: center;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            margin-top: 12px;
        }

        .risk-low {
            background: rgba(16, 185, 129, 0.1);
            color: #10b981;
            border: 1px solid rgba(16, 185, 129, 0.2);
        }

        .risk-med {
            background: rgba(245, 158, 11, 0.1);
            color: #f59e0b;
            border: 1px solid rgba(245, 158, 11, 0.2);
        }

        .risk-high {
            background: rgba(239, 68, 68, 0.1);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.2);
        }

        .risk-factors {
            font-size: 12px;
            color: #fca5a5;
            margin-top: 8px;
            list-style-type: none;
        }

        .risk-factors li {
            position: relative;
            padding-left: 12px;
            margin-bottom: 4px;
        }

        .risk-factors li::before {
            content: '•';
            position: absolute;
            left: 0;
            color: #ef4444;
        }

        .card-actions {
            display: flex;
            gap: 12px;
            position: relative;
            z-index: 1;
        }

        .btn {
            flex: 1;
            padding: 12px;
            border-radius: 12px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            text-align: center;
        }

        .btn-approve {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            border: none;
            color: white;
        }

        .btn-approve:hover {
            box-shadow: var(--emerald-glow);
            transform: translateY(-2px);
        }

        .btn-reject {
            background: transparent;
            border: 1px solid rgba(244, 63, 94, 0.3);
            color: #f43f5e;
        }

        .btn-reject:hover {
            background: rgba(244, 63, 94, 0.05);
            border-color: #f43f5e;
            box-shadow: var(--rose-glow);
            transform: translateY(-2px);
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            transform: none !important;
            box-shadow: none !important;
        }

        .empty-state {
            grid-column: 1 / -1;
            text-align: center;
            padding: 60px;
            background: var(--card-bg);
            border: 1px dashed var(--card-border);
            border-radius: 20px;
            color: var(--text-secondary);
        }

        .empty-state h3 {
            font-size: 20px;
            color: var(--text-primary);
            margin-bottom: 8px;
        }

        /* Spinner */
        .spinner {
            border: 2px solid rgba(255, 255, 255, 0.1);
            border-top: 2px solid var(--text-primary);
            border-radius: 50%;
            width: 16px;
            height: 16px;
            animation: spin 0.8s linear infinite;
            display: inline-block;
        }

        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }

        .loading-overlay {
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(11, 15, 25, 0.8);
            display: flex;
            justify-content: center;
            align-items: center;
            z-index: 10;
            border-radius: 20px;
            backdrop-filter: blur(4px);
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s ease;
        }

        .loading-overlay.active {
            opacity: 1;
            pointer-events: auto;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-area">
                <h1>Expense Approvals</h1>
                <p>Dashboard for Manager Decision Intercepts</p>
            </div>
            <button class="refresh-btn" onclick="fetchPendingApprovals()">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
                Refresh
            </button>
        </header>

        <div class="grid" id="approvals-grid">
            <div class="empty-state">
                <div class="spinner" style="width: 30px; height: 30px; margin-bottom: 12px;"></div>
                <h3>Loading approvals...</h3>
                <p>Connecting to Vertex AI Session Service</p>
            </div>
        </div>
    </div>

    <script>
        async function fetchPendingApprovals() {
            const grid = document.getElementById('approvals-grid');
            grid.innerHTML = `
                <div class="empty-state">
                    <div class="spinner" style="width: 30px; height: 30px; margin-bottom: 12px;"></div>
                    <h3>Loading approvals...</h3>
                    <p>Connecting to Vertex AI Session Service</p>
                </div>
            `;

            try {
                const res = await fetch('/api/pending');
                const data = await res.json();
                
                if (data.length === 0) {
                    grid.innerHTML = `
                        <div class="empty-state">
                            <h3>No Pending Approvals</h3>
                            <p>All expense reports are fully processed.</p>
                        </div>
                    `;
                    return;
                }

                grid.innerHTML = '';
                data.forEach(item => {
                    const expense = item.expense || {};
                    const risk = item.risk_review || {};
                    const riskScore = risk.risk_score || 0;
                    
                    let riskClass = 'risk-low';
                    let riskText = 'Low Risk';
                    if (riskScore >= 4) {
                        riskClass = 'risk-high';
                        riskText = `High Risk (${riskScore}/5)`;
                    } else if (riskScore >= 2) {
                        riskClass = 'risk-med';
                        riskText = `Medium Risk (${riskScore}/5)`;
                    }

                    const card = document.createElement('div');
                    card.className = 'card';
                    card.id = `card-${item.session_id}`;
                    
                    // Risk factors list
                    let riskFactorsHtml = '';
                    if (risk.risk_factors && risk.risk_factors.length > 0) {
                        riskFactorsHtml = `
                            <ul class="risk-factors">
                                ${risk.risk_factors.map(f => `<li>${f}</li>`).join('')}
                            </ul>
                        `;
                    }

                    card.innerHTML = `
                        <div class="loading-overlay" id="overlay-${item.session_id}">
                            <div style="text-align: center;">
                                <div class="spinner" style="width: 30px; height: 30px; margin-bottom: 8px;"></div>
                                <div style="font-size: 14px; font-weight: 500;">Resuming session...</div>
                            </div>
                        </div>
                        <div>
                            <div class="card-header">
                                <div class="submitter-info">
                                    <span class="submitter-name">${expense.submitter || 'Unknown'}</span>
                                    <span class="submitter-role">Submitter</span>
                                </div>
                                <span class="amount-tag">$${parseFloat(expense.amount || 0).toFixed(2)}</span>
                            </div>
                            <div class="card-body">
                                <div class="detail-row">
                                    <span class="detail-label">Category</span>
                                    <span class="detail-value">${expense.category || 'Other'}</span>
                                </div>
                                <div class="detail-row">
                                    <span class="detail-label">Date</span>
                                    <span class="detail-value">${expense.date || 'N/A'}</span>
                                </div>
                                <div class="detail-row" style="margin-top: 10px;">
                                    <span class="detail-label">Risk Evaluation</span>
                                    <span class="risk-badge ${riskClass}">${riskText}</span>
                                </div>
                                ${riskFactorsHtml}
                                <div class="description">
                                    <strong>Description:</strong><br>
                                    ${expense.description || 'No description provided.'}
                                </div>
                            </div>
                        </div>
                        <div class="card-actions">
                            <button class="btn btn-reject" onclick="handleAction('${item.session_id}', '${item.interrupt_id}', '${item.user_id}', false)">Reject</button>
                            <button class="btn btn-approve" onclick="handleAction('${item.session_id}', '${item.interrupt_id}', '${item.user_id}', true)">Approve</button>
                        </div>
                    `;
                    grid.appendChild(card);
                });
            } catch (err) {
                console.error(err);
                grid.innerHTML = `
                    <div class="empty-state" style="border-color: rgba(239, 68, 68, 0.2);">
                        <h3 style="color: #ef4444;">Error Loading Approvals</h3>
                        <p>${err.message || 'Check backend logs.'}</p>
                    </div>
                `;
            }
        }

        async function handleAction(sessionId, interruptId, userId, approved) {
            const overlay = document.getElementById(`overlay-${sessionId}`);
            overlay.classList.add('active');

            try {
                const res = await fetch(`/api/action/${sessionId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        approved: approved,
                        interrupt_id: interruptId,
                        user_id: userId
                    })
                });
                
                const data = await res.json();
                if (data.status === 'success') {
                    // Remove card from UI with a nice fadeout
                    const card = document.getElementById(`card-${sessionId}`);
                    card.style.opacity = '0';
                    card.style.transform = 'scale(0.9)';
                    setTimeout(() => {
                        card.remove();
                        // Check if grid is empty now
                        const grid = document.getElementById('approvals-grid');
                        if (grid.children.length === 0) {
                            grid.innerHTML = `
                                <div class="empty-state">
                                    <h3>No Pending Approvals</h3>
                                    <p>All expense reports are fully processed.</p>
                                </div>
                            `;
                        }
                    }, 300);
                } else {
                    alert('Failed to process approval: ' + JSON.stringify(data));
                    overlay.classList.remove('active');
                }
            } catch (err) {
                console.error(err);
                alert('Error submitting action: ' + err.message);
                overlay.classList.remove('active');
            }
        }

        // Fetch approvals on load
        window.addEventListener('DOMContentLoaded', fetchPendingApprovals);
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)
