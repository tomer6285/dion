"""Shared terminal output with a fallback for limited console encodings."""
from __future__ import annotations

import sys
from typing import TextIO

from rich.console import Console


def configure_output(stream: TextIO) -> None:
    """Keep the destination encoding, replacing glyphs it cannot represent.

    Windows pipes and legacy consoles may use a code page such as cp1252.
    Emoji and Rich spinners must not turn a successful operation into an error.
    Capture streams such as StringIO need no configuration.
    """
    encoding = getattr(stream, "encoding", None)
    reconfigure = getattr(stream, "reconfigure", None)
    if not encoding or reconfigure is None:
        return
    try:
        "🍇⠋".encode(encoding)
    except UnicodeEncodeError:
        reconfigure(errors="replace")


configure_output(sys.stdout)
configure_output(sys.stderr)
console = Console()
