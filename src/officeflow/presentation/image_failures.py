"""Shared, explicit image diagnostics without inferring integrity from file roles."""

from officeflow.application.attachments import AttachmentMissingError, AttachmentPreviewError
from officeflow.application.file_operations import FileInUseError
from officeflow.application.image_attachments import ImageFailure, ImageReadError

LABELS = {
    ImageFailure.UNSUPPORTED: "형식 미지원",
    ImageFailure.SIZE: "크기 제한",
    ImageFailure.READ: "읽기 불가",
    ImageFailure.INTEGRITY: "고정 내용 불일치",
    ImageFailure.CHANGING: "다시 확인 필요",
    ImageFailure.DECODE: "미리보기 실패",
}


def image_error_label(error: object) -> str:
    if isinstance(error, (ImageReadError, AttachmentPreviewError)):
        return LABELS[error.kind]
    if isinstance(error, (FileNotFoundError, AttachmentMissingError)):
        return "파일 없음"
    if isinstance(error, (PermissionError, FileInUseError)):
        return "읽기 불가"
    return "미리보기 실패"
