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
QWidget#taskEditorBody {
    color: #172033;
    background: #F4F6FA;
}
QScrollArea#taskEditorScroll,
QScrollArea#taskEditorScroll QWidget#qt_scrollarea_viewport {
    background: #F4F6FA;
    border: none;
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
QPushButton[nav="true"][shortHeight="true"] {
    padding: 4px 14px;
}
QPushButton[nav="true"][shortHeight="true"][compact="true"] {
    padding: 4px 4px;
}
#topBar, #detailPanel, #contentCard, #calendarCard {
    background: #FFFFFF;
    border: 1px solid #E1E6EF;
    border-radius: 12px;
}
QLineEdit#taskSearchEdit[searchActive="true"] {
    color: #173A82;
    background: #EEF4FF;
    border: 2px solid #2F6FED;
}
#taskSearchFeedback {
    color: #173A82;
    background: #EEF4FF;
    border: 1px solid #AFC5F5;
    border-radius: 8px;
}
#taskSearchFeedbackLabel {
    color: #173A82;
    font-weight: 600;
}
#clearTaskSearch, #emptyClearTaskSearch {
    color: #245CCC;
    background: #FFFFFF;
    border-color: #AFC5F5;
    font-weight: 600;
}
#emptyTaskTitle {
    font-size: 16px;
    font-weight: 600;
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
#calendarDayCount {
    color: #68738A;
    padding: 3px 8px;
}
#calendarDayCount[hasMany="true"] {
    color: #1F55C3;
    background: #E3ECFF;
    border: 1px solid #AFC5F5;
    border-radius: 9px;
    font-weight: 700;
}
#calendarDayMoreHint {
    color: #B45309;
    font-weight: 600;
}
#calendarDayPanel {
    background: #FFFFFF;
}
QSplitter#calendarDaySplitter::handle {
    background: #E1E6EF;
    border-radius: 3px;
    margin: 2px 36px;
}
QSplitter#calendarDaySplitter::handle:hover {
    background: #7FA3EF;
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
QPushButton[calendarAction="true"] {
    min-width: 0px;
    padding: 5px 8px;
    font-size: 13px;
}
QListWidget#calendarDayList, QListWidget#reminderList {
    background: #F8FAFD;
    border: 1px solid #E1E6EF;
    border-radius: 8px;
    outline: none;
    padding: 3px;
}
QListWidget#checklistList, QListWidget#taskWorkLogList, QListWidget#workLogBrowserList,
QListWidget#attachmentList, QListWidget#attachmentCleanupList, QListWidget#reminderPreviewList, QListWidget#reminderSettingsList,
QListWidget#attachmentExportResults, QListWidget#attachmentRecoveryList, QListWidget#folderLinksList {
    color: #172033;
    background: #FFFFFF;
    selection-color: #172033;
    selection-background-color: #DFE9FF;
    border: 1px solid #DCE2EC;
    border-radius: 8px;
    outline: none;
    padding: 4px;
}
QListWidget#attachmentList[dragActive="true"] {
    background: #EEF4FF;
    border: 2px dashed #2F6FED;
}
#attachmentDropHint {
    color: #245CCC;
    background: #EEF4FF;
    border: 1px dashed #AFC5F5;
    border-radius: 7px;
    padding: 8px 10px;
}
QListWidget#checklistList::item, QListWidget#taskWorkLogList::item,
QListWidget#workLogBrowserList::item, QListWidget#attachmentList::item,
QListWidget#attachmentCleanupList::item, QListWidget#reminderSettingsList::item,
QListWidget#attachmentExportResults::item, QListWidget#attachmentRecoveryList::item, QListWidget#folderLinksList::item {
    border-radius: 5px;
    padding: 8px 10px;
}
QListWidget#checklistList::item:selected, QListWidget#taskWorkLogList::item:selected,
QListWidget#workLogBrowserList::item:selected, QListWidget#attachmentList::item:selected,
QListWidget#attachmentCleanupList::item:selected, QListWidget#reminderSettingsList::item:selected,
QListWidget#attachmentExportResults::item:selected, QListWidget#attachmentRecoveryList::item:selected, QListWidget#folderLinksList::item:selected {
    color: #172033;
    background: #DFE9FF;
}
QListWidget#attachmentCleanupList::item:disabled {
    color: #68738A;
}
QWidget#dataManagementPage {
    color: #172033;
    background: #FFFFFF;
}
QScrollArea#dataManagementScroll, QScrollArea#dataManagementScroll QWidget#qt_scrollarea_viewport {
    background: #FFFFFF;
    border: none;
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
QListWidget#calendarDayList::item, QListWidget#reminderList::item, QListWidget#reminderPreviewList::item {
    border-radius: 5px;
    padding: 6px 8px;
}
QListWidget#calendarDayList::item:selected, QListWidget#reminderList::item:selected, QListWidget#reminderPreviewList::item:selected {
    color: #172033;
    background: #DFE9FF;
}
QListWidget#calendarDayList QScrollBar:vertical, QScrollArea#taskEditorScroll QScrollBar:vertical {
    width: 13px;
    background: #EDF1F7;
    border: none;
    border-radius: 6px;
    margin: 2px;
}
QListWidget#attachmentList QScrollBar:vertical,
QListWidget#documentFileList QScrollBar:vertical,
QListWidget#viewCopyList QScrollBar:vertical,
QListWidget#folderLinksList QScrollBar:vertical,
QScrollArea#taskArchiveScroll QScrollBar:vertical {
    width: 13px;
    background: #EDF1F7;
    border: none;
    border-radius: 6px;
    margin: 2px;
}
QListWidget#attachmentList QScrollBar::handle:vertical,
QListWidget#documentFileList QScrollBar::handle:vertical,
QListWidget#viewCopyList QScrollBar::handle:vertical,
QListWidget#folderLinksList QScrollBar::handle:vertical,
QScrollArea#taskArchiveScroll QScrollBar::handle:vertical {
    min-height: 28px;
    background: #7FA3EF;
    border-radius: 5px;
}
QListWidget#attachmentList QScrollBar::handle:vertical:hover,
QListWidget#documentFileList QScrollBar::handle:vertical:hover,
QListWidget#viewCopyList QScrollBar::handle:vertical:hover,
QListWidget#folderLinksList QScrollBar::handle:vertical:hover,
QScrollArea#taskArchiveScroll QScrollBar::handle:vertical:hover {
    background: #4F7FDF;
}
QListWidget#attachmentList QScrollBar::add-line:vertical,
QListWidget#attachmentList QScrollBar::sub-line:vertical,
QListWidget#attachmentList QScrollBar::add-page:vertical,
QListWidget#attachmentList QScrollBar::sub-page:vertical,
QListWidget#documentFileList QScrollBar::add-line:vertical,
QListWidget#documentFileList QScrollBar::sub-line:vertical,
QListWidget#documentFileList QScrollBar::add-page:vertical,
QListWidget#documentFileList QScrollBar::sub-page:vertical,
QListWidget#viewCopyList QScrollBar::add-line:vertical,
QListWidget#viewCopyList QScrollBar::sub-line:vertical,
QListWidget#viewCopyList QScrollBar::add-page:vertical,
QListWidget#viewCopyList QScrollBar::sub-page:vertical,
QListWidget#folderLinksList QScrollBar::add-line:vertical,
QListWidget#folderLinksList QScrollBar::sub-line:vertical,
QListWidget#folderLinksList QScrollBar::add-page:vertical,
QListWidget#folderLinksList QScrollBar::sub-page:vertical,
QScrollArea#taskArchiveScroll QScrollBar::add-line:vertical,
QScrollArea#taskArchiveScroll QScrollBar::sub-line:vertical,
QScrollArea#taskArchiveScroll QScrollBar::add-page:vertical,
QScrollArea#taskArchiveScroll QScrollBar::sub-page:vertical {
    background: transparent;
    border: none;
    height: 0;
}
QListWidget#calendarDayList QScrollBar::handle:vertical, QScrollArea#taskEditorScroll QScrollBar::handle:vertical {
    min-height: 28px;
    background: #7FA3EF;
    border-radius: 5px;
}
QListWidget#calendarDayList QScrollBar::handle:vertical:hover, QScrollArea#taskEditorScroll QScrollBar::handle:vertical:hover {
    background: #4F7FDF;
}
QListWidget#calendarDayList QScrollBar::add-line:vertical,
QListWidget#calendarDayList QScrollBar::sub-line:vertical,
QListWidget#calendarDayList QScrollBar::add-page:vertical,
QListWidget#calendarDayList QScrollBar::sub-page:vertical,
QScrollArea#taskEditorScroll QScrollBar::add-line:vertical,
QScrollArea#taskEditorScroll QScrollBar::sub-line:vertical,
QScrollArea#taskEditorScroll QScrollBar::add-page:vertical,
QScrollArea#taskEditorScroll QScrollBar::sub-page:vertical {
    background: transparent;
    border: none;
    height: 0;
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
QTextEdit, QPlainTextEdit {
    color: #172033;
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
QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDateEdit:focus, QTimeEdit:focus, QSpinBox:focus {
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
QPushButton#dangerButton {
    color: #B42318;
    background: #FFF4F2;
    border-color: #F3B7B1;
    font-weight: 600;
}
QPushButton#dangerButton:hover {
    color: #FFFFFF;
    background: #C4322B;
    border-color: #C4322B;
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
QListWidget#documentFileList, QListWidget#viewCopyList, QListWidget#internalRecoveryList {
    color: #172033;
    background: #FFFFFF;
    selection-color: #172033;
    selection-background-color: #DFE9FF;
    border: 1px solid #DCE2EC;
    border-radius: 8px;
    padding: 4px;
}
QListWidget#documentFileList QWidget#qt_scrollarea_viewport,
QListWidget#viewCopyList QWidget#qt_scrollarea_viewport,
QListWidget#internalRecoveryList QWidget#qt_scrollarea_viewport {
    color: #172033;
    background: #FFFFFF;
}
QListWidget#documentFileList::item, QListWidget#viewCopyList::item, QListWidget#internalRecoveryList::item {
    padding: 7px 6px;
}
QListWidget#documentFileList::item:selected, QListWidget#viewCopyList::item:selected, QListWidget#internalRecoveryList::item:selected {
    color: #172033;
    background: #DFE9FF;
}
QPlainTextEdit QWidget#qt_scrollarea_viewport {
    color: #172033;
    background: #FFFFFF;
}
QLineEdit[inputError="true"], QDateEdit[inputError="true"],
QComboBox[inputError="true"], QPushButton[inputError="true"] {
    border: 2px solid #C62828;
    background-color: #FFF4F4;
    color: #10243A;
}
"""
