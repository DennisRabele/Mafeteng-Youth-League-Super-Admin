from io import BytesIO

import pytest
from fastapi import UploadFile

from app.services.storage import _validate_upload


def make_upload(filename: str, size: int) -> UploadFile:
    return UploadFile(filename=filename, file=BytesIO(b"x" * size))


def test_image_uploads_allow_three_megabytes():
    _validate_upload(make_upload("photo.jpg", 3 * 1024 * 1024))


def test_image_uploads_reject_over_three_megabytes():
    with pytest.raises(ValueError, match="Images must be less than 3MB"):
        _validate_upload(make_upload("photo.png", 3 * 1024 * 1024 + 1))


def test_documents_allow_five_megabytes():
    _validate_upload(make_upload("form.pdf", 5 * 1024 * 1024))


def test_documents_reject_over_five_megabytes():
    with pytest.raises(ValueError, match="Documents must be less than 5MB"):
        _validate_upload(make_upload("form.docx", 5 * 1024 * 1024 + 1))


def test_unsupported_file_types_are_rejected():
    with pytest.raises(ValueError, match="Unsupported file type"):
        _validate_upload(make_upload("archive.zip", 100))
