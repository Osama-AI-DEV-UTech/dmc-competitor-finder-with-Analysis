import hashlib

from app.config import Settings
from ui.auth import check_password


def test_plain_and_hash():
    assert check_password(Settings(app_password="s3cret"), "s3cret")
    assert not check_password(Settings(app_password="s3cret"), "S3cret")
    assert not check_password(Settings(app_password="s3cret"), "")
    h = hashlib.sha256(b"pw").hexdigest()
    assert check_password(Settings(app_password_sha256=h), "pw") and not check_password(Settings(app_password_sha256=h), "px")
    assert not check_password(Settings(), "anything")  # nothing configured -> never accepts
