"""
NEXUS Multi-Agent AI System — FastAPI Backend
=============================================
Architecture:
  Primary Agent (Orchestrator) → [Task Agent | Calendar Agent | Notes Agent]
  Each sub-agent connects to an MCP server and reads/writes SQLite.
  All agents use Anthropic Claude claude-sonnet-4 as their reasoning core.
"""

import os
import json
import uuid
import asyncio
import sqlite3
from datetime import datetime, date
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import anthropic

# ──────────────────────────────────────────────────────────
# 1. APP & DB SETUP
# ──────────────────────────────────────────────────────────

app = FastAPI(
    title="NEXUS Multi-Agent AI System",
    description="Primary agent coordinating task, calendar, and notes sub-agents via MCP + SQLite",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ.get("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"))
DB_PATH = "nexus.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            description TEXT,
            priority    TEXT DEFAULT 'med',
            status      TEXT DEFAULT 'pending',
            due_date    TEXT,
            tags        TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            start_time  TEXT NOT NULL,
            end_time    TEXT NOT NULL,
            location    TEXT,
            attendees   TEXT,
            notes       TEXT,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            content     TEXT NOT NULL,
            tags        TEXT,
            pinned      INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS agent_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT,
            agent_name  TEXT NOT NULL,
            tool_name   TEXT,
            tool_input  TEXT,
            tool_output TEXT,
            duration_ms INTEGER,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            history     TEXT DEFAULT '[]',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


init_db()


# ──────────────────────────────────────────────────────────
# 2. PYDANTIC MODELS
# ──────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = {}


class ChatResponse(BaseModel):
    response: str
    session_id: str
    agents_used: List[str]
    tool_calls: List[Dict[str, Any]]
    workflow_steps: List[str]
    db_changes: Dict[str, int]
    duration_ms: int


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: str = Field("med", pattern="^(high|med|low)$")
    due_date: Optional[str] = None
    tags: Optional[List[str]] = []


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[str] = None


class EventCreate(BaseModel):
    title: str
    start_time: str
    end_time: str
    location: Optional[str] = None
    attendees: Optional[List[str]] = []
    notes: Optional[str] = None


class NoteCreate(BaseModel):
    title: str
    content: str
    tags: Optional[List[str]] = []
    pinned: bool = False


class WorkflowRequest(BaseModel):
    steps: List[Dict[str, Any]]
    session_id: Optional[str] = None


# ──────────────────────────────────────────────────────────
# 3. MCP TOOL DEFINITIONS (passed to Claude)
# ──────────────────────────────────────────────────────────

TASK_AGENT_TOOLS = [
    {
        "name": "create_task",
        "description": "Create a new task and store it in the database",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "description": {"type": "string"},
                "priority":    {"type": "string", "enum": ["high", "med", "low"]},
                "due_date":    {"type": "string", "description": "ISO date or natural language like 'Friday'"},
                "tags":        {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List tasks from the database with optional filters",
        "input_schema": {
            "type": "object",
            "properties": {
                "status":   {"type": "string", "enum": ["pending", "in_progress", "done", "all"]},
                "priority": {"type": "string", "enum": ["high", "med", "low", "all"]},
                "due_date": {"type": "string"},
            },
        },
    },
    {
        "name": "update_task",
        "description": "Update an existing task's fields",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id":  {"type": "string"},
                "title":    {"type": "string"},
                "status":   {"type": "string"},
                "priority": {"type": "string"},
                "due_date": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "delete_task",
        "description": "Delete a task by ID",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]

CALENDAR_AGENT_TOOLS = [
    {
        "name": "create_event",
        "description": "Create a new calendar event",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":      {"type": "string"},
                "start_time": {"type": "string", "description": "ISO datetime or natural language"},
                "end_time":   {"type": "string"},
                "location":   {"type": "string"},
                "attendees":  {"type": "array", "items": {"type": "string"}},
                "notes":      {"type": "string"},
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
    {
        "name": "get_schedule",
        "description": "Get calendar events within a date range",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string"},
                "to_date":   {"type": "string"},
            },
        },
    },
    {
        "name": "find_free_slots",
        "description": "Find free time slots in the calendar",
        "input_schema": {
            "type": "object",
            "properties": {
                "date":     {"type": "string"},
                "duration": {"type": "integer", "description": "duration in minutes"},
            },
            "required": ["date"],
        },
    },
]

NOTES_AGENT_TOOLS = [
    {
        "name": "save_note",
        "description": "Save a note to the knowledge base",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":   {"type": "string"},
                "content": {"type": "string"},
                "tags":    {"type": "array", "items": {"type": "string"}},
                "pinned":  {"type": "boolean"},
            },
            "required": ["title", "content"],
        },
    },
    {
        "name": "search_notes",
        "description": "Search notes by query string or tags",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "tags":  {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "summarize_notes",
        "description": "Get a summary of all notes or notes matching a filter",
        "input_schema": {
            "type": "object",
            "properties": {"filter": {"type": "string"}},
        },
    },
]


# ──────────────────────────────────────────────────────────
# 4. TOOL EXECUTORS (simulate MCP server responses via DB)
# ──────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.utcnow().isoformat()


def execute_task_tool(tool_name: str, tool_input: dict) -> dict:
    conn = get_db()
    cur = conn.cursor()
    result = {}

    if tool_name == "create_task":
        task_id = str(uuid.uuid4())[:8]
        cur.execute("""
            INSERT INTO tasks (id, title, description, priority, status, due_date, tags, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            task_id,
            tool_input["title"],
            tool_input.get("description", ""),
            tool_input.get("priority", "med"),
            "pending",
            tool_input.get("due_date", ""),
            json.dumps(tool_input.get("tags", [])),
            now_iso(), now_iso(),
        ))
        conn.commit()
        result = {"task_id": task_id, "status": "created", "title": tool_input["title"]}

    elif tool_name == "list_tasks":
        status = tool_input.get("status", "all")
        priority = tool_input.get("priority", "all")
        query = "SELECT * FROM tasks WHERE 1=1"
        params = []
        if status != "all":
            query += " AND status=?"; params.append(status)
        if priority != "all":
            query += " AND priority=?"; params.append(priority)
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        result = {"tasks": rows, "count": len(rows)}

    elif tool_name == "update_task":
        task_id = tool_input.pop("task_id")
        set_clauses = [f"{k}=?" for k in tool_input]
        if set_clauses:
            cur.execute(
                f"UPDATE tasks SET {', '.join(set_clauses)}, updated_at=? WHERE id=?",
                list(tool_input.values()) + [now_iso(), task_id]
            )
            conn.commit()
        result = {"task_id": task_id, "updated_fields": list(tool_input.keys())}

    elif tool_name == "delete_task":
        cur.execute("DELETE FROM tasks WHERE id=?", (tool_input["task_id"],))
        conn.commit()
        result = {"task_id": tool_input["task_id"], "status": "deleted"}

    conn.close()
    return result


def execute_calendar_tool(tool_name: str, tool_input: dict) -> dict:
    conn = get_db()
    cur = conn.cursor()
    result = {}

    if tool_name == "create_event":
        event_id = str(uuid.uuid4())[:8]
        cur.execute("""
            INSERT INTO events (id, title, start_time, end_time, location, attendees, notes, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            event_id,
            tool_input["title"],
            tool_input["start_time"],
            tool_input["end_time"],
            tool_input.get("location", ""),
            json.dumps(tool_input.get("attendees", [])),
            tool_input.get("notes", ""),
            now_iso(),
        ))
        conn.commit()
        result = {"event_id": event_id, "status": "created", "title": tool_input["title"]}

    elif tool_name == "get_schedule":
        cur.execute("SELECT * FROM events ORDER BY start_time")
        rows = [dict(r) for r in cur.fetchall()]
        result = {"events": rows, "count": len(rows)}

    elif tool_name == "find_free_slots":
        # Simplified: return mock free slots
        result = {
            "date": tool_input.get("date", "today"),
            "free_slots": ["9:00 AM", "11:00 AM", "2:00 PM", "4:00 PM"],
            "duration_min": tool_input.get("duration", 30),
        }

    conn.close()
    return result


def execute_notes_tool(tool_name: str, tool_input: dict) -> dict:
    conn = get_db()
    cur = conn.cursor()
    result = {}

    if tool_name == "save_note":
        note_id = str(uuid.uuid4())[:8]
        cur.execute("""
            INSERT INTO notes (id, title, content, tags, pinned, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            note_id,
            tool_input["title"],
            tool_input["content"],
            json.dumps(tool_input.get("tags", [])),
            1 if tool_input.get("pinned") else 0,
            now_iso(), now_iso(),
        ))
        conn.commit()
        result = {"note_id": note_id, "status": "saved", "title": tool_input["title"]}

    elif tool_name == "search_notes":
        query = tool_input.get("query", "")
        cur.execute(
            "SELECT * FROM notes WHERE content LIKE ? OR title LIKE ?",
            (f"%{query}%", f"%{query}%")
        )
        rows = [dict(r) for r in cur.fetchall()]
        result = {"notes": rows, "count": len(rows), "query": query}

    elif tool_name == "summarize_notes":
        cur.execute("SELECT title, content FROM notes LIMIT 10")
        rows = [dict(r) for r in cur.fetchall()]
        result = {"notes_count": len(rows), "titles": [r["title"] for r in rows]}

    conn.close()
    return result


def route_tool(agent: str, tool_name: str, tool_input: dict) -> dict:
    if agent == "task":
        return execute_task_tool(tool_name, tool_input)
    elif agent == "calendar":
        return execute_calendar_tool(tool_name, tool_input)
    elif agent == "notes":
        return execute_notes_tool(tool_name, tool_input)
    return {"error": "unknown agent"}


# ──────────────────────────────────────────────────────────
# 5. SUB-AGENT RUNNER
# ──────────────────────────────────────────────────────────

def run_sub_agent(
    agent_name: str,
    agent_system: str,
    tools: list,
    user_message: str,
    session_id: str,
) -> tuple[str, list]:
    """Run a sub-agent with its own tools and return (response_text, tool_calls_log)."""
    tool_calls_log = []
    messages = [{"role": "user", "content": user_message}]

    while True:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=agent_system,
            tools=tools,
            messages=messages,
        )

        # collect text
        text_blocks = [b.text for b in resp.content if b.type == "text"]
        text = " ".join(text_blocks)

        if resp.stop_reason == "end_turn":
            # Log to DB
            _log_agent(session_id, agent_name, None, None, text)
            return text, tool_calls_log

        if resp.stop_reason == "tool_use":
            # Execute all tool calls
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    t_result = route_tool(agent_name, block.name, dict(block.input))
                    tool_calls_log.append({
                        "agent": f"{agent_name}_agent",
                        "tool": block.name,
                        "input": block.input,
                        "result": t_result,
                    })
                    _log_agent(session_id, agent_name, block.name, block.input, t_result)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(t_result),
                    })

            # Append assistant + tool results and continue
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        break

    return text or "(no response)", tool_calls_log


def _log_agent(session_id, agent_name, tool_name, tool_input, tool_output):
    try:
        conn = get_db()
        conn.execute("""
            INSERT INTO agent_logs (session_id, agent_name, tool_name, tool_input, tool_output, created_at)
            VALUES (?,?,?,?,?,?)
        """, (session_id, agent_name, tool_name, json.dumps(tool_input), json.dumps(tool_output), now_iso()))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ──────────────────────────────────────────────────────────
# 6. PRIMARY ORCHESTRATOR AGENT
# ──────────────────────────────────────────────────────────

PRIMARY_SYSTEM = """You are Nexus, an AI orchestrator managing three specialized sub-agents:
- task_agent: handles task creation, listing, updating, deletion
- calendar_agent: handles event scheduling, finding free time, viewing schedule
- notes_agent: handles saving, searching, and summarizing notes

Your job:
1. Analyze the user's request
2. Determine which sub-agents are needed (can be multiple)
3. Return a JSON routing plan

Always respond with ONLY valid JSON in this format:
{
  "intent": "brief description of what the user wants",
  "agents_needed": ["task", "calendar", "notes"],  // subset of these three
  "agent_instructions": {
    "task": "specific instruction for task agent",
    "calendar": "specific instruction for calendar agent",
    "notes": "specific instruction for notes agent"
  },
  "is_query": false  // true if user is asking for info, false if creating/modifying
}
"""

TASK_SYSTEM = """You are a Task Management Agent. You help users create, list, update and delete tasks.
Always use your tools to actually perform the operation. Be concise and action-oriented.
When creating tasks, extract priority (high/med/low) and due dates from context.
After using a tool, confirm what was done."""

CALENDAR_SYSTEM = """You are a Calendar Management Agent. You help users schedule events and manage their time.
Always use your tools. When creating events, extract time information carefully.
Find free slots proactively when scheduling. Confirm event details after creation."""

NOTES_SYSTEM = """You are a Notes Management Agent. You help users capture and retrieve information.
Always use your tools to save or search notes. Extract key information from user messages.
Auto-generate relevant tags. Confirm saves with a brief summary."""


async def orchestrate(message: str, session_id: str) -> ChatResponse:
    start = datetime.utcnow()
    all_tool_calls = []
    agents_used = ["primary"]
    db_changes = {"tasks": 0, "events": 0, "notes": 0}

    # Step 1: Primary agent decides routing
    routing_resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        system=PRIMARY_SYSTEM,
        messages=[{"role": "user", "content": message}],
    )
    routing_text = routing_resp.content[0].text.strip()

    try:
        # strip possible markdown fences
        if routing_text.startswith("```"):
            routing_text = routing_text.split("```")[1]
            if routing_text.startswith("json"):
                routing_text = routing_text[4:]
        plan = json.loads(routing_text)
    except Exception:
        plan = {
            "agents_needed": ["task"],
            "agent_instructions": {"task": message},
            "intent": message,
        }

    agents_needed = plan.get("agents_needed", ["task"])
    instructions = plan.get("agent_instructions", {})

    # Step 2: Execute sub-agents (in parallel for multi-agent workflows)
    sub_agent_map = {
        "task":     (TASK_SYSTEM,     TASK_AGENT_TOOLS),
        "calendar": (CALENDAR_SYSTEM, CALENDAR_AGENT_TOOLS),
        "notes":    (NOTES_SYSTEM,    NOTES_AGENT_TOOLS),
    }

    sub_results = {}
    for agent_name in agents_needed:
        if agent_name not in sub_agent_map:
            continue
        agents_used.append(agent_name)
        sys_prompt, tools = sub_agent_map[agent_name]
        instruction = instructions.get(agent_name, message)

        text, tool_calls = run_sub_agent(agent_name, sys_prompt, tools, instruction, session_id)
        sub_results[agent_name] = text
        all_tool_calls.extend(tool_calls)

        # Count DB changes
        for tc in tool_calls:
            if "create" in tc["tool"] or "save" in tc["tool"]:
                key = "tasks" if agent_name=="task" else "events" if agent_name=="calendar" else "notes"
                db_changes[key] += 1

    # Step 3: Primary agent synthesizes final response
    synthesis_prompt = f"""Original request: {message}

Sub-agent results:
{json.dumps(sub_results, indent=2)}

Tool calls executed:
{json.dumps([{"agent": tc["agent"], "tool": tc["tool"], "result": tc["result"]} for tc in all_tool_calls], indent=2)}

Provide a clear, helpful summary response to the user. Mention what was done, what data was stored, and any next steps."""

    synthesis_resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system="You are Nexus, a friendly AI assistant. Synthesize the results from your sub-agents into a clear, helpful response.",
        messages=[{"role": "user", "content": synthesis_prompt}],
    )
    final_response = synthesis_resp.content[0].text

    # Save session history
    _save_session(session_id, message, final_response)

    duration = int((datetime.utcnow() - start).total_seconds() * 1000)

    return ChatResponse(
        response=final_response,
        session_id=session_id,
        agents_used=list(set(agents_used)),
        tool_calls=[{"agent": tc["agent"], "tool": tc["tool"], "result": tc["result"]} for tc in all_tool_calls],
        workflow_steps=["parse", "route", "execute", "store", "respond"],
        db_changes=db_changes,
        duration_ms=duration,
    )


def _save_session(session_id: str, user_msg: str, assistant_msg: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT history FROM sessions WHERE id=?", (session_id,))
    row = cur.fetchone()
    if row:
        history = json.loads(row["history"])
        history.append({"role": "user", "content": user_msg})
        history.append({"role": "assistant", "content": assistant_msg})
        cur.execute("UPDATE sessions SET history=?, updated_at=? WHERE id=?",
                    (json.dumps(history[-20:]), now_iso(), session_id))
    else:
        history = [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": assistant_msg},
        ]
        cur.execute("INSERT INTO sessions (id, history, created_at, updated_at) VALUES (?,?,?,?)",
                    (session_id, json.dumps(history), now_iso(), now_iso()))
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────
# 7. API ENDPOINTS
# ──────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Main orchestrator endpoint. Routes to sub-agents, executes tools, returns synthesis."""
    session_id = req.session_id or str(uuid.uuid4())
    return await orchestrate(req.message, session_id)


@app.get("/agents/status")
def agents_status():
    conn = get_db()
    log_count = conn.execute("SELECT COUNT(*) as n FROM agent_logs").fetchone()["n"]
    conn.close()
    return {
        "primary": {"status": "online", "model": "claude-sonnet-4-20250514"},
        "task_agent": {"status": "online", "mcp_server": "task-manager", "tools": 4},
        "calendar_agent": {"status": "online", "mcp_server": "calendar", "tools": 3},
        "notes_agent": {"status": "online", "mcp_server": "notes", "tools": 3},
        "total_logs": log_count,
    }


# Tasks CRUD
@app.get("/tasks")
def list_tasks(status: str = "all", priority: str = "all"):
    conn = get_db()
    q = "SELECT * FROM tasks WHERE 1=1"
    params = []
    if status != "all": q += " AND status=?"; params.append(status)
    if priority != "all": q += " AND priority=?"; params.append(priority)
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    conn.close()
    return {"tasks": rows, "count": len(rows)}


@app.post("/tasks", status_code=201)
def create_task(task: TaskCreate):
    result = execute_task_tool("create_task", task.model_dump())
    return result


@app.patch("/tasks/{task_id}")
def update_task(task_id: str, update: TaskUpdate):
    data = {k: v for k, v in update.model_dump().items() if v is not None}
    data["task_id"] = task_id
    return execute_task_tool("update_task", data)


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str):
    return execute_task_tool("delete_task", {"task_id": task_id})


# Events
@app.get("/events")
def list_events():
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY start_time").fetchall()]
    conn.close()
    return {"events": rows, "count": len(rows)}


@app.post("/events", status_code=201)
def create_event(event: EventCreate):
    return execute_calendar_tool("create_event", event.model_dump())


# Notes
@app.get("/notes")
def list_notes(query: str = ""):
    conn = get_db()
    if query:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM notes WHERE title LIKE ? OR content LIKE ?",
            (f"%{query}%", f"%{query}%")
        ).fetchall()]
    else:
        rows = [dict(r) for r in conn.execute("SELECT * FROM notes ORDER BY pinned DESC, created_at DESC").fetchall()]
    conn.close()
    return {"notes": rows, "count": len(rows)}


@app.post("/notes", status_code=201)
def create_note(note: NoteCreate):
    return execute_notes_tool("save_note", note.model_dump())


# Multi-step workflow
@app.post("/workflow")
async def run_workflow(req: WorkflowRequest):
    """Execute a predefined multi-step workflow."""
    session_id = req.session_id or str(uuid.uuid4())
    results = []
    for step in req.steps:
        message = step.get("message", "")
        if message:
            result = await orchestrate(message, session_id)
            results.append({
                "step": step.get("name", "step"),
                "response": result.response,
                "agents_used": result.agents_used,
                "tool_calls": result.tool_calls,
            })
    return {"session_id": session_id, "steps_completed": len(results), "results": results}


# DB stats
@app.get("/stats")
def stats():
    conn = get_db()
    return {
        "tasks":      conn.execute("SELECT COUNT(*) as n FROM tasks").fetchone()["n"],
        "events":     conn.execute("SELECT COUNT(*) as n FROM events").fetchone()["n"],
        "notes":      conn.execute("SELECT COUNT(*) as n FROM notes").fetchone()["n"],
        "agent_logs": conn.execute("SELECT COUNT(*) as n FROM agent_logs").fetchone()["n"],
        "sessions":   conn.execute("SELECT COUNT(*) as n FROM sessions").fetchone()["n"],
    }


@app.get("/")
def root():
    return {
        "name": "NEXUS Multi-Agent AI System",
        "version": "1.0.0",
        "agents": ["primary", "task_agent", "calendar_agent", "notes_agent"],
        "endpoints": ["/chat", "/tasks", "/events", "/notes", "/workflow", "/agents/status", "/stats"],
        "docs": "/docs",
    }


# ──────────────────────────────────────────────────────────
# 8. ENTRY POINT
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)