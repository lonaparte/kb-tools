"""Individual MCP tool implementations.

Each module exposes a pure function *_impl(kb_root, ...) that returns a
string. The MCP server (server.py) wraps these with FastMCP decorators
and tool descriptions (which are critical — they shape AI behavior).
"""
