🎯 Hackathon Track: Multi-Agent Productivity Assistant

📌 Problem Statement


Build a multi-agent AI system that helps users manage:

Tasks
Schedules
Information

by interacting with multiple tools and data sources.

✅ How This Project Meets Requirements

✔️ 1. Primary Agent + Sub-Agents

🧠 Primary Agent (Orchestrator)
Understands user intent
Routes requests intelligently
Combines outputs into a final response
🤖 Sub-Agents
📋 Task Agent → task management
📅 Calendar Agent → scheduling
📝 Notes Agent → knowledge storage

✔️ 2. Structured Data Storage

Uses SQLite database to store:
Tasks
Events
Notes
Agent logs
Supports:
Create / Read / Update / Delete operations

✔️ 3. Multi-Tool Integration (MCP Concept)

Each agent uses dedicated tools:

Task tools → create_task, update_task, list_tasks
Calendar tools → create_event, get_schedule, find_free_slots
Notes tools → save_note, search_notes, summarize_notes

This simulates MCP-based tool integration across agents.

✔️ 4. Multi-Step Workflow Execution

The system follows a structured pipeline:

PARSE → ROUTE → EXECUTE → STORE → RESPOND

Example:

“Plan my week with deadlines and meetings”

Task agent → creates tasks
Calendar agent → schedules events
Notes agent → stores context
Orchestrator → combines everything

✔️ 5. API-Based System

Built using FastAPI
Supports:
/chat endpoint for AI interaction
Enables easy integration with external apps

🎯 Goal Achievement

This system demonstrates:

✅ Coordination between multiple AI agents
✅ Tool-based execution
✅ Persistent data handling
✅ Real-world workflow automation


