"""Streamable HTTP vstupný bod pre Flowise hostovaný v Dockeri."""

from __future__ import annotations

import logging
import os

from mcp.server.transport_security import TransportSecuritySettings

from server import mcp
from terminal_ui import print_startup_banner

def _csv_env(name: str, default: str) -> list[str]:
    """Načíta premennú prostredia ako zoznam hodnôt oddelených čiarkou."""
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]

def _silence_http_clients() -> None:
    """Stlmí podrobné HTTP logovanie, aby sa do výstupu nedostali citlivé URL adresy."""
    for name in ("httpx", "httpcore", "hpack", "h11"):
        logging.getLogger(name).setLevel(logging.WARNING)

def main() -> None:
    """Spustí MCP server cez Streamable HTTP transport."""
    _silence_http_clients()
    mcp.settings.host = os.getenv("MCP_HOST", "0.0.0.0")
    mcp.settings.port = int(os.getenv("MCP_PORT", "8000"))
    mcp.settings.streamable_http_path = os.getenv("MCP_PATH", "/mcp")
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_csv_env(
            "MCP_ALLOWED_HOSTS",
            "127.0.0.1:*,localhost:*,host.docker.internal:*,mcp-research-server:*",
        ),
        allowed_origins=_csv_env(
            "MCP_ALLOWED_ORIGINS",
            "http://127.0.0.1:*,http://localhost:*,http://host.docker.internal:*,http://mcp-research-server:*",
        ),
    )
    print_startup_banner(
        host=mcp.settings.host,
        port=mcp.settings.port,
        path=mcp.settings.streamable_http_path,
    )
    mcp.run(transport="streamable-http")

if __name__ == "__main__":
    main()