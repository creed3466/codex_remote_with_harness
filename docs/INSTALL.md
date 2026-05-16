# codex_rc 설치 가이드

이 문서는 배포판에 맞춰 실제 작동 가능한 설치·초기 설정 순서를 기준으로 작성합니다.

## 1) 선행 요구사항

- Python 3.12 이상.
- `git` 설치.
- Discord bot을 실행할 수 있는 권한이 있는 계정.
- 로컬에서 동작하는 Codex CLI (`codex` 바이너리).
- `tmux` (선택): `CODEX_RC_TRANSPORT=ws` 사용 시 권장.

### Codex CLI 준비

`codex_rc`는 로컬 `codex app-server`를 구동합니다. 최소 조건은:

- `codex` 실행 가능
- `codex app-server`가 현재 환경에서 실행 가능한 상태

테스트용으로:

```bash
codex --version
codex app-server --help
```

## 2) 설치 방법 (공식 배포판 문서 기준)

현재는 GitHub 레포지토리를 직접 설치해 배포합니다.

### A. `uv tool` 설치 (권장)

```bash
uv tool install git+https://github.com/creed3466/codex_remote_with_harness.git
```

실행:

```bash
codex-rc
codex-rc-discord
```

### B. `pipx` 설치

```bash
pipx install git+https://github.com/creed3466/codex_remote_with_harness.git
```

### C. 소스 편집/개발용

```bash
git clone git@github.com:creed3466/codex_remote_with_harness.git codex_rc
cd codex_rc
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
codex-rc
```

개발이 끝나면:

```bash
codex-rc-discord
```

## 3) Discord 봇 설정

### 앱/봇 생성

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 새 앱 생성.
2. Bot 탭에서 토큰 발급 (`CODEX_RC_DISCORD_TOKEN`).
3. General Information에서 앱 ID 확인 (`CODEX_RC_DISCORD_APP_ID`).
4. `Privileged Gateway Intents`에서 필요한 항목 활성화:
   - Message Content Intent
   - Server Members Intent (필요 시)
   - Reactions Intent
5. OAuth2 URL Generator에서 **bot** 및 **applications.commands** 권한으로 초대 URL 생성.
6. 서버에 봇 초대.

권장 채널 권한:

- Send Messages
- Use Slash Commands
- Add Reactions
- Read Message History

### 즉시 슬래시 동기화

`CODEX_RC_GUILD_ID`에 개발/테스트 길드 ID를 넣으면 슬래시 명령 동기화가 즉시 반영됩니다.
비워 두면 전역 동기화(지연 가능)로 동작합니다.

## 4) `.env` 구성

설치 후 다음 중 하나로 환경 변수를 준비합니다.

- 권장: `codex-rc` 실행 후 프롬프트로 생성
- 또는: `.env.example` 복사 후 직접 편집

```bash
cp .env.example .env
$EDITOR .env
```

필수 키:

- `CODEX_RC_DISCORD_TOKEN`
- `CODEX_RC_DISCORD_APP_ID`
- `CODEX_RC_ALLOWED_USER_IDS` (또는 권한을 넓히는 JSON 구조)

권장 보안 값:

- `CODEX_RC_DISCORD_CHANNEL_IDS` (운영 채널 제한)
- `CODEX_RC_OPS_CHANNEL_ID` (운영 알림 채널)
- `CODEX_RC_MENTION_ON_COMPLETE` (알림용 유저 멘션)

주요 선택 값:

- `CODEX_RC_TRANSPORT` (`ws` 또는 `stdio`, 기본 `ws`)
- `CODEX_RC_DEFAULT_SANDBOX` (`workspace-write` 기본)
- `CODEX_RC_DEFAULT_APPROVAL` (`on-request` 기본)
- `CODEX_RC_EVENT_LOG_MODE` (`errors` / `debug` / `off`)
- `CODEX_RC_HEALTH_HOST`, `CODEX_RC_HEALTH_PORT`

상세 변수 설명은 `README.md`와 `.env.example`를 함께 참고하세요.

## 5) 첫 실행

### 전방향 실행

```bash
codex-rc-discord
```

정상 기동 시, 봇 로그에 Discord Ready 이벤트와 명령 등록 로그가 표시됩니다.

### Discord에서 첫 동작

```text
/codex start ~/Project/my-repo
```

입력 후 `README`/`USAGE`에 있는 사용법을 따라 바로 코드 요청을 보냅니다.

## 6) 운영: 갱신, 재시작, 제거

### 업그레이드

`uv tool` 설치 사용자:

```bash
uv tool uninstall codex-rc   # 선택적
uv tool install git+https://github.com/creed3466/codex_remote_with_harness.git
```

`pipx` 사용자:

```bash
pipx uninstall codex_rc || true
pipx install --force git+https://github.com/creed3466/codex_remote_with_harness.git
```

소스 사용자:

```bash
git pull
uv pip install -e ".[dev]"
```

### 중지/제거

- 봇은 `Ctrl-C`로 종료 가능.
- `.env`는 민감 값 보관 파일이므로 안전하게 삭제/백업합니다.
- 소스 삭제 시 런타임 데이터(`data/`)는 프로젝트와 함께 지워집니다.
  운영 배포의 경우 런타임만 별도 보존 정책을 적용하세요.

## 7) 서비스형 운영 팁

- `systemd`/`supervisor` 같은 프로세스 매니저로 `codex-rc-discord`를 장기 실행.
- 별도 모니터링에는 `/healthz` (기본 `CODEX_RC_HEALTH_PORT=0`, 활성화 시 설정 필요) 사용.
- 장애 분석은 `CODEX_RC_LOG_LEVEL=DEBUG` + `CODEX_RC_EVENT_LOG_MODE=debug`로 전환해 임시 재현.
