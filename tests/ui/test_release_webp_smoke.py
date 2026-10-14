import pytest
from PySide6.QtGui import QImage, QImageWriter

from officeflow.main import _verify_smoke_webp


def test_release_smoke_exercises_webp_codec_in_memory(qapp):
    _verify_smoke_webp()


@pytest.mark.parametrize("stage", ["encode", "decode"])
def test_release_smoke_rejects_unavailable_webp_codec(qapp, monkeypatch, stage):
    if stage == "encode":
        monkeypatch.setattr(QImageWriter, "write", lambda *_args: False)
    else:
        monkeypatch.setattr(QImage, "fromData", lambda *_args: QImage())
    with pytest.raises(RuntimeError, match="WebP codec"):
        _verify_smoke_webp()
