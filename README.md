# 윈도우 카카오 하루 단위 자료 수집

> **운영 기준일**: 2026-09-20
> **표준 작업 경로**: `D:\kakao\kakao-cli-win`

---

## 1. 목적

본 프로젝트는 Windows PC 환경에서 구동되는 카카오톡 로컬 암호화 데이터베이스(`chatLogs_*.edb` 등)를 안전하게 추출·복호화하여 단일 SQLite(`messages_v2.sqlite`)로 통합하고, 일일 단위의 대화 증분 수집 및 원격 분석 서버로의 안전한 무결성 미러링을 자동화하는 로컬 운영 파이프라인입니다.

외부 비인가 유출 없이 모든 키 복구 및 복호화는 로컬 PC 내부 메모리와 저장소에서만 수행되며, 원격 백업 역시 암호화된 SSH 터널과 엄격한 무결성 검증을 거쳐 안전하게 수행됩니다.

---

## 2. 현재 운영 구조

```text
[KakaoTalk PC 원본 데이터]
  └─ %LOCALAPPDATA%\Kakao\KakaoTalk\users\<user-hash>\chat_data\
      (chatLogs_*.edb, appstate.dat, TalkUserDB.edb 등 - 철저한 읽기 전용 접근)
                           │
                           ▼ (키 복구 및 복호화 / 동기화 파이프라인)
[프로그램 작업 루트]
  └─ D:\kakao\kakao-cli-win
      ├─ kwin/                 : 복호화, 통합, 미러링 핵심 파이썬 모듈
      ├─ backend/server.py     : 로컬 HTTP API 서버 (포트 8780)
      ├─ sync-on-login.ps1     : 로그인/스케줄 연동 동기화 워커
      ├─ post-sync-mirror.ps1  : 스냅샷 무결성 검증 및 원격 미러 러너
      └─ web/                  : 로컬 대화방 뷰어 웹 UI (빌드 정적 파일 서빙)
                           │
                           ▼ (실행 상태 관리 및 로그 기록)
[워커 런타임 저장소]
  └─ %LOCALAPPDATA%\KakaoCollector
      ├─ state\last-sync.json          : 원자적 동기화 상태 마커 (IN_PROGRESS, SUCCESS, FAILED)
      └─ logs\worker-YYYY-MM-DD.jsonl  : 일일 단위 실행 통계 로그 (민감정보 배제)
                           │
                           ▼ (로컬 통합 산출물)
[수집 결과 산출물]
  └─ data\output\
      ├─ v2_keys.json                      : 복구된 SQLCipher DB 복호화 키 메타데이터
      └─ <user-id>\
          ├─ v2_decrypted\                 : 복호화된 개별 SQLite 파일 (*.sqlite)
          ├─ messages_v2.sqlite            : 통합 메시지·대화방·연락처 SQLite DB
          ├─ snapshot\messages_v2.sqlite   : 온라인 백업 API로 생성된 읽기 전용 스냅샷
          └─ mirror_state.json             : 원격 미러링 동기화 상태 마커
                           │
                           ▼ (보안 SSH + 무결성 검증 전송)
[원격 서버 분석 복사본]
  └─ 저장소 외부의 안전한 비공개 원격 위치
      (원격 호스트의 private 격리 디렉터리, 소유권 UID:GID 및 권한 700/600 적용 관리)
```

- **`%LOCALAPPDATA%` 카카오 원본**: 원본 메신저 DB 및 상태 파일이 보관되는 위치입니다. 본 시스템은 데이터 파손을 원천 차단하기 위해 카카오 원본 데이터에 대해 **철저한 읽기 전용(`mode=ro`)**으로만 접근합니다.
- **`D:\kakao\kakao-cli-win` 프로그램**: 수집 도구, 백엔드 API 서버, 동기화/미러링 러너가 설치된 고정 작업 루트입니다.
- **`%LOCALAPPDATA%\KakaoCollector` 워커**: Windows 로그인 또는 작업 스케줄러가 구동하는 동기화 워커의 상태(`state\last-sync.json`) 및 일일 로그(`logs\worker-YYYY-MM-DD.jsonl`)를 격리 보관합니다.
- **`data\output` 수집 결과**: 키 메타(`v2_keys.json`), 복호화 산출물, 통합 SQLite DB(`messages_v2.sqlite`) 및 로컬 스냅샷이 생성되는 디렉터리입니다.
- **서버 분석 복사본**: 저장소 밖 비공개 원격 위치(VPS 격리 환경)로 안전하게 동기화되는 분석용 복사본입니다. 보안 유지를 위해 실제 원격 private 경로는 문서나 로그에 일체 기재하지 않습니다.

---

## 3. 동기화 및 미러링 파이프라인 흐름

```text
Windows 로그인 / 스케줄러 트리거
       │
       ▼
[sync-on-login.ps1]
  ├─ 1. 시스템 전역 Mutex(Local\KakaoCollectorSync) 획득 (단일 실행 보장)
  ├─ 2. last-sync.json에 "status: IN_PROGRESS", "stage: startup" 원자적 기록
  ├─ 3. KakaoTalk 프로세스 및 백엔드 헬스체크 (미실행 시 backend\server.py 백그라운드 기동)
  └─ 4. Stage: sync 진입 (최대 840초 데드라인, 일시 오류 시 최대 3회 백오프 재시도)
         ├─ GET /api/status -> 동기화 전 메시지 수(messages_before) 기록
         ├─ POST /api/sync  -> backend SYNC_LOCK 하에 `python -m kwin v2sync` 수행
         │                     (chatLogs_*.edb 복호화 후 messages_v2.sqlite 재구축)
         └─ GET /api/status -> 동기화 후 메시지 수(messages_after) 및 ready 상태 검증
       │
       ▼ (동기화 성공 시)
[last-sync.json 원자적 갱신]
  └─ "status: SUCCESS", "stage: COMPLETE", 전후 건수 및 완료시각 기록
       │
       ▼ (Fail-Closed 검증 통과 시)
[post-sync-mirror.ps1 -UploadOnly 훅 호출]
  ├─ 1. last-sync.json이 방금 완료된 최신 SUCCESS 마커인지 검증 (부적합 시 즉시 건너뜀)
  ├─ 2. messages_v2.sqlite를 SQLite 온라인 백업 API를 통해 snapshot 디렉터리에 읽기 전용 복사
  ├─ 3. 원격 서버에 임시 파일(.partial)로 안전 업로드
  ├─ 4. 원격지 python3 원샷 스크립트 실행 (stdout/stderr 리다이렉션으로 SSH 채널 행 방지)
  │      - PRAGMA integrity_check == ok 검증
  │      - 로컬-원격 메시지 수, min/max sentAtIso 일치 검증
  │      - SHA-256 해시 일치 검증
  ├─ 5. 검증 통과 시 원격지 원자적 교체 (mv .partial -> current) 및 권한(700/600) 검증
  │      (실패 시 이전 버전 롤백 및 .partial 즉시 정리)
  └─ 6. mirror_state.json에 "status: SUCCESS" 갱신
       │
       ▼
[worker-YYYY-MM-DD.jsonl 기록]
  └─ 일일 실행 결과 한 줄 추가 (상태, 건수 통계, 소요시간) 및 정상 종료
```

- **실패 처리**: 동기화 또는 미러 단계에서 오류 발생 시, 허용된 안전 토큰(`TIMEOUT`, `CONNECTION_FAILED`, `SERVER_BUSY`, `HTTP_500`, `HTTP_502`, `HTTP_503`, `HTTP_504`, `WAITING_READY`, `BACKEND_LAUNCH_FAILED`, `POST_SYNC_NOT_READY`, `DEADLINE_EXCEEDED`, `MAX_RETRIES_EXCEEDED`, `SYNC_FAILED`) 중 하나로 정규화하여 `last-sync.json`에 `status: FAILED`로 원자적 기록하며, 민감한 시스템 예외나 파일 경로는 절대 노출하지 않습니다.

---

## 4. 코드에서 확인되는 “하루 단위 증분”의 정확한 의미

코드(`kwin/collect.py`, `backend/server.py`, `sync-on-login.ps1`)를 통해 확인되는 “하루 단위 증분 수집”의 실제 동작 메커니즘은 다음과 같습니다.

1. **카카오톡 클라이언트의 로컬 누적 메커니즘**:
   - 카카오톡 PC 클라이언트는 로그인 상태에서 사용자가 열람하거나 백그라운드 수신된 대화를 로컬 암호화 DB(`chat_data\chatLogs_*.edb`)에 계속해서 누적 기록합니다.
2. **`v2sync`의 재구축(`rebuild`) 방식**:
   - `kwin v2sync`(`cmd_v2decrypt` + `cmd_v2collect`)는 현재 시점의 EDB 파일 전체를 복호화한 후, 통합 대상 `messages_v2.sqlite`의 `messages` 테이블을 `DROP TABLE IF EXISTS messages`한 뒤 처음부터 다시 적재(`rebuild`)합니다.
   - 이전 작업에서 누락되었거나 불완전하게 처리된 레코드가 잔존하지 않도록 보장합니다.
3. **복합 기본키에 의한 무결성 보장**:
   - `messages` 테이블의 기본키는 `PRIMARY KEY(chatId, logId)`로 지정되어 있으며, 복호화된 레코드는 `INSERT OR IGNORE` 문으로 삽입됩니다. 따라서 동일한 메시지가 여러 번 처리되더라도 중복 레코드가 발생하지 않습니다.
4. **증분 수치의 실질적 의미**:
   - `sync-on-login.ps1`은 동기화 직전(`messages_before`)과 동기화 직후(`messages_after`)의 총 메시지 수를 측정합니다.
   - 하루 1회(또는 PC 로그인 시) 워커가 실행되므로, `messages_after - messages_before`의 차이가 직전 실행 이후부터 당일까지 추가로 수집된 “하루 단위 순수 증분 메시지 수”를 의미합니다.
5. **멱등성(Idempotency)과 재실행 안전성**:
   - 하루 중 시스템이 여러 번 재부팅되거나 워커가 반복 실행되어도 `messages_v2.sqlite`는 항상 현재 EDB의 전체 정합 상태로 수렴하며, 데이터 왜곡이나 중복 증분이 발생하지 않습니다.
6. **날짜 경계 기반 조회 API 지원**:
   - 백엔드 서버의 `GET /api/messages?chatId=...&since=YYYY-MM-DD&until=YYYY-MM-DD` 엔드포인트는 KST 기준의 날짜 문자열을 파싱하여, `since`는 `00:00:00`, `until`은 `23:59:59`의 유닉스 타임스탬프로 자동 변환합니다. 이를 통해 특정 하루 동안 발생한 메시지만을 정확하게 슬라이싱하여 조회하거나 분석 파이프라인으로 추출할 수 있습니다.

---

## 5. 주요 파일 및 디렉터리 구성

- **`kwin/`**: 카카오톡 데이터 처리 핵심 파이썬 패키지
  - `keyderiv.py`, `decryptor.py`, `memkey.py`, `meminspect.py`: KakaoTalk 프로세스 메모리 검사 및 EDB 복호화 키 탐색/유도.
  - `collect.py`: 복호화된 개별 SQLite 파일들을 `messages_v2.sqlite`로 병합하고 대화방·연락처·멤버십 메타데이터를 통합.
  - `mirror.py`: SQLite 온라인 백업 API 기반 읽기 전용 스냅샷 생성 및 원격 VPS SSH 미러 파이프라인.
  - `cli.py`: `python -m kwin` CLI 진입점 (`v2recover`, `v2sync`, `v2recent`, `v2mirror` 등 제공).
- **`backend/server.py`**: 로컬 백엔드 HTTP 서버
  - 포트 8780에서 구동되며 상태 조회(`/api/status`, `/api/health`), 동기화 요청(`/api/sync`), 대화방/메시지 조회(`/api/rooms`, `/api/messages`), 정적 웹 파일 서빙 담당.
- **`sync-on-login.ps1`**: 동기화 총괄 워커 스크립트
  - 뮤텍스 기반 단일 인스턴스 보장, 백엔드 기동 관리, 상태 마커 원자적 기록, 백오프 재시도 및 미러링 훅 호출 수행.
- **`post-sync-mirror.ps1`**: 원격 미러 전송 러너 스크립트
  - `-UploadOnly`, `-Status`, `-Enable`, `-Disable` 옵션을 지원하며 스냅샷 생성 및 원격 업로드/무결성 검증을 총괄.
- **`install-sync-worker.ps1`**: 동기화 워커 설치 스크립트
  - `%LOCALAPPDATA%\KakaoCollector\sync-on-login.ps1`로 최신 워커 스크립트를 원자적이고 멱등하게 복사/배포.
- **`install-post-sync-mirror.ps1`**: 포스트 싱크 미러 훅 설치 스크립트
  - `sync-on-login.ps1` 내부에 미러 연동 훅 블록을 멱등하게 주입 및 갱신.
- **`selftest.py`**: 파이프라인 자가 진단 스크립트
  - 가상 암호화 EDB 생성 및 복호화 전체 단계를 자체 검증.
- **`build-web.ps1`**: 웹 대화방 뷰어 프론트엔드 빌드 스크립트 (`web` 디렉터리 npm 빌드).

---

## 6. 설치, 실행 및 테스트 명령

본 저장소에 실제로 존재하며 동작이 검증된 PowerShell 기반 실행 명령입니다.

### 6.1. 가상환경 활성화 및 사전 준비

```powershell
# 가상환경 활성화 (필요 시)
.\.venv\Scripts\Activate.ps1

# 프론트엔드 웹 UI 빌드 (정적 배포 파일 생성)
.\build-web.ps1
```

### 6.2. 워커 및 미러 훅 설치

```powershell
# 1. 로그인 동기화 워커 설치 (로컬 앱데이터 경로로 원자적 복사)
.\install-sync-worker.ps1

# 워커 설치 상태 확인
.\install-sync-worker.ps1 -Status

# 2. 포스트 싱크 미러 훅 주입/점검
.\install-post-sync-mirror.ps1

# 미러 훅 설치 상태 확인
.\install-post-sync-mirror.ps1 -Status
```

### 6.3. 수동 실행 및 파이프라인 검증

```powershell
# 1. 카카오톡 실행 중 상태에서 초기 키 탐색 및 복구
python -m kwin v2recover

# 2. 복구된 키로 로컬 DB 복호화 및 통합
python -m kwin v2sync

# 3. 통합 DB 최신 10건 메타데이터 확인
python -m kwin v2recent --limit 10

# 4. 로컬 백엔드 서버 수동 구동 (포트 8780)
python backend\server.py --host 127.0.0.1 --port 8780

# 5. 동기화 워커 수동 테스트 실행 (프로세스 체크 건너뛰기 옵션 지원)
.\sync-on-login.ps1 -SkipProcessCheck

# 6. 미러 러너 상태 확인 및 업로드 수동 테스트
.\post-sync-mirror.ps1 -Status
.\post-sync-mirror.ps1 -UploadOnly
```

### 6.4. 자동화 테스트 수행

```powershell
# 단위/통합 테스트 전체 수행 (65개 항목)
python -m pytest

# 가상 암호화 EDB 기반 자가 진단 수행
python selftest.py
```

---

## 7. `last-sync`와 로그 점검법 (민감 정보 보호)

운영 상태를 확인할 때는 대화 원문이나 개인정보가 콘솔/로그에 출력되지 않도록 **반드시 통계 및 상태 필드만 선별하여 점검**해야 합니다.

### 7.1. 최신 동기화 상태 마커 확인

```powershell
Get-Content "$env:LOCALAPPDATA\KakaoCollector\state\last-sync.json" | ConvertFrom-Json | Select-Object status, stage, started_at, completed_at, elapsed_seconds, attempts, messages_before, messages_after, db_updated_kst, error
```

- 정상 예시: `status: SUCCESS`, `stage: COMPLETE`, `messages_after >= messages_before`.

### 7.2. 당일 일일 워커 로그 확인

```powershell
$todayLog = Join-Path $env:LOCALAPPDATA "KakaoCollector\logs\worker-$(Get-Date -Format 'yyyy-MM-dd').jsonl"
if (Test-Path $todayLog) {
    Get-Content $todayLog | ConvertFrom-Json | Select-Object status, stage, completed_at, elapsed_seconds, attempts, messages_before, messages_after, error
} else {
    Write-Host "오늘 생성된 워커 로그 파일이 없습니다."
}
```

### 7.3. 미러링 전송 상태 확인

```powershell
.\post-sync-mirror.ps1 -Status
```

- STOP 마커 활성 여부, 마지막 시도 시각(`lastAttemptKst`), 성공 시각(`lastSuccessKst`), 스냅샷 메시지 건수(`messageCount`)를 안전하게 확인할 수 있습니다.

---

## 8. GitHub 복원 한계 경고

> [!WARNING]
> **GitHub 저장소는 프로그램 소스 코드와 설정 스크립트만 복원할 수 있습니다.**
>
> 카카오톡 로컬 원본 데이터, 암호화 키(`v2_keys.json`), 복호화된 대화 DB(`messages_v2.sqlite`), 일일 런타임 상태 마커(`last-sync.json`) 및 로그는 저장소에 포함되지 않습니다.
>
> 새로운 PC 환경이나 다른 위치에서 저장소를 클론한 경우, 반드시 카카오톡 PC 클라이언트에 정상 로그인한 뒤 `python -m kwin v2recover` 및 `python -m kwin v2sync`를 직접 실행하여 초기 키 복구 및 로컬 DB 구성을 새로 완료해야 정상적인 운영이 가능합니다.

---

## 9. GitHub 제외 대상 목록 (Git Ignore)

다음 파일과 디렉터리는 보안 및 개인정보 보호를 위해 Git 추적에서 영구적으로 제외되어 있으며, 원격 저장소로 절대 커밋되어서는 안 됩니다.

- `data/output/` : 복구된 키, 복호화 산출물, 통합 DB 및 스냅샷 전체
- `messages_v2.sqlite` 및 관련 파일
- `v2_decrypted/` : 복호화된 임시 SQLite 파일 디렉터리
- `v2_keys.json` : 추출된 복호화 대칭키 파일
- `*.edb`, `*.sqlite`, `*.db`, `*.wal`, `*.shm` : 모든 원본/복호화 데이터베이스 및 저널 파일
- `logs/` : 실행 로그 파일
- `state/` : 동기화 상태 마커 파일
- `snapshot/` : 로컬 미러링 스냅샷 파일
- `.env` : 로컬 환경 변수 설정 파일
- 자격증명 파일 : SSH 개인키, 인증 토큰, 비밀번호 등

---

## 10. 고정 프로젝트 경로 주의사항

본 프로젝트의 모든 연동 스크립트(`sync-on-login.ps1`, `install-sync-worker.ps1`, `install-post-sync-mirror.ps1`, `post-sync-mirror.ps1`)와 윈도우 작업 스케줄러 등록 경로는 기본적으로 아래 경로에 고정되어 있습니다:

```text
D:\kakao\kakao-cli-win
```

만약 저장소를 다른 드라이브나 폴더(예: `C:\...`)로 이동하거나 복원할 경우:
1. `sync-on-login.ps1` 내부의 백엔드 실행 커맨드라인(`cd /d D:\kakao\kakao-cli-win`)을 해당 위치에 맞게 수정해야 합니다.
2. `install-sync-worker.ps1` 및 `install-post-sync-mirror.ps1`를 반드시 다시 실행하여 `%LOCALAPPDATA%\KakaoCollector`의 워커 스크립트를 갱신해야 합니다.
3. 등록된 Windows 작업 스케줄러의 실행 경로 및 시작 위치 인수를 빠짐없이 점검해야 합니다.

---

## 11. 카카오 원본 읽기 전용 및 민감 로그 금지 원칙

1. **카카오 원본 불변성 보장**:
   - 카카오톡 원본 로컬 디렉터리(`%LOCALAPPDATA%\Kakao\KakaoTalk\users\...`)의 파일은 프로그램 실행 중 어떠한 경우에도 직접 수정, 삭제, 덮어쓰기를 하지 않습니다.
   - 모든 연결은 읽기 전용 모드(`?mode=ro` 또는 읽기 전용 파일 스트림)로만 접근합니다.
2. **민감 정보 출력 및 로깅 엄격 금지**:
   - 대화 본문 텍스트, 개인 닉네임, 친구 이름, 사용자 고유 식별 번호, 채팅방 제목, 암호화 키 값, 세션 토큰 등은 콘솔 화면, 상태 파일(`last-sync.json`), 실행 로그(`worker-*.jsonl`), 미러링 상태(`mirror_state.json`)에 일체 기록하거나 출력하지 않습니다.
   - 오직 집계된 메시지 건수(`count`), 타임스탬프(`sentAtIso`), 안전한 표준 오류 코드(`errorCode`)만 허용됩니다.

---

## 12. 트러블슈팅 및 일일 운영 체크리스트

### 12.1. 주요 오류 코드별 트러블슈팅

| 오류 코드 / 증상 | 추정 원인 | 해결 조치 |
| :--- | :--- | :--- |
| `WAITING_READY` | 카카오톡 PC 미실행 또는 백엔드 서버 기동 지연 | 카카오톡에 로그인되어 있는지 확인하고, 포트 8780이 점유되어 있지 않은지 점검 후 `python backend\server.py --port 8780` 수동 실행. |
| `SERVER_BUSY` | 이전 동기화 작업이 진행 중이거나 백엔드 `SYNC_LOCK` 미해제 | 다른 `v2sync` 작업이 실행 중인지 확인하고, 작업 완료 대기 또는 백엔드 프로세스 재시작. |
| `POST_SYNC_NOT_READY` | `v2sync` 실행 후 DB 생성 실패 또는 키 미인식 | 카카오톡 대화방 몇 개를 직접 열어둔 상태에서 `python -m kwin v2recover`를 재수행하여 키 갱신. |
| `TIMEOUT` / `DEADLINE_EXCEEDED` | 대용량 DB 처리 지연 또는 네트워크 미러링 지연 | 전체 데드라인(기본 840초) 및 SSH 네트워크 연결 상태를 확인하고, `post-sync-mirror.ps1 -Status` 점검. |
| `STALE_MARKER` / `MARKER_NOT_FOUND` | 포스트 싱크 미러 훅에서 최신 성공 상태 마커를 찾지 못함 | `sync-on-login.ps1`이 정상적으로 `SUCCESS` 상태를 기록했는지 `last-sync.json` 파일 확인. |
| `CONNECTION_FAILED` | 로컬 API 서버(`127.0.0.1:8780`)에 연결할 수 없음 | 백엔드 서버가 종료되었는지 확인하고 수동 기동(`start.ps1` 또는 `backend\server.py`). |

### 12.2. 일일 운영 체크리스트

- [ ] **PC 부팅 및 카카오톡 로그인**: 카카오톡 PC 클라이언트가 정상 로그인되어 트레이에 상주하는지 확인합니다.
- [ ] **동기화 상태 마커 점검**:
  ```powershell
  Get-Content "$env:LOCALAPPDATA\KakaoCollector\state\last-sync.json" | ConvertFrom-Json | Select-Object status, stage, completed_at, messages_after
  ```
  - `status`가 `SUCCESS`인지 확인합니다.
- [ ] **일일 로그 누적 확인**:
  - `%LOCALAPPDATA%\KakaoCollector\logs\worker-YYYY-MM-DD.jsonl` 파일에 당일 성공 로그가 기록되었는지 확인합니다.
- [ ] **메시지 건수 증가 확인**:
  - `messages_after`가 `messages_before` 이상으로 정상 반영되었는지 확인합니다.
- [ ] **미러링 전송 상태 점검**:
  - `.\post-sync-mirror.ps1 -Status`를 실행하여 STOP 마커가 비활성 상태이고 최신 시도가 정상 완료되었는지 확인합니다.
