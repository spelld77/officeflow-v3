# OfficeFlow 3.0.5 릴리스 체크리스트

- 기준일: 2026-10-02 (한국 시간)
- 대상: Windows 10/11 x64, Python 3.12 실행 모드
- 포함: v2.6 가져오기 기능 종료 및 기존 v3 데이터 호환 유지
- 데이터: 기존 경로·원본 ID·가져오기 표식 보존, DB 스키마 변경 없음

## 검증·게시

- [x] 제거 변경 검증: 381개 전체 테스트, Ruff·mypy 및 격리 실행 통과
- [x] 3.0.5 버전·전체 품질 검사 (381개 통과, 커버리지 85%, Ruff·mypy 통과; 최종 패키징 테스트 9개 통과)
- [x] Windows 실행 파일·설치본 재빌드 및 격리 설치/재설치/제거·사용자 데이터 보존 검증
- [x] Python ZIP의 격리 런타임·진단 8/8·smoke·사용자 데이터 제외 및 가져오기 코드/빈 폴더 제거 확인
- [x] 배포 파일 SHA-256·명세·버전 확인 (880개 파일, 앱·설치본 3.0.5)
- [x] 소스 커밋·main 업로드
- [x] v3.0.5 태그·GitHub 최신 안정 릴리스 및 5개 자산 게시
- [x] GitHub 자산 uploaded 상태·크기·서버 SHA-256과 로컬 검증본 일치 확인

## 산출물

- `dist/installer/OfficeFlow-3.0.5-Setup.exe`
- `dist/python/OfficeFlow-Python-3.0.5.zip`
- `dist/python/OfficeFlow-Python-3.0.5.zip.sha256.txt`
- `dist/SHA256SUMS.txt`
- `dist/release-manifest.json`

검증은 격리된 `build`/`artifacts` 데이터만 사용한다. 실제 DB·첨부·실사용 설치 등록·자동 시작은 변경하지 않는다.
이전 로컬 3.0.4 빌드 산출물은 `artifacts/release-before-3.0.5`에 보존했다. 기존 GitHub 릴리스는 변경하지 않는다.
변환 규칙·이전 릴리스의 설계와 검증 문서는 역사 기록으로 유지한다.

Python 검증은 3.0.4에서 준비한 격리 런타임을 재사용하되, 최종 ZIP의 새 추출 폴더에서
3.0.5 소스·메타데이터·DB 무결성을 검사했다. 개발용 `.venv`도 3.0.5 메타데이터로 갱신했다.
EXE의 내장 모듈 목록과 Python ZIP에서 가져오기 전용 모듈이 모두 없는 것을 확인했다.

## 게시 확인

- 릴리스 소스: `b32ab3d2958976fbeb86c0c222800f9da0a10da4`
- 태그: `v3.0.5` (위 소스 커밋, annotated tag)
- 공개 릴리스: https://github.com/spelld77/officeflow-v3/releases/tag/v3.0.5
- 게시: 한국 시간 2026-10-03 00:02:50 (UTC 2026-10-02 15:02:50)
- 상태: 최신 안정 버전, `draft=false`, `prerelease=false`
- GitHub 자산 5개의 uploaded 상태·크기·SHA-256이 로컬 검증본과 모두 일치한다.
- 설치본: 44,270,572 bytes, SHA-256 `e3eb558b88b107920f76765e7cb4719a8f960b99fd910a433d8fb92a4ef885b6`
- Python ZIP: 209,322 bytes, SHA-256 `26dbf8ec8712a6eb33963f822b34f7e7b35a6fa15165ed7229aa4c0773438152`

버전 준비·빌드는 10월 2일, 공개는 자정을 지난 10월 3일에 완료했다.
이 게시 기록은 릴리스 이후 문서 커밋으로 main에 추가한다. 배포 태그와 공개 파일은 변경하지 않는다.
