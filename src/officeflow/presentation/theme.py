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
QDialog {
    background: #F4F6FA;
}
QMenu {
    color: #172033;
    background: #FFFFFF;
    border: 1px solid #D5DBE7;
    border-radius: 7px;
    padding: 5px;
}
QMenu::item {
    color: #172033;
    background: transparent;
    border-radius: 5px;
    padding: 7px 28px 7px 12px;
}
QMenu::item:selected {
    color: #172033;
    background: #DFE9FF;
}
QMenu::item:disabled {
    color: #9AA5B7;
}
QMenu::separator {
    height: 1px;
    background: #E1E6EF;
    margin: 5px 8px;
}
QCalendarWidget {
    color: #172033;
    background: #FFFFFF;
}
QCalendarWidget QWidget {
    color: #172033;
    background: #FFFFFF;
}
QCalendarWidget QWidget#qt_calendar_navigationbar {
    background: #2F6FED;
}
QCalendarWidget QToolButton {
    color: #FFFFFF;
    background: #2F6FED;
    border: none;
    border-radius: 4px;
    padding: 5px;
}
QCalendarWidget QToolButton:hover {
    background: #245CCC;
}
QCalendarWidget QSpinBox {
    color: #172033;
    background: #FFFFFF;
    border: 1px solid #DCE2EC;
    selection-color: #172033;
    selection-background-color: #DFE9FF;
}
QCalendarWidget QAbstractItemView:enabled {
    color: #172033;
    background: #FFFFFF;
    alternate-background-color: #F1F4F8;
    selection-color: #172033;
    selection-background-color: #DFE9FF;
    outline: none;
}
QCalendarWidget QAbstractItemView:disabled {
    color: #9AA5B7;
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
QPushButton[nav="true"]:disabled {
    color: #66758F;
    background: transparent;
    border: none;
}
QPushButton[nav="true"][compact="true"] {
    text-align: center;
    padding: 10px 4px;
}
#topBar, #detailPanel, #contentCard, #calendarCard {
    background: #FFFFFF;
    border: 1px solid #E1E6EF;
    border-radius: 12px;
}
#pageTitle {
    font-size: 24px;
    font-weight: 700;
}
#calendarMonthTitle {
    font-size: 20px;
    font-weight: 700;
    min-width: 120px;
    qproperty-alignment: AlignCenter;
}
#calendarDayTitle {
    font-size: 16px;
    font-weight: 700;
}
#calendarPrevious, #calendarNext {
    min-width: 34px;
    max-width: 34px;
    font-size: 20px;
    padding: 5px;
}
QPushButton[calendarPrimary="true"] {
    color: #245CCC;
    background: #E3ECFF;
    border-color: #AFC5F5;
    font-weight: 600;
}
QListWidget#calendarDayList, QListWidget#reminderList {
    background: #F8FAFD;
    border: 1px solid #E1E6EF;
    border-radius: 8px;
    outline: none;
    padding: 3px;
}
QListWidget#checklistList, QListWidget#taskWorkLogList, QListWidget#workLogBrowserList,
QListWidget#attachmentList {
    background: #FFFFFF;
    border: 1px solid #DCE2EC;
    border-radius: 8px;
    outline: none;
    padding: 4px;
}
QListWidget#checklistList::item, QListWidget#taskWorkLogList::item,
QListWidget#workLogBrowserList::item, QListWidget#attachmentList::item {
    border-radius: 5px;
    padding: 8px 10px;
}
QListWidget#checklistList::item:selected, QListWidget#taskWorkLogList::item:selected,
QListWidget#workLogBrowserList::item:selected, QListWidget#attachmentList::item:selected {
    color: #172033;
    background: #DFE9FF;
}
QTabWidget::pane {
    background: #FFFFFF;
    border: 1px solid #DCE2EC;
    border-radius: 9px;
    top: -1px;
}
QTabBar::tab {
    color: #526078;
    background: #E9EDF4;
    border: 1px solid #DCE2EC;
    padding: 9px 16px;
    margin-right: 3px;
}
QTabBar::tab:selected {
    color: #245CCC;
    background: #FFFFFF;
    font-weight: 600;
}
QListWidget#calendarDayList::item, QListWidget#reminderList::item {
    border-radius: 5px;
    padding: 6px 8px;
}
QListWidget#calendarDayList::item:selected, QListWidget#reminderList::item:selected {
    color: #172033;
    background: #DFE9FF;
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
QTextEdit {
    background: #FFFFFF;
    border: 1px solid #DCE2EC;
    border-radius: 8px;
    padding: 8px 10px;
}
QComboBox, QDateEdit, QTimeEdit, QSpinBox {
    background: #FFFFFF;
    border: 1px solid #DCE2EC;
    border-radius: 8px;
    padding: 5px 10px;
}
QTextEdit:focus, QComboBox:focus, QDateEdit:focus, QTimeEdit:focus, QSpinBox:focus {
    border-color: #2F6FED;
}
QGroupBox {
    background: #FFFFFF;
    border: 1px solid #E1E6EF;
    border-radius: 10px;
    font-weight: 600;
    margin-top: 10px;
    padding: 12px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
}
QPushButton {
    background: #F1F4F8;
    border: 1px solid #DCE2EC;
    border-radius: 8px;
    padding: 8px 12px;
}
QPushButton:hover {
    background: #E8EDF5;
}
QPushButton:checked {
    color: #245CCC;
    background: #E3ECFF;
    border-color: #AFC5F5;
    font-weight: 600;
}
QPushButton:disabled {
    color: #9AA5B7;
    background: #F7F8FA;
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
QPushButton[primaryAction="true"] {
    color: #FFFFFF;
    background: #2F6FED;
    border: none;
    font-weight: 600;
}
QPushButton[primaryAction="true"]:hover {
    background: #245CCC;
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
QPushButton[summaryJump="true"] {
    color: #68738A;
    background: transparent;
    border: none;
    padding: 1px 0;
    text-align: left;
}
QPushButton[summaryJump="true"]:hover {
    color: #2F6FED;
    background: transparent;
}
#filterBar QComboBox {
    padding: 6px 8px;
}
QListView#taskList {
    background: #FFFFFF;
    border: none;
    outline: none;
}
QListView#taskList::item {
    background: transparent;
    border: none;
}
QStatusBar {
    color: #526078;
    background: #F4F6FA;
}
"""
