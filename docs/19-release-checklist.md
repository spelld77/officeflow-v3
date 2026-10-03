# OfficeFlow 3.0.6 릴리스 체크리스트

## 범위

- 전체 정비 A1–A8·B1–B4와 캘린더 기록·첨부 중심 UX를 함께 배포한다.
- DB 스키마와 기본 사용자 데이터 경로는 유지한다. 새 백업 형식 v2는 3.0.6 이상에서 복원한다.
- 실제 사용자 DB·첨부파일·설치 등록·Windows 시작 설정은 건드리지 않고 격리 검증한다.

## 검증 및 배포

- [x] 버전·EXE 리소스·설치파일·도움말·변경 기록을 3.0.6으로 정렬
- [x] 전체 567개 테스트 통과(209.01초, 커버리지 87%), Ruff·mypy 74개 소스·diff 검사
- [x] 실제 Windows 100%·125% 배율에서 각각 47개 UI 검사 통과(25.94초·25.70초)
- [x] 이전 로컬 빌드 산출물 보존 후 EXE·설치파일·Python ZIP 재생성
- [x] 격리 EXE 실행 및 설치·재설치·제거·데이터 보존 검증
- [x] 새 폴더에 Python ZIP을 풀어 격리 런타임 진단 8/8·실행 검증
- [x] 산출물 버전·크기·867개 파일 체크섬·사용자 자료 제외 검증; EXE의 개발 캐시 제외 보완 및 배포 검사 9개 통과
- [ ] main 커밋·push 및 v3.0.6 태그
- [ ] GitHub 릴리스와 5개 자산 공개
- [ ] 공개 자산 상태·크기·SHA-256 및 원격 커밋 확인

## 공개 결과

배포 완료 후 소스 커밋과 공개 자산 검증 결과를 기록한다.

## 격리 검증 자료

- 전체 검사: `artifacts/release-3.0.6-tests-01`
- 실제 Windows: `artifacts/release-3.0.6-windows-100-01`, `artifacts/release-3.0.6-windows-125-01`
- 최종 배포 검사: `artifacts/release-3.0.6-packaging-final-01`
- 최종 빌드 로그: `artifacts/release-3.0.6-build-final.log`
- Python ZIP 검증: `artifacts/release-3.0.6-extracted-final`; 개발 환경이 아닌 기존 격리 런타임의 의존성을 재사용하고 새 소스 경로·버전·빈 DB를 확인
- 이전 3.0.5 로컬 산출물: `artifacts/release-before-3.0.6`

이 자료는 Git과 공개 ZIP에 포함하지 않는다. 사용자의 실제 DB·첨부와 기존 공개 릴리스도 변경하지 않는다.
전체 커버리지 검사 중 대용량 Excel 메모리 검사가 30초 진단 스택을 출력했으나 이후 정상 완료·통과했다.
