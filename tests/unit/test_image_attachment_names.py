from datetime import datetime

import pytest

from officeflow.application.image_attachments import image_filename, suggested_image_name


def test_default_name_uses_add_time_and_collision_number():
    now = datetime(2026, 10, 10, 14, 30, 25)
    name = "캡처_2026-10-10_14-30-25.png"
    assert suggested_image_name(now, ()) == name
    assert suggested_image_name(now, (name,)) == "캡처_2026-10-10_14-30-25 (1).png"


@pytest.mark.parametrize("name,expected", [("오류화면", "오류화면.png"), ("결과.PNG", "결과.PNG")])
def test_png_names(name, expected):
    assert image_filename(name) == expected


@pytest.mark.parametrize("name", ["", "../사진", "CON.png", "사진.jpg", "사진.", "x" * 101])
def test_bad_names(name):
    with pytest.raises(ValueError):
        image_filename(name)
