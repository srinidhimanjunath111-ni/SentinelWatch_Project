"""Safe local evidence storage for uploaded and generated images."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from app.config import settings

ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


def _safe_filename(name: str) -> str:
    name = Path(name or "image.png").name
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-") or "image"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported image type. Use one of: {', '.join(sorted(ALLOWED_SUFFIXES))}",
        )
    return f"{stem}-{uuid.uuid4().hex[:10]}{suffix}"


async def save_upload(upload: UploadFile, case_id: str, role: str) -> Path:
    settings.ensure_directories()
    filename = _safe_filename(upload.filename or "image.png")
    destination = settings.upload_dir / f"{case_id}-{role}-{filename}"
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    with destination.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                handle.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Image exceeds {settings.max_upload_mb} MB limit.",
                )
            handle.write(chunk)
    return destination


def public_path(path: Path) -> str:
    return "/files/" + path.as_posix().replace("\\", "/")
