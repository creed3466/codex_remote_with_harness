# 문제 해결 가이드

운영 초반 이슈를 빠르게 줄이는 대상별 체크리스트입니다.

## 1) Discord 권한/설정

### 증상
- 봇이 가입한 채널에서 아무 반응이 없음
- `/codex`가 보이지 않음
- 슬래시 명령이 보였다가 사라짐

### 점검
- Bot Token과 App ID가 `.env`에 정확한지 확인.
- `CODEX_RC_GUILD_ID` 값이 서버 ID인지 확인(테스트 Guild 등록).
- Bot에 최소 권한 부여:
  - Send Messages
  - Use Slash Commands
  - Read Message History
  - Add Reactions
- Message Content Intent/Reaction Intent 활성화 여부 점검.
- 초대 URL이 정확한 서버를 가리키는지 확인.

## 2) 슬래시 동기화 지연

### 증상
- `/codex` 명령이 즉시 안 보이거나 선택지 일부만 보임.

### 점검
- `CODEX_RC_GUILD_ID`를 채널이 속한 길드 ID로 설정 후 재기동.
- 전역 동기화는 Discord측 반영 지연(수 분)이 있으므로 테스트는 우선 Guild 방식 권장.
- 코드 변경 후 `codex-rc-discord` 재시작.

## 3) APP token / ID 오류

### 증상
- 시작 직후 인증 실패 로그.

### 점검
- `CODEX_RC_DISCORD_TOKEN`에 공백/개행이 없는지 확인.
- `CODEX_RC_DISCORD_APP_ID`가 숫자 문자열인지 확인.
- 토큰 재발급 후 `.env` 반영.

## 4) Codex CLI/`app-server` 문제

### 증상
- `/codex start` 후 즉시 실패
- `codex` 프로세스가 즉시 종료되거나 attach 불가

### 점검
- 다음 실행 확인:

```bash
codex --version
codex app-server --help
```

- `PATH`에서 `codex` 접근 가능한지 확인.
- `CODEX_RC_TRANSPORT=ws`에서 `tmux`가 설치되어 있는지 확인(권장).
- `CODEX_RC_EVENT_LOG_MODE=errors`에서 오류 로그가 `data/logs/errors`에 생성되는지 확인.

## 5) tmux / websocket 문제

### 증상
- `codex --remote` 접속이 안 되거나 TUI가 붙지 않음.

### 점검
- `CODEX_RC_TRANSPORT=ws`인지 확인 (`stdio`이면 리모트 TUI 미생성).
- 실행 사용자에게 tmux 접근 권한이 있는지 확인.
- 동일 프로젝트에서 중복 세션이 남아 있는지 확인 후 정리.

## 6) Python/의존성 설치 문제

### 증상
- `codex-rc` 실행 시 `ImportError`, `ModuleNotFoundError`

### 점검
- 설치 방식이 현재 실행 경로와 맞는지 확인:
  - `uv tool` 유저 경로 vs 가상환경 경로 혼재 여부.
- 소스 설치 시 `uv venv`/`uv pip install -e ".[dev]"` 재실행.
- 필요한 경우 가상환경 재생성:

```bash
rm -rf .venv
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

## 7) 로깅이 없어서 추적이 안 되는 경우

### 점검
- `CODEX_RC_LOG_LEVEL=DEBUG`.
- `CODEX_RC_LOG_PATH`를 쓰거나 콘솔 로그를 확인.
- 필요 시 `CODEX_RC_EVENT_LOG_MODE=debug`로 전환 (완전 추적 증가).
- Ops 채널 연동이 필요한 경우 `CODEX_RC_OPS_CHANNEL_ID` 설정.

## 8) 자주 놓치는 실수

- `CODEX_RC_ALLOWED_USER_IDS`에 운영 사용자 ID 미입력(로컬 기본 허용이 아닌 환경에서는 즉시 차단).
- `.env`의 값이 따옴표로 둘러싸여 파서 실패.
- 프로젝트 경로 오탈자/퍼미션 부족(`/codex start ~/proj` 경로 오류).

## 9) 긴급 복구

1. `codex-rc-discord` 종료.
2. `.env` 핵심 값 검증.
3. `uv pip install -e ".[dev]"` 또는 툴 재설치.
4. `uv tool`/`pipx` 실행 경로 충돌 정리 후 재기동.
5. 필요 시 `data/state/memory.json`을 백업하고 새로 시작.

문제가 계속되면 PR/이슈 템플릿에 최소 정보(OS, Python, `codex --version`, transport,
실행 명령, 예측/실제 결과, 마스킹한 로그)를 포함해 공유하세요.
