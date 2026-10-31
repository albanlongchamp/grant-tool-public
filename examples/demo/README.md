# Fictional MCP demo

Install the MCP extra and run `python3 scripts/demo.py` from the repository root.
A scripted MCP client answers the minimal first-use question with invented
applications and an invented RFP, indexes only the demo folder, gathers evidence,
saves a prewritten draft, reads the review queue and exports Markdown.

No model, network service, user archive or browser is involved. Rerunning reuses
its saved proposal. This is a protocol demonstration, not a scientific evaluation.
Generated state stays under `.local/demo-mcp/` and is excluded from Git.

To continue with a frontier-model MCP client, give its stdio server configuration
the absolute Python interpreter path, `-m grantlib.mcp_server`, and
`--config /absolute/path/to/checkout/.local/demo-mcp/config.local.json`.
