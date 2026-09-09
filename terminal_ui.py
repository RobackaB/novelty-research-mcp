"""A simple dependency-free terminal UI for the HTTP MCP launcher."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Iterable


TOOLS = [
    "research_session_start",
    "research_session_understand_query",
    "patent_evidence_to_session",
    "publication_evidence_to_session",
    "web_evidence_to_session",
    "research_session_checklist",
    "research_session_user_answer",
]
KEYS = [
    ("GOOGLE_CSE_API_KEY", "primárny web_search backend"),
    ("GOOGLE_CSE_ID", "ID vyhľadávacieho nástroja pre web_search"),
    ("TAVILY_API_KEY", "fallback pre web/patent vyhľadávanie"),
    ("EXA_API_KEY", "fallback pre web/patent vyhľadávanie"),
    ("SEMANTIC_SCHOLAR_API_KEY", "vyššie limity pre publikácie"),
    ("PUBMED_API_KEY", "vyššie limity pre PubMed"),
    ("ALPHAXIV_API_KEY", "doplnkové publikácie cez AlphaXiv MCP"),
]


def _enable_windows_ansi() -> None:
    """Enable ANSI colours in the Windows terminal where possible."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        return


def _color_enabled() -> bool:
    """Determine whether the banner should use ANSI colours."""
    if os.getenv("NO_COLOR") or os.getenv("MCP_PLAIN_UI"):
        return False
    return bool(sys.stdout.isatty() or os.name == "nt") and os.getenv("TERM", "") != "dumb"


def _c(text: str, code: str, enabled: bool) -> str:
    """Wrap text in an ANSI colour when colours are enabled."""
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _status(value: str | None, color: bool) -> str:
    """Return the textual configuration status of an environment key."""
    if value:
        return _c("configured", "32;1", color)
    return _c("missing", "33;1", color)


def _env_line(path: Path, color: bool) -> str:
    """Build the line reporting whether an env file exists."""
    marker = _c("found", "32", color) if path.exists() else _c("missing", "33", color)
    return f"{marker}  {path}"


def _wrap_rows(rows: Iterable[str], indent: str = "  ") -> str:
    """Indent several banner lines with the same prefix."""
    return "\n".join(f"{indent}{row}" for row in rows)


def startup_banner(host: str, port: int, path: str) -> str:
    """Return the startup banner without printing it, which is useful for tests."""
    _enable_windows_ansi()
    color = _color_enabled()
    project_dir = Path(__file__).resolve().parent
    workspace_env = project_dir.parent / "env.env"
    server_env = project_dir / ".env"
    local_url = f"http://127.0.0.1:{port}{path}"
    flowise_url = f"http://host.docker.internal:{port}{path}"
    title = _c("MCP Research Server", "36;1", color)
    line = _c("=" * 72, "36", color)
    key_rows = [f"{_status(os.getenv(name), color):<22} {name:<28} {purpose}" for name, purpose in KEYS]
    tool_rows = []
    for index in range(0, len(TOOLS), 2):
        left = TOOLS[index]
        right = TOOLS[index + 1] if index + 1 < len(TOOLS) else ""
        tool_rows.append(f"{left:<28} {right}")
    return "\n".join([
        "",
        line,
        f"  {title}",
        line,
        f"  Režim       : Streamable HTTP MCP",
        f"  Bind        : {host}:{port}{path}",
        f"  Flowise URL : {_c(flowise_url, '32;1', color)}",
        f"  Lokálna URL : {_c(local_url, '32', color)}",
        f"  Python      : {platform.python_version()}",
        f"  Projekt     : {project_dir}",
        "",
        "  Súbory prostredia",
        _wrap_rows([_env_line(server_env, color), _env_line(workspace_env, color)], "    "),
        "",
        "  API kľúče",
        _wrap_rows(key_rows, "    "),
        "",
        "  Nástroje",
        _wrap_rows(tool_rows, "    "),
        "",
        f"  Flowise Custom MCP config: {{\"url\": \"{flowise_url}\", \"headers\": {{}}}}",
        f"  Server zastavíš cez: {_c('Ctrl+C', '31;1', color)}",
        line,
        "",
    ])


def print_startup_banner(host: str, port: int, path: str) -> None:
    """Print the HTTP launcher banner unless it is disabled by an environment variable."""
    if os.getenv("MCP_NO_UI"):
        return
    print(startup_banner(host, port, path), flush=True)
