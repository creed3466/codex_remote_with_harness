# codex_rc 사용 가이드

아래는 운영 가능한 핵심 워크플로우 중심의 사용 설명서입니다.

## 핵심 흐름

### 1) 세션 시작

```text
/codex start /path/to/project
```

`/codex start`는 현재 채널에 프로젝트 컨텍스트를 바인딩합니다. 이후의
메시지(텍스트/이미지)는 해당 채널의 세션으로 전달됩니다.

### 2) 작업 요청

세션 시작 후 일반 텍스트 메시지로 요청을 보냅니다.

```text
이 함수의 경계 조건 테스트를 추가해줘.
```

이미지 첨부도 같은 채널에서 함께 전송 가능합니다.

### 3) 진행 확인

- Codex 버튼/임베드로 진행이 표시됩니다.
- 진행 중 응답이 길면 진행표시 반응이 바뀝니다.

### 4) 완료

완료 시 세션 상태가 갱신되고, 이전 컨텍스트를 `thread` 상태로 유지합니다.

## 시작/종료 명령

| 명령 | 기능 |
| --- | --- |
| `/codex start <path>` | 세션 시작 |
| `/codex stop` | 세션 종료 및 handoff 저장 |
| `/codex continue` | 최신 handoff 기반으로 새 세션 계속 |
| `/codex restart` | 배포 후 코덱스 게이트웨이 재시작 (확인 필요) |
| `/codex status` | 세션 상태, thread/sandbox/approval 보기 |

슬래시 명령이 어려운 클라이언트는 `/codex start ...` 같은 텍스트 접두사도
동일하게 사용 가능합니다.

## turn 제어

### Steering(동일 요청 이어 받기)

실행 중인 turn이 있을 때 동일 채널에서 추가 메시지를 보내면 자동으로
`turn/steer`로 라우팅되어 같은 turn 안에서 맥락이 반영됩니다.

### Cancel

- `/codex cancel` : 현재 진행중 turn 즉시 중단 요청.
- 메시지에 `✋` 반응 추가(allowed user): 같은 기능의 단축 동작.

## 승인(Approval) 처리

Codex가 승인 요청을 보내면 Discord embed + 버튼으로 표시됩니다.

지원되는 동작:

- `success` (계속 진행)
- `reject` (중단)
- 추가 승인 유형은 구현된 Codex 프로토콜 범위 내에서 라우팅

운영 안정성:

- `CODEX_RC_AUTO_APPROVE=true` 로 설정하면 승인 흐름을 자동 승인(테스트/비권장).
- 기본은 사용자의 수동 승인입니다.

## 히스토리 / handoff

### Thread history

- `/codex history` : 최근 스레드 목록 확인.
- `/codex resume <handoff-id>` : 특정 handoff로 신규 컨텍스트 시작.
- `/codex continue` : 최신 handoff로 계속.

`/codex stop`은 새로 시작할 때 재활용 가능한 handoff를 생성합니다.
`/codex restart`는 코드 반영 후 서버 프로세스를 재시작합니다.

### 새 스레드

- `/codex new` : 현재 채널에서 새 Codex thread를 강제로 발행.

### Rollback

- `/codex rollback` : 기본 1회 롤백.
- `/codex rollback 3` : 최근 3 turn만큼 rollback.

## 네이티브 Codex slash passthrough

다음 명령은 텍스트 접두사 없이도 허용됩니다(채널 허용 목록 적용):

`/model [id]`, `/permissions <preset>`, `/review [target]`, `/fork`,
`/goal [text]`, `/compact`, `/new`, `/resume`, `/status`, `/capabilities`,
`/exit`, `/quit`.

동일 명령은 `/codex` 접두사 형태로도 동작합니다(`/codex model`, `/codex exit` 등).

## tmux / remote 조작

`CODEX_RC_TRANSPORT=ws`일 때 채널마다 tmux 세션이 생성됩니다.

- 코드 실행 터미널 화면과 동일 backend를 공유.
- Codex TUI 접근은 `codex --remote <ws-url>` 형태로 가능합니다.
- 세션 관리 권한은 운영 환경에서 로컬 사용자 권한 정책에 따라 달라집니다.

## 로그, health, 진단

- 기본 로그:
  - 운영: `data/logs/errors/`
  - 자세한 추적: `data/debug/` (`CODEX_RC_EVENT_LOG_MODE=debug`일 때)
- 운영 알림 채널: `CODEX_RC_OPS_CHANNEL_ID` 설정 시 `ERROR/EXCEPTION` 이벤트 전달.
- 헬스엔드포인트:
  - `CODEX_RC_HEALTH_HOST` + `CODEX_RC_HEALTH_PORT`
  - `/healthz`는 버전, 활성 채널 수, transport, uptime을 반환합니다.

## 보안/동작 제어

### Allowlist

- `CODEX_RC_ALLOWED_USER_IDS=uid1,uid2` : 간단 모드
- `CODEX_RC_ALLOWED_USER_IDS='{"*":["uid1"],"12345678901234":["uid2"]}'` : 채널별 모드
- 공란: 기본 허용(로컬 단일 사용자 전용 추천)

### Sandbox / approval 기본값

- `CODEX_RC_DEFAULT_SANDBOX`: `read-only`, `workspace-write`, `danger-full-access`
- `CODEX_RC_DEFAULT_APPROVAL`: `on-request`, `never`, `untrusted`

## 실패 복구 체크리스트

1. 권한이 필요한 환경변수 누락 (`TOKEN`, `APP_ID`, `ALLOWED_USER_IDS`).
2. `codex` 바이너리 미설치 또는 권한 문제.
3. 슬래시 동기화 지연(전역 등록 시 1~5분 대기).
4. 토큰/웹소켓 권한(특히 tmux 연동) 문제.
5. 로그 레벨을 `DEBUG`로 올리고 handoff/세션 메시지로 추가 재현.

문제별 상세 대응은 [Troubleshooting](TROUBLESHOOTING.md)을 확인하세요.
