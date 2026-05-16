# Launch & Distribution Pack

이 문서는 배포 전/후에 바로 적용할 실행 전략입니다. 목적은 star-growth보다
우선 순위가 높은 “방문자 첫 30초 전환”입니다.

## A. 왜 다른 high-star 레포들은 잘 보이나?

1. **첫 화면에서 의도가 한 줄에 보인다.**
   - 설치, 첫 실행, 첫 명령이 위쪽에 바로 있음.
   - 설치 실패 원인보다 사용자 목표를 먼저 답함.

2. **작업 결과를 바로 보여준다.**
   - 데모(영상/스크린샷) 링크, 화면 예시, 대표 명령 출력이 초반에 존재.
   - “무엇을 할 수 있나”보다 “지금 바로 할 수 있나”가 먼저 보임.

3. **명령 표가 짧고 정확하다.**
   - 자주 쓰는 8~12개 명령만 노출하고, 덜 쓰는 세부는 링크로 분기.
   - 길고 추상적인 설정 블록 대신 실수 줄이기 가이드 제공.

4. **기여장벽이 낮다.**
   - 기여 가이드에는 “이슈/PR 템플릿 + 필요한 로그 샘플”이 있어야 재작업이 적음.

5. **운영 신호를 보여준다.**
   - CI 상태, 버전 정책, 버그 리포팅 경로가 명확.
   - 문서와 코드의 차이가 없으면 신뢰가 올라감.

## B. 이번 배포판 적용 체크리스트

- [x] README 1분 읽기형으로 개편: 첫 블록에 가치 제안, 설치 3개 경로, 즉시 실행 예시.
- [x] 설치/사용/문제해결 문서를 분리 (`docs/INSTALL.md`, `docs/USAGE.md`, `docs/TROUBLESHOOTING.md`).
- [x] 비교분석형 문서 추가 (`docs/LAUNCH.md`)로 배포 운영 문턱 기록.
- [x] GitHub 템플릿(버그/기능/PR) 추가.
- [ ] 시각적 데모 제작 및 릴리스 노트 링크 연결 (`docs/assets/demo.gif`).
- [ ] 배포 노트와 릴리스 주기 안내 추가.

## C. 배포 체크리스트

### README 레이어

- [ ] 프로젝트 한 줄 요약(타깃+결과+제약) 확정
- [ ] 60초 빠른 시작(Install → Setup → first command) 실행 성공
- [ ] 데모 링크(영상/GIF/스크린샷) 유효성 점검
- [ ] 명령 표에서 실제 동작과 1:1 일치 확인

### 문서 레이어

- [ ] 설치 가이드: Discord Bot 생성 → `.env` → 실행까지 누락 없이 정합성 확인
- [ ] 사용 가이드: `start`, `cancel`, `rollback`, `compact`, `history` 흐름 포함
- [ ] 트러블슈팅: 권한/슬래시/Codex/tmux/logs 4축 커버

### 운영 레이어

- [ ] `CODEX_RC_TRANSPORT` 기본값(`ws`)의 장단점 안내
- [ ] 기본 `--help`/health 검사 루틴 등록
- [ ] 로그 로테이션/보존 정책 공지 (`CODEX_RC_ERROR_LOG_RETENTION_DAYS`)

## D. 발표 복사본(영문)

### Short (1줄)

> `codex_rc` lets your team run their local Codex CLI from Discord in under a minute—no cloud relay, no API-key vault, just `/codex start` and go.

### Long (약 80자 내외 + 상세)

> `codex_rc` is a local-first Discord control surface for Codex. It keeps your code local, sessions per channel, and approvals visible in chat. It works with `ws` transport and `tmux -- codex --remote`, supports image prompts, handoff resume, and a documented troubleshooting path for the first 10 minutes of setup.

## E. 유지/출시 운영 포인트

- 릴리스 후 24시간 내 최소 3개 채널에서 첫 성공률 기록.
- 신규 이슈에서 공통 실패 패턴(권한, slash, codex, tmux) 집계.
- Star 증가보다 “First Success Rate(install → start)”를 핵심 KPI로 추적.
- 실패 패턴이 높은 항목을 다음 릴리스 노트 선행으로 이동.

## F. 유지보수 전제(사전 점검)

- GitHub 레포지토리 주소가 공개 배포 대상과 동일해야 함(README badge와 install URL 일치).
- `uv tool`/`pipx` 사용자, 소스 사용자별 지원 정책 정의.
- 데모 업데이트 일정(적어도 월 1회 또는 주요 릴리스마다 1개).
- PR 템플릿 채택 후, 버그 보고의 로그 포맷 강제 수집.

## G. 추가 실행 제안

1. 스타 수치보다 **신뢰 신호**를 먼저 늘릴 것: 데모, 트러블슈팅, 운영 가이드.
2. 한국어 사용자 유입이 크다면, docs 하단에 한국어 요약 뱃지 혹은 별도
   `docs/INSTALL.ko.md`를 다음 마일스톤에서 추가.
3. `/codex`가 기본 명령이므로, 문서의 첫 30초 블록에서 핵심 4개 명령만 반복.
