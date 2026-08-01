from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
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
_CLOUDINARY_FOLDERS = {
    "admin-photos": "admin-photos",
    "team-logos": "team-logos",
    "player-documents": "player-documents",
    "player-photos": "player-photos",
    "player-agreements": "player-agreements",
}
_CLOUDINARY_PRESETS = {
    "admin-photos": "admin_photos",
    "team-logos": "team_logos",
    "player-documents": "player_documents",
    "player-photos": "player_photos",
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


def _cloudinary_configured() -> bool:
    return bool(
        settings.cloudinary_cloud_name
        or settings.cloudinary_api_key
        or settings.cloudinary_api_secret
    )


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


def _save_to_cloudinary(upload: UploadFile, folder: str) -> str:
    uploader = _get_cloudinary_uploader()
    if uploader is None:
        raise RuntimeError(
            "Cloudinary is configured, but the cloudinary package is unavailable."
        )

    upload.file.seek(0)
    cloudinary_preset = _cloudinary_preset(folder)
    upload_kwargs = {
        "public_id": uuid4().hex,
        "overwrite": True,
        "unique_filename": False,
        "use_filename": False,
        "resource_type": "auto",
        "filename_override": upload.filename or uuid4().hex,
    }
    if cloudinary_preset:
        upload_kwargs["upload_preset"] = cloudinary_preset
    else:
        upload_kwargs["folder"] = _cloudinary_folder(folder)
    result = uploader.upload(
        upload.file,
        **upload_kwargs,
    )
    upload.file.seek(0)
    return result["secure_url"]


def _delete_from_supabase(path: str) -> bool:
    client = _get_supabase_client()
    if client is None:
        return False

    parsed = urlparse(path)
    if not parsed.netloc:
        return False

    parts = [part for part in parsed.path.split("/") if part]
    try:
        bucket_index = parts.index("public") + 1
    except ValueError:
        return False
    if bucket_index >= len(parts):
        return False
    bucket = unquote(parts[bucket_index])
    object_name = "/".join(unquote(part) for part in parts[bucket_index + 1 :])
    if not bucket or not object_name:
        return False

    client.storage.from_(bucket).remove([object_name])
    return True


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


def _delete_local_upload(path: str) -> bool:
    root = _local_upload_root()
    candidates: list[Path] = []
    if path.startswith("/uploads/"):
        candidates.append(root / path.removeprefix("/uploads/"))
    else:
        candidates.append(Path(path))

    deleted = False
    for candidate in candidates:
        try:
            if candidate.is_file():
                candidate.unlink()
                deleted = True
        except Exception:
            continue
    return deleted


def delete_upload(path: str | None, folder: str | None = None) -> bool:
    if not path:
        return False
    normalized = path.strip()
    if not normalized:
        return False
    if normalized.startswith("http://") or normalized.startswith("https://"):
        if _cloudinary_ready() and "res.cloudinary.com" in normalized:
            return _delete_from_cloudinary(normalized)
        if _supabase_ready():
            return _delete_from_supabase(normalized)
        return False
    if normalized.startswith("/uploads/") or normalized.startswith(str(_local_upload_root())):
        return _delete_local_upload(normalized)
    if folder:
        return _delete_local_upload(f"/uploads/{folder}/{normalized.lstrip('/')}")
    return _delete_local_upload(normalized)


def save_upload(upload: UploadFile | None, folder: str) -> str | None:
    if not upload or not upload.filename:
        return None

    if _cloudinary_configured():
        if not _cloudinary_ready():
            raise RuntimeError(
                "Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET to use Cloudinary."
            )
        return _save_to_cloudinary(upload, folder)

    if _supabase_configured():
        if not _supabase_ready():
            raise RuntimeError(
                "Set both SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to use Supabase Storage."
            )
        return _save_to_supabase(upload, folder)

    return _save_locally(upload, folder)
