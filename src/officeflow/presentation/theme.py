from __future__ import annotations

LIGHT_STYLESHEET = """
QWidget {
    color: #172033;
    font-family: "Segoe UI", "Malgun Gothic";
    font-size: 14px;
}
QMainWindow, #appRoot {
    background: #F4F6FA;
}
#sidebar {
    background: #162033;
    border: none;
}
#brandTitle {
    color: #FFFFFF;
    font-size: 22px;
    font-weight: 700;
}
#brandCaption {
    color: #91A0BA;
    font-size: 12px;
}
QPushButton[nav="true"] {
    color: #C8D2E4;
    background: transparent;
    border: none;
    border-radius: 9px;
    text-align: left;
    padding: 10px 14px;
}
QPushButton[nav="true"]:hover {
    background: #22314C;
    color: #FFFFFF;
}
QPushButton[nav="true"][selected="true"] {
    background: #2F6FED;
    color: #FFFFFF;
    font-weight: 600;
}
QPushButton[nav="true"][compact="true"] {
    text-align: center;
    padding: 10px 4px;
}
#topBar, #detailPanel, #contentCard {
    background: #FFFFFF;
    border: 1px solid #E1E6EF;
    border-radius: 12px;
}
#pageTitle {
    font-size: 24px;
    font-weight: 700;
}
#mutedText {
    color: #68738A;
}
QLineEdit {
    background: #F5F7FB;
    border: 1px solid #DCE2EC;
    border-radius: 9px;
    padding: 10px 12px;
}
QLineEdit:focus {
    background: #FFFFFF;
    border-color: #2F6FED;
}
QPushButton#primaryButton {
    color: #FFFFFF;
    background: #2F6FED;
    border: none;
    border-radius: 9px;
    font-weight: 600;
    padding: 10px 16px;
}
QPushButton#primaryButton:hover {
    background: #245CCC;
}
QFrame[summary="true"] {
    background: #F8FAFD;
    border: 1px solid #E5EAF2;
    border-radius: 10px;
}
QLabel[count="true"] {
    font-size: 20px;
    font-weight: 700;
}
"""
