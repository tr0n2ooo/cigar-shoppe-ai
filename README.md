# XLSX Analyst Agent

A CrewAI-powered agent that answers natural-language questions about any Excel
file on your local filesystem. Ships with:

- **Gradio chatbot UI** — browser-based interface for interactive Q&A
- **MCP server** — exposes the agent as a tool for other agents in a multi-agent system
- **Custom `xlsx_reader` tool** — lightweight openpyxl-based tool (no external dependencies)

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│               Multi-Agent System                     │
│                                                      │
│   Other Agent  ──MCP stdio/SSE──▶  MCP Server       │
│                                    (mcp_server/)     │
└────────────────────────────────────┬────────────────┘
                                     │
                              ┌──────▼──────┐
          Gradio UI ─────────▶│ CrewAI Agent│
          (ui.py)             │  (agent.py) │
                              └──────┬──────┘
                                     │ XlsxReaderTool
                              ┌──────▼──────┐
                              │  XLSX File  │
                              │ (local fs)  │
                              └─────────────┘
```

---

## Quickstart

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set your Anthropic API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

---

## Usage

### Run the Gradio chat UI

```bash
python ui.py --file /path/to/your/data.xlsx
```

Open [http://127.0.0.1:7860](http://127.0.0.1:7860) in your browser.

Optional flags:
```bash
python ui.py --file data.xlsx --port 8080 --host 0.0.0.0 --share
```

| Flag | Default | Description |
|------|---------|-------------|
| `--file` | required | Path to the `.xlsx` file |
| `--port` | `7860` | Port to serve Gradio on |
| `--host` | `127.0.0.1` | Bind address |
| `--share` | off | Create a public Gradio share link |

---

### Run the MCP server (for multi-agent integration)

**Stdio transport** (works with Claude Desktop, CrewAI, LangChain, etc.):
```bash
python mcp_server/server.py --file /path/to/data.xlsx
```

**SSE/HTTP transport** (for remote agents or orchestrators):
```bash
python mcp_server/server.py --file /path/to/data.xlsx --transport sse --port 8000
```

The MCP server exposes two tools:

| Tool | Description |
|------|-------------|
| `query_xlsx` | Full natural-language Q&A via CrewAI agent |
| `describe_xlsx` | Fast structural overview (sheets, headers, row counts) — no LLM call |

---

### Use from another CrewAI agent

```python
from crewai.mcp import MCPServerStdio

xlsx_mcp = MCPServerStdio(
    command="python",
    args=["mcp_server/server.py", "--file", "/path/to/data.xlsx"],
    env={"ANTHROPIC_API_KEY": "sk-ant-..."},
)

# Use inside a CrewAI agent
from crewai import Agent
agent = Agent(
    role="Analyst",
    goal="Answer questions about sales data",
    mcp_servers=[xlsx_mcp],
    ...
)
```

### Connect via Claude Desktop

Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "xlsx-analyst": {
      "command": "python",
      "args": [
        "/absolute/path/to/xlsx_agent/mcp_server/server.py",
        "--file",
        "/absolute/path/to/your/data.xlsx"
      ],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-..."
      }
    }
  }
}
```

---

## Project structure

```
xlsx_agent/
├── agent.py              # CrewAI agent + run_query() entry point
├── ui.py                 # Gradio chatbot web UI
├── requirements.txt
├── README.md
├── tools/
│   ├── __init__.py
│   └── xlsx_tool.py      # XlsxReaderTool (openpyxl-based CrewAI tool)
└── mcp_server/
    ├── __init__.py
    └── server.py         # FastMCP server exposing the agent as MCP tools
```

---

## Example questions

- *"What sheets does this file contain?"*
- *"Describe the structure of the Sales sheet."*
- *"How many rows of data are there?"*
- *"What are the top 5 items by quantity sold?"*
- *"Show me all transactions where the amount is over 1000."*
- *"What is the total revenue by category?"*
- *"Are there any duplicate entries?"*

---

## Extending for multi-agent systems

The MCP server is the integration point. To add this agent to a larger system:

1. **As a subprocess tool** — use `MCPServerStdio` (shown above)
2. **As a remote service** — run with `--transport sse` and connect via `MCPServerSSE`  
3. **As a CrewAI task** — import `run_query` directly and wrap it in a `Task`

The `describe_xlsx` tool is intentionally lightweight (no LLM call) so orchestrators
can do rapid structural discovery before routing richer questions to `query_xlsx`.
