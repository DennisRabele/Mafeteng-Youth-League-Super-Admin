from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import UploadFile

from app.core.config import BASE_DIR, settings


_SUPABASE_BUCKETS = {
    "admin-photos": settings.supabase_admin_photos_bucket,
    "team-logos": settings.supabase_team_logos_bucket,
    "player-documents": settings.supabase_player_documents_bucket,
    "player-photos": settings.supabase_player_photos_bucket,
    "player-agreements": settings.supabase_player_agreements_bucket,
}
_supabase_client = None


def _local_upload_root() -> Path:
    upload_root = settings.upload_dir
    if not upload_root.is_absolute():
        upload_root = BASE_DIR / upload_root
    return upload_root


def _save_locally(upload: UploadFile, folder: str) -> str:
    upload_root = _local_upload_root()
    destination_dir = upload_root / folder
    destination_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(upload.filename).suffix.lower()
    filename = f"{uuid4().hex}{suffix}"
    destination = destination_dir / filename

    upload.file.seek(0)
    with destination.open("wb") as out_file:
        while chunk := upload.file.read(1024 * 1024):
            out_file.write(chunk)

    return f"/uploads/{folder}/{filename}"


def _supabase_configured() -> bool:
    return bool(settings.supabase_url or settings.supabase_service_role_key)


def _supabase_ready() -> bool:
    return bool(settings.supabase_url and settings.supabase_service_role_key)


def _get_supabase_client():
    global _supabase_client
    if not _supabase_ready():
        return None
    if _supabase_client is not None:
        return _supabase_client

    try:
        from supabase import create_client
    except ImportError:
        return None

    _supabase_client = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    return _supabase_client


def _bucket_for_folder(folder: str) -> str:
    bucket = _SUPABASE_BUCKETS.get(folder)
    if not bucket:
        raise ValueError(f"Unsupported upload folder: {folder}")
    return bucket


def _content_type(upload: UploadFile) -> str:
    if upload.content_type:
        return upload.content_type
    guessed_type, _ = mimetypes.guess_type(upload.filename or "")
    return guessed_type or "application/octet-stream"


def _supabase_public_url(bucket: str, object_name: str) -> str:
    base_url = settings.supabase_url.rstrip("/")
    bucket_path = quote(bucket, safe="")
    object_path = quote(object_name, safe="")
    return f"{base_url}/storage/v1/object/public/{bucket_path}/{object_path}"


def _save_to_supabase(upload: UploadFile, folder: str) -> str:
    client = _get_supabase_client()
    if client is None:
        raise RuntimeError(
            "Supabase Storage is configured, but the supabase client package is unavailable."
        )

    bucket = _bucket_for_folder(folder)
    suffix = Path(upload.filename).suffix.lower()
    object_name = f"{uuid4().hex}{suffix}"

    upload.file.seek(0)
    payload = upload.file.read()
    upload.file.seek(0)

    client.storage.from_(bucket).upload(
        path=object_name,
        file=payload,
        file_options={
            "cache-control": "3600",
            "content-type": _content_type(upload),
            "upsert": "false",
        },
    )
    return _supabase_public_url(bucket, object_name)


def save_upload(upload: UploadFile | None, folder: str) -> str | None:
    if not upload or not upload.filename:
        return None

    if _supabase_configured():
        if not _supabase_ready():
            raise RuntimeError(
                "Set both SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to use Supabase Storage."
            )
        return _save_to_supabase(upload, folder)

    return _save_locally(upload, folder)
