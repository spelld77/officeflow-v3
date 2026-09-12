from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from officeflow.infrastructure.database.migrate import upgrade_database
from officeflow.infrastructure.database.models import TaskRecord
from officeflow.infrastructure.database.session import SessionFactory, create_database_engine


def generate(database_file: Path, count: int) -> None:
    upgrade_database(database_file)
    now = datetime.now(UTC).replace(microsecond=0)
    engine = create_database_engine(database_file)
    sessions = SessionFactory(engine)
    with sessions.transaction() as session:
        for index in range(count):
            start = now + timedelta(hours=index % 24, days=(index % 21) - 7)
            duration_days = 3 if index % 11 == 0 else 0
            session.add(
                TaskRecord(
                    title=f"샘플 업무 {index + 1}",
                    description="대량 목록과 기간 일정 검증을 위한 개발용 데이터입니다.",
                    status="completed" if index % 7 == 0 else "active",
                    priority=("normal", "attention", "important", "urgent")[index % 4],
                    is_pinned=index % 17 == 0,
                    all_day=index % 3 == 0,
                    starts_at=start,
                    ends_at=start + timedelta(days=duration_days, hours=1),
                    timezone="Asia/Seoul",
                    created_at=now,
                    updated_at=now,
                    completed_at=now if index % 7 == 0 else None,
                )
            )
    engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Create OfficeFlow development sample data")
    parser.add_argument("database", type=Path)
    parser.add_argument("--count", type=int, default=50)
    args = parser.parse_args()
    if args.count < 0:
        parser.error("--count must be zero or greater")
    generate(args.database, args.count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
