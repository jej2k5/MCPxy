# mcpxy-client

Lightweight CLI to register [MCPxy](https://github.com/jej2k5/mcpxy) as an MCP server in AI clients (Claude Desktop, Claude Code, ChatGPT, etc.) — without installing the full proxy server stack.

## Installation

```bash
pip install mcpxy-client
```

Only depends on `httpx` (~2 MB). No FastAPI, SQLAlchemy, or other server dependencies.

## Usage

### Register MCPxy with a client

```bash
# Claude Desktop (stdio adapter)
mcpxy-client install --client claude-desktop --url http://127.0.0.1:8000

# Claude Code (HTTP transport)
mcpxy-client install --client claude-code --url http://127.0.0.1:8000

# ChatGPT (prints config snippet to paste manually)
mcpxy-client install --client chatgpt --url http://127.0.0.1:8000
```

Point `--url` at your MCPxy proxy, whether it's running locally, in Docker, or on a remote server.

### Preview changes without writing

```bash
mcpxy-client install --client claude-desktop --url http://proxy.example.com:8000 --dry-run
```

### List supported clients

```bash
mcpxy-client list-clients
```

### Run the stdio adapter directly

```bash
mcpxy-client stdio --connect http://127.0.0.1:8000
```

This is used internally by Claude Desktop. You normally don't need to run it manually — the `install` command configures it automatically.

## Full server

If you also need the MCPxy proxy server itself, install the full package:

```bash
pip install mcpxy-proxy
```
