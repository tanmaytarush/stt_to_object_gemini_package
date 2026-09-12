"""OCR-module settings: image path(s) plus the shared screen context."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from ..common.config import BaseSettings, add_shared_arguments, resolve_shared

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heic",
}


@dataclass
class Settings(BaseSettings):
    image_paths: list[Path] = field(default_factory=list)


def mime_for(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        raise SystemExit(
            f"unsupported image type {suffix or path.name!r} — "
            f"use {', '.join(sorted(MIME_BY_SUFFIX))}"
        )
    return mime


def read_image(path: Path) -> tuple[bytes, str]:
    mime = mime_for(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"could not read {path}: {exc}") from exc
    if not data:
        raise SystemExit(f"{path} is empty")
    return data, mime


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m voice_logging ocr",
        description=(
            "Read the Items section of a New Order from an image: item, "
            "quantity, unit. The dealer, the client and the order type come "
            "from the screen — the image never sets them."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_shared_arguments(p)
    p.add_argument(
        "--image", "-i", action="append", dest="images", metavar="PATH",
        help="Item-list image. Repeat for extra pages; rows are concatenated. "
             "Required.",
    )
    return p


def resolve(args: argparse.Namespace) -> Settings:
    shared = resolve_shared(args)
    raw = args.images or []
    if not raw:
        raise SystemExit("pass at least one --image PATH")

    paths: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if not path.is_file():
            raise SystemExit(f"image not found: {path}")
        mime_for(path)  # fail early on a bad suffix
        paths.append(path)

    return Settings(**shared, image_paths=paths)
