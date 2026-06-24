from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any
from uuid import uuid4


_IMAGE_EXTENSIONS_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def safe_images(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    images = [dict(item) for item in value if isinstance(item, dict)]
    return images or None


def materialize_image_attachments(
    cwd: str,
    prompt: str,
    images: list[dict[str, Any]] | None,
) -> tuple[str, list[dict[str, str]]]:
    if not images:
        return prompt, []
    cwd_path = Path(cwd).expanduser()
    if not cwd_path.is_dir():
        return prompt, []
    attachment_dir = cwd_path / "runtime" / "native-attachments" / "claude"
    attachment_dir.mkdir(parents=True, exist_ok=True)

    materialized: list[dict[str, str]] = []
    for image in images:
        data_url = str(image.get("url") or image.get("data_url") or "")
        payload = _decode_image_data_url(data_url)
        if payload is None:
            continue
        mime_type = _image_mime_type(image, data_url)
        filename = _safe_image_filename(str(image.get("filename") or ""), mime_type)
        path = attachment_dir / f"{uuid4().hex}-{filename}"
        path.write_bytes(payload)
        relative_path = path.relative_to(cwd_path).as_posix()
        materialized.append(
            {
                "path": str(path),
                "relative_path": relative_path,
                "filename": filename,
                "mime_type": mime_type,
            }
        )

    if not materialized:
        return prompt, []
    attachment_lines = ["Attached images:"]
    for image in materialized:
        attachment_lines.append(
            f"- {image['filename']}: {image['relative_path']} ({image['mime_type']})"
        )
    attachment_lines.append("Please inspect these local image files when answering.")
    if prompt.strip():
        return prompt.rstrip() + "\n\n" + "\n".join(attachment_lines), materialized
    return "\n".join(attachment_lines), materialized


def _decode_image_data_url(data_url: str) -> bytes | None:
    if not data_url.startswith("data:image/") or "," not in data_url:
        return None
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _image_mime_type(image: dict[str, Any], data_url: str) -> str:
    explicit = str(image.get("mime_type") or image.get("mimeType") or "")
    if explicit.startswith("image/"):
        return explicit
    header = data_url.split(",", 1)[0]
    return header.removeprefix("data:").split(";", 1)[0] or "image/png"


def _safe_image_filename(filename: str, mime_type: str) -> str:
    extension = _IMAGE_EXTENSIONS_BY_MIME.get(mime_type, ".img")
    stem = Path(filename).stem if filename else "image"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in stem)
    safe = safe.strip("-_") or "image"
    if not safe.lower().endswith(extension.lower()):
        return f"{safe[:80]}{extension}"
    return safe[:80]
