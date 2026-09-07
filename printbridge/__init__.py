"""printbridge — a self-hosted MCP print server.

Assistants call `print_text`/`print_url`/`print_file`; printbridge renders the document to PDF
and hands it to a CUPS queue that reaches the printer over a private route. Nothing about the
document leaves the operator's own infrastructure.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
