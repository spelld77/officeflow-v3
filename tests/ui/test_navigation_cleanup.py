from officeflow.application.tasks import TaskView
from officeflow.infrastructure.settings.store import AppSettings
from officeflow.presentation.main_window import MainWindow


def test_sidebar_groups_navigation_without_removing_actions(qtbot, task_service):
    window = MainWindow(AppSettings(), task_service)
    qtbot.addWidget(window)
    assert [button.property("fullLabel") for button in window._nav_buttons] == [
        "오늘",
        "예정",
        "캘린더",
        "전체 업무",
        "중요",
        "대기",
        "완료",
        "업무일지",
        "휴지통",
        "관리",
    ]
    assert [action.text() for action in window._management_menu.actions()] == [
        "데이터 관리",
        "설정",
        "도움말",
    ]
    window._set_view(TaskView.IMPORTANT)
    assert window._view_buttons[TaskView.IMPORTANT].property("selected")
    assert not window._data_action.isEnabled()
    assert window._management_button.text() == "관리"
    assert window._management_button.menu() is window._management_menu
