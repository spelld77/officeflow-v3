# OfficeFlow 3.0.3 릴리스 체크리스트

- 기준일: 2026-10-01
- 대상: Windows 10/11 x64, Python 3.12 실행 모드

## 검증

- [x] 첨부파일 검색 구현·통합·UI 및 기존 기능 테스트 260개 통과 (구현 검증)
- [x] 작은 창, 150%·200% 화면 배율 검증
- [x] 기존 DB 업그레이드·보호 사본·실패 롤백 및 백업 복원·v2.6 가져오기 검증
- [x] 대량 데이터 성능 측정·사용자 도움말 작성
- [x] 3.0.3 버전의 전체 품질 검사 (265개 통과, 83% 커버리지)
- [x] Windows 실행 파일·설치 프로그램 재빌드 및 실행 점검
- [x] 깨끗한 설치·재설치·제거 및 사용자 데이터 보존 검증
- [x] Python 배포 ZIP 격리 환경 구성·실행 점검 (새 Python 3.12 가상환경, 진단 8/8)
- [x] ZIP에 사용자 DB·첨부·가상환경·바이트코드 캐시·개발용 egg-info 제외 확인
- [x] 패키징된 실행 파일로 구버전 DB 업그레이드·기존 업무/첨부 유지·보호 사본 확인
- [x] 버전·SHA-256·릴리스 명세 일치 검증 (배포 파일 888개)
- [ ] 소스 커밋·main 업로드
- [ ] v3.0.3 태그·GitHub 릴리스와 배포 파일 게시

## 산출물

- `dist/installer/OfficeFlow-3.0.3-Setup.exe`
- `dist/python/OfficeFlow-Python-3.0.3.zip`
- `dist/python/OfficeFlow-Python-3.0.3.zip.sha256.txt`
- `dist/SHA256SUMS.txt`
- `dist/release-manifest.json`

실제 사용자 데이터는 `%LOCALAPPDATA%\OfficeFlow`에 보존하며 검증은 별도 임시 폴더만 사용한다.
기존 3.0.0~3.0.2 GitHub 릴리스와 다운로드 파일은 유지한다.

설치 수명주기 검증은 같은 앱 파일을 사용하는 `VerificationBuild` 설치본으로 수행했다.
검증용 앱 ID를 별도로 쓰고 시작 등록·제거를 제외하여 실사용 설치 등록과 자동 시작 설정을
변경하지 않았다. 공개 설치본의 ID와 자동 시작 동작은 기존과 동일하게 유지한다.

Python ZIP은 wheelhouse를 포함하지 않으므로 새 환경의 최초 설정에는 인터넷이 필요하다.
검증용 가상환경과 테스트 DB는 프로젝트의 `artifacts`/`build`에만 생성하며 Git에서 제외한다.
