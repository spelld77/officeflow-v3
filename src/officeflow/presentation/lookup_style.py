"""Scoped light controls for file lookup; no change to other application screens."""

SCROLLBAR_STYLE = """
QScrollBar:vertical { background:#EEF3FB; width:12px; margin:0; }
QScrollBar::handle:vertical { background:#779CEB; min-height:24px; border-radius:5px; }
QScrollBar::handle:vertical:hover { background:#4D7EE0; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:none; }
"""

CHECKBOX_STYLE = """
QCheckBox::indicator { width:16px; height:16px; background:#FFFFFF;
    border:1px solid #98A8C4; border-radius:3px; }
QCheckBox::indicator:checked { background:#2F6FED; border-color:#2F6FED; }
QCheckBox::indicator:disabled { background:#E8EDF6; border-color:#B6C2D6; }
"""
