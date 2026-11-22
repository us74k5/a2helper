"""Downloading the cube parser.

The hidden-cube packet decoding is the Aion2CubeGPS project's work. Its license has not been
established, so this program does not ship it: it downloads the one file it needs, onto the
player's own computer, from the project's GitHub repository.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

from .capture.core import PARSER_FILE

UPSTREAM_REPO = "https://github.com/fokeiro2020/Aion2CubeGPS"
PARSER_URL = os.environ.get(
    "AION2_HELPER_PARSER_URL",
    "https://raw.githubusercontent.com/fokeiro2020/Aion2CubeGPS/master/" + PARSER_FILE)
MARKERS = (b"class PureStreamEngine", b"class RadarState")


def parser_path(parser_dir: Path) -> Path:
    return Path(parser_dir) / PARSER_FILE


def have_parser(parser_dir: Path) -> bool:
    return parser_path(parser_dir).is_file()


def fetch_parser(parser_dir: Path, url: str = PARSER_URL, timeout: float = 30.0) -> str:
    """Download (or update) the parser. Returns a sentence saying what happened."""
    parser_dir = Path(parser_dir)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = r.read(5_000_000)
    if not all(m in body for m in MARKERS):
        raise RuntimeError("the downloaded file does not look like the cube parser; nothing was changed")
    parser_dir.mkdir(parents=True, exist_ok=True)
    dest = parser_path(parser_dir)
    old = dest.read_bytes() if dest.exists() else None
    if old == body:
        return "the cube parser is already up to date"
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(body)
    tmp.replace(dest)
    return "cube parser updated" if old is not None else "cube parser downloaded"
