"""Jednoduché terminálové UI bez externých závislostí pre HTTP MCP launcher."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Iterable


TOOLS = [
    "patent_evidence_pack",
    "publication_evidence_pack",
    "web_evidence_pack",
    "merge_evidence_pack",
]
KEYS = [
    ("GOOGLE_CSE_API_KEY", "primárny web_search backend"),
    ("GOOGLE_CSE_ID", "ID vyhľadávacieho nástroja pre web_search"),
    ("TAVILY_API_KEY", "fallback pre web/patent vyhľadávanie"),
    ("EXA_API_KEY", "fallback pre web/patent vyhľadávanie"),
    ("SEMANTIC_SCHOLAR_API_KEY", "vyššie limity pre publikácie"),
]


def _enable_windows_ansi() -> None:
    """Povolí ANSI farby vo Windows termináli, ak je to možné."""
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
    """Zistí, či má banner používať ANSI farby."""
    if os.getenv("NO_COLOR") or os.getenv("MCP_PLAIN_UI"):
        return False
    return bool(sys.stdout.isatty() or os.name == "nt") and os.getenv("TERM", "") != "dumb"


def _c(text: str, code: str, enabled: bool) -> str:
    """Obalí text ANSI farbou, ak sú farby povolené."""
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _status(value: str | None, color: bool) -> str:
    """Vráti textový stav konfigurácie premenného kľúča."""
    if value:
        return _c("configured", "32;1", color)
    return _c("missing", "33;1", color)


def _env_line(path: Path, color: bool) -> str:
    """Vytvorí riadok s informáciou, či env súbor existuje."""
    marker = _c("found", "32", color) if path.exists() else _c("missing", "33", color)
    return f"{marker}  {path}"


def _wrap_rows(rows: Iterable[str], indent: str = "  ") -> str:
    """Odsadí viacero riadkov banneru rovnakým prefixom."""
    return "\n".join(f"{indent}{row}" for row in rows)


def startup_banner(host: str, port: int, path: str) -> str:
    """Vráti startup banner bez priameho vytlačenia, čo je užitočné pre testy."""
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
    """Vytlačí HTTP launcher banner, ak nie je vypnutý cez env premennú."""
    if os.getenv("MCP_NO_UI"):
        return
    print(startup_banner(host, port, path), flush=True)
