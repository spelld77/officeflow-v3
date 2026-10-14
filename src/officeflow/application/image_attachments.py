"""Conservative bounds and user-visible names for explicit image attachments."""

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from officeflow.domain.attachment import validate_display_name

MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_PENDING_BYTES = 128 * 1024 * 1024
MAX_PENDING_IMAGES = 8
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


class ImageFailure(StrEnum):
    UNSUPPORTED = "unsupported"
    SIZE = "size"
    READ = "read"
    INTEGRITY = "integrity"
    CHANGING = "changing"
    DECODE = "decode"


class ImageReadError(ValueError):
    """A decoder failure is not evidence that a frozen asset was modified."""

    def __init__(self, kind: ImageFailure, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def image_filename(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValueError("사진 이름을 입력하세요.")
    validate_display_name(value)
    if not PurePosixPath(value).suffix:
        value += ".png"
    if len(value) > 100:
        raise ValueError("사진 이름은 확장자를 포함해 100자 이내로 입력하세요.")
    return validate_display_name(value, extension=".png")


def suggested_image_name(now: datetime, existing: tuple[str, ...]) -> str:
    stem = now.strftime("캡처_%Y-%m-%d_%H-%M-%S")
    names = {name.casefold() for name in existing}
    value, index = f"{stem}.png", 0
    while value.casefold() in names:
        index += 1
        value = f"{stem} ({index}).png"
    return value
