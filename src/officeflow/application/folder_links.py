"""Links are metadata; this service never mutates an external folder's contents."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from officeflow.infrastructure.folder_identity import probe


@dataclass(frozen=True, slots=True)
class FolderLink:
    id: int
    task_id: int
    name: str
    path: str
    position: int
    created_at: datetime
    folder_identity: str | None = None


@dataclass(frozen=True, slots=True)
class FolderRelinkCandidate:
    link: FolderLink
    path: Path


class FolderLinkRepository(Protocol):
    def list(self, task_id: int) -> tuple[FolderLink, ...]: ...
    def save(
        self,
        task_id: int,
        name: str,
        path: str,
        identity: int | None,
        expected: FolderLink | None = None,
        folder_identity: str | None = None,
    ) -> FolderLink: ...
    def delete(self, task_id: int, identity: int) -> None: ...


class FolderLinkService:
    def __init__(self, repository: FolderLinkRepository, managed_roots: tuple[Path, ...]) -> None:
        self.repository = repository
        self._roots = tuple(path.resolve() for path in managed_roots)

    def list(self, task_id: int) -> tuple[FolderLink, ...]:
        # Do not stat network shares while rendering the list or starting the app.
        return self.repository.list(task_id)

    def validate(self, path: Path) -> Path:
        if not path.is_absolute():
            raise ValueError("절대 경로의 외부 폴더를 선택하세요.")
        actual = path.resolve(strict=True)
        if any(actual.is_relative_to(root) for root in self._roots):
            raise ValueError("OfficeFlow 관리 폴더가 아닌 외부 업무 폴더를 선택하세요.")
        if not actual.is_dir():
            raise ValueError("사용할 수 있는 폴더가 아닙니다.")
        return actual

    def save(
        self,
        task_id: int,
        name: str,
        path: Path,
        *,
        identity: int | None = None,
        expected: FolderLink | None = None,
        canceled: Callable[[], bool] = lambda: False,
    ) -> FolderLink:
        name = name.strip()
        if not 1 <= len(name) <= 200 or any(ord(char) < 32 for char in name):
            raise ValueError("폴더 이름을 1~200자로 입력하세요.")
        result = probe(path, self._roots, capture=True, canceled=canceled)
        actual = Path(result["path"])
        if canceled():
            raise RuntimeError("폴더 연결을 취소했습니다.")
        # Canonicalize aliases so the same actual directory cannot be added twice.
        return self.repository.save(
            task_id, name, str(actual), identity, expected, folder_identity=result["identity"]
        )

    def rename(self, task_id: int, link: FolderLink, name: str) -> FolderLink:
        name = name.strip()
        if not 1 <= len(name) <= 200 or any(ord(char) < 32 for char in name):
            raise ValueError("폴더 이름을 1~200자로 입력하세요.")
        return self.repository.save(
            task_id, name, link.path, link.id, link, folder_identity=link.folder_identity
        )

    def inspect_open(
        self,
        link: FolderLink,
        canceled: Callable[[], bool] = lambda: False,
    ) -> Path | FolderRelinkCandidate:
        result = probe(
            Path(link.path),
            self._roots,
            expected=link.folder_identity,
            search=link.folder_identity is not None,
            canceled=canceled,
        )
        path = Path(result["path"])
        return FolderRelinkCandidate(link, path) if result["candidate"] else path

    def confirm_candidate(
        self,
        candidate: FolderRelinkCandidate,
        canceled: Callable[[], bool] = lambda: False,
    ) -> Path:
        link = candidate.link
        if not link.folder_identity or candidate.path.parent != Path(link.path).parent:
            raise ValueError("폴더 후보를 다시 확인하세요. 연결은 유지했습니다.")
        result = probe(
            candidate.path, self._roots, expected=link.folder_identity, canceled=canceled
        )
        if canceled():
            raise ValueError("폴더 연결을 취소했습니다.")
        actual = Path(result["path"])
        name = actual.name[:200] if link.name == Path(link.path).name else link.name
        self.repository.save(
            link.task_id, name, str(actual), link.id, link, folder_identity=link.folder_identity
        )
        return actual

    def open_path(self, link: FolderLink) -> Path:
        try:
            result = self.inspect_open(link)
            if isinstance(result, FolderRelinkCandidate):
                raise ValueError("폴더 위치 변경을 확인하세요. 연결은 유지했습니다.")
            return result
        except (OSError, ValueError) as error:
            raise ValueError(
                "폴더를 열 수 없습니다. 폴더 이동·이름 변경, 접근 권한, 공유 드라이브 연결을 "
                "확인하세요. 위치가 바뀌었다면 '폴더 다시 지정'을 사용하세요. 연결은 유지했습니다."
            ) from error

    def delete(self, task_id: int, identity: int) -> None:
        self.repository.delete(task_id, identity)
