"""Mapping between evidence files on disk and the URLs that serve them."""

from __future__ import annotations

from pathlib import Path

from app.config import settings

# Only these parts of the storage tree are served; the raw imagery cache is not.
SERVED_FOLDERS = ("outputs", "outbox", "uploads")


def public_path(path: Path) -> str:
    """URL for a file inside the storage tree, independent of where that tree is mounted."""

    relative = path.resolve().relative_to(settings.storage_dir.resolve())
    return "/files/" + relative.as_posix()


def resolve_public_path(url_path: str | None) -> Path | None:
    """The file behind a /files URL, or None if it is absent or outside the served tree.

    Records written before v3 stored paths with a leading ``storage/``.
    """

    if not url_path:
        return None
    relative = url_path.removeprefix("/files/").lstrip("/").removeprefix("storage/")
    root = settings.storage_dir.resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents or not candidate.is_file():
        return None
    if candidate.relative_to(root).parts[0] not in SERVED_FOLDERS:
        return None
    return candidate
