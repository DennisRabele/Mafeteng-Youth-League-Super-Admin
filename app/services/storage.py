from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import settings


_CLOUDINARY_FOLDERS = {
    "admin-photos": "admin-photos",
    "team-logos": "team-logos",
    "player-documents": "player-documents",
    "player-photos": "player-photos",
    "player-agreements": "player-agreements",
    "match-results": "match-results",
}
_CLOUDINARY_PRESETS = {
    "admin-photos": "admin_photos",
    "team-logos": "team_logos",
    "player-documents": "player_documents",
    "player-photos": "player_photos",
}
_IMAGE_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".avi"}
_DOCUMENT_UPLOAD_EXTENSIONS = {".doc", ".docx", ".pdf", ".txt"}
_IMAGE_UPLOAD_MAX_BYTES = 3 * 1024 * 1024
_DOCUMENT_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
logger = logging.getLogger(__name__)


def _cloudinary_ready() -> bool:
    return bool(
        settings.cloudinary_cloud_name
        and settings.cloudinary_api_key
        and settings.cloudinary_api_secret
    )


def _get_cloudinary_uploader():
    if not _cloudinary_ready():
        return None

    try:
        import cloudinary
        from cloudinary import uploader
    except ImportError:
        return None

    cloudinary.config(
        cloud_name=settings.cloudinary_cloud_name,
        api_key=settings.cloudinary_api_key,
        api_secret=settings.cloudinary_api_secret,
        secure=True,
    )
    return uploader


def _cloudinary_folder(folder: str) -> str:
    cloudinary_folder = _CLOUDINARY_FOLDERS.get(folder)
    if not cloudinary_folder:
        raise ValueError(f"Unsupported upload folder: {folder}")
    prefix = settings.cloudinary_folder_prefix
    return f"{prefix}/{cloudinary_folder}" if prefix else cloudinary_folder


def _cloudinary_preset(folder: str) -> str | None:
    return _CLOUDINARY_PRESETS.get(folder)


def _content_type(upload: UploadFile) -> str:
    if upload.content_type:
        return upload.content_type
    guessed_type, _ = mimetypes.guess_type(upload.filename or "")
    return guessed_type or "application/octet-stream"


def _upload_limit(upload: UploadFile) -> tuple[int, str]:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix in _IMAGE_UPLOAD_EXTENSIONS:
        return _IMAGE_UPLOAD_MAX_BYTES, "Images"
    if suffix in _DOCUMENT_UPLOAD_EXTENSIONS:
        return _DOCUMENT_UPLOAD_MAX_BYTES, "Documents"

    guessed_type = _content_type(upload)
    if guessed_type.startswith("image/") or guessed_type in {"video/x-msvideo", "video/avi"}:
        return _IMAGE_UPLOAD_MAX_BYTES, "Images"
    if guessed_type in {"application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/pdf", "text/plain"}:
        return _DOCUMENT_UPLOAD_MAX_BYTES, "Documents"

    raise ValueError(
        "Unsupported file type. Allowed image files are JPG, JPEG, PNG, and AVI. "
        "Allowed document files are DOC, DOCX, PDF, and TXT."
    )


def _upload_size_bytes(upload: UploadFile) -> int:
    current_position = upload.file.tell()
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(current_position)
    return size


def _validate_upload(upload: UploadFile) -> None:
    max_bytes, label = _upload_limit(upload)
    if _upload_size_bytes(upload) > max_bytes:
        max_mb = max_bytes // (1024 * 1024)
        raise ValueError(f"{label} must be less than {max_mb}MB.")


def _save_to_cloudinary(upload: UploadFile, folder: str) -> str:
    uploader = _get_cloudinary_uploader()
    if uploader is None:
        raise RuntimeError(
            "Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET to use Cloudinary."
        )

    upload.file.seek(0)
    cloudinary_preset = _cloudinary_preset(folder)
    base_kwargs = {
        "public_id": uuid4().hex,
        "overwrite": True,
        "unique_filename": False,
        "use_filename": False,
        "resource_type": "auto",
        "filename_override": upload.filename or uuid4().hex,
        "folder": _cloudinary_folder(folder),
    }
    try:
        if cloudinary_preset:
            result = uploader.upload(upload.file, upload_preset=cloudinary_preset, **base_kwargs)
        else:
            result = uploader.upload(upload.file, **base_kwargs)
    except Exception as exc:
        logger.warning(
            "Cloudinary preset upload failed for folder=%s; falling back to folder upload: %s",
            folder,
            exc,
        )
        upload.file.seek(0)
        result = uploader.upload(upload.file, **base_kwargs)
    upload.file.seek(0)
    return result["secure_url"]


def _parse_cloudinary_public_id(path: str) -> tuple[str, str] | None:
    parsed = urlparse(path)
    if parsed.netloc != "res.cloudinary.com":
        return None

    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        upload_index = parts.index("upload")
    except ValueError:
        return None
    if upload_index == 0:
        return None

    resource_type = parts[upload_index - 1]
    remaining = parts[upload_index + 1 :]
    if not remaining:
        return None
    if remaining[0].startswith("v") and remaining[0][1:].isdigit():
        remaining = remaining[1:]
    if not remaining:
        return None

    file_name = Path(remaining[-1]).stem
    public_id_parts = remaining[:-1] + [file_name]
    public_id = "/".join(part for part in public_id_parts if part)
    if not public_id:
        return None
    return resource_type, public_id


def _delete_from_cloudinary(path: str) -> bool:
    uploader = _get_cloudinary_uploader()
    if uploader is None:
        return False

    parsed = _parse_cloudinary_public_id(path)
    if not parsed:
        return False

    resource_type, public_id = parsed
    uploader.destroy(public_id, resource_type=resource_type, invalidate=True)
    return True


def delete_upload(path: str | None, folder: str | None = None) -> bool:
    if not path:
        return False
    normalized = path.strip()
    if not normalized:
        return False
    if normalized.startswith("http://") or normalized.startswith("https://"):
        if "res.cloudinary.com" in normalized:
            return _delete_from_cloudinary(normalized)
    return False


def save_upload(upload: UploadFile | None, folder: str) -> str | None:
    if not upload or not upload.filename:
        return None
    _validate_upload(upload)
    return _save_to_cloudinary(upload, folder)
