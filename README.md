# kakao-cli-windows

Windows KakaoTalk PC 로컬 DB 복호화/수집 도구와 React 웹 UI, 그리고 로컬 API 서버입니다.

이 프로젝트는 사용자의 PC 안에서만 동작합니다. KakaoTalk 데이터를 외부 서버로 업로드하지 않고, 복구된 키와 복호화 결과도 프로젝트 내부 `data/` 폴더에만 저장합니다.

## 무엇을 할 수 있나

```text
KakaoTalk PC 로그인
→ 실행 중인 KakaoTalk 메모리에서 DB 키 복구
→ 로컬 chatLogs DB 복호화
→ messages_v2.sqlite로 통합
→ 대화방/유저/멤버 매핑
→ 웹 UI에서 대화방 확인
→ 로컬 API로 대화방/기간별 메시지 조회
→ LLM 요약/분류/자동화에 활용
```

즉, “본인 PC의 KakaoTalk 대화 데이터를 로컬 API처럼 쓰는 도구”입니다.

## 처음 실행

공유받은 사람은 아래 순서대로 실행합니다.

```powershell
cd kakao-cli-windows-share
cd web
npm.cmd install
npm.cmd run build
cd ..
python backend\server.py --port 8780
```

브라우저에서 엽니다.

```text
http://127.0.0.1:8780/
```

PowerShell에서 `npm` 실행이 막히면 `npm.cmd`를 사용하세요.

## 처음 사용자 흐름

처음에는 아직 `v2_keys.json`과 `messages_v2.sqlite`가 없으므로 대화방 목록이 비어 있는 것이 정상입니다.

1. KakaoTalk PC에 로그인합니다.
2. 수집하고 싶은 대화방 몇 개를 직접 열어둡니다.
3. 웹에서 `키 찾기 + 최신화` 버튼을 누릅니다.
4. 성공하면 대화방 목록이 표시됩니다.

`키 찾기 + 최신화`는 내부적으로 아래 명령을 순서대로 실행합니다.

```powershell
python -m kwin v2recover
python -m kwin v2sync
```

성공하면 아래 파일이 생성됩니다.

```text
data\output\v2_keys.json
data\output\<user-folder>\messages_v2.sqlite
```

## 버튼 구분

### 키 찾기 + 최신화

처음 실행하거나 새 대화방의 DB 키가 없을 때 사용합니다.

```powershell
python -m kwin v2recover
python -m kwin v2sync
```

### 대화 최신화

이미 복구된 키가 있을 때 빠르게 다시 수집합니다.

```powershell
python -m kwin v2sync
```

키가 없는 완전 초기 상태에서는 `대화 최신화`만으로는 실패할 수 있습니다. 그럴 때는 `키 찾기 + 최신화`를 먼저 사용하세요.

## 로컬 API 빠른 시작

기본 주소:

```text
http://127.0.0.1:8780
```

상태 확인:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/status"
```

처음 키 복구 + 복호화 + 수집:

```powershell
Invoke-RestMethod -Method Post "http://127.0.0.1:8780/api/recover-sync"
```

대화방 목록:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/rooms?limit=50"
```

대화방 검색:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/rooms?q=업무&limit=20"
```

특정 방 최근 메시지:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/rooms/123456789/messages?limit=100"
```

날짜별 메시지:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/rooms/123456789/messages?date=2026-07-26&limit=500"
```

기간별 메시지:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/rooms/123456789/messages?from=2026-07-20&to=2026-07-26&limit=1000"
```

시간 범위:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/rooms/123456789/messages?from=2026-07-26T09:00&to=2026-07-26T18:00"
```

대화방 멤버:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/rooms/123456789/members?limit=1000"
```

유저 검색:

```powershell
Invoke-RestMethod "http://127.0.0.1:8780/api/users?q=홍길동&limit=20"
```

CSV export:

```powershell
Invoke-WebRequest "http://127.0.0.1:8780/api/rooms/123456789/export?format=csv&date=2026-07-26" -OutFile messages.csv
```

PowerShell 환경에서 `Invoke-WebRequest`가 이상하게 동작하면 `curl.exe`를 사용해도 됩니다.

```powershell
curl.exe -L "http://127.0.0.1:8780/api/rooms/123456789/export?format=csv&date=2026-07-26" -o messages.csv
```

## API 목록

| Method | Path | 기능 |
|---|---|---|
| `GET` | `/api/status` | 서버/키/DB/메시지 수 상태 확인 |
| `POST` | `/api/recover` | KakaoTalk 메모리에서 SQLCipher raw key 복구 |
| `POST` | `/api/sync` | 기존 키로 DB 복호화 + 통합 수집 |
| `POST` | `/api/recover-sync` | 키 복구 후 복호화 + 통합 수집 |
| `GET` | `/api/rooms` | 대화방 목록/검색 |
| `GET` | `/api/rooms/{chatId}/messages` | 특정 방 메시지 조회 |
| `GET` | `/api/rooms/{chatId}/members` | 특정 방 멤버 조회 |
| `GET` | `/api/rooms/{chatId}/export` | 특정 방 메시지 JSON/CSV export |
| `GET` | `/api/users` | 유저/연락처 검색 |
| `GET` | `/api/jobs` | 저장된 자동화 작업 조회 |
| `POST` | `/api/jobs` | 자동화 작업 저장 |
| `DELETE` | `/api/jobs?id=...` | 자동화 작업 삭제 |
| `GET` | `/api/style` | 요약 스타일 조회 |
| `POST` | `/api/style` | 요약 스타일 저장 |

## LLM/자동화에서 쓰는 흐름

로컬 LLM, Codex, Python 스크립트, n8n, Dify 같은 도구에서는 다음처럼 쓰면 됩니다.

```text
1. GET /api/status
   ready 여부 확인

2. ready=false이면 POST /api/recover-sync
   사용자가 KakaoTalk PC에서 필요한 방을 열어둔 뒤 실행

3. GET /api/rooms?q=검색어
   사용자가 원하는 대화방을 선택

4. GET /api/rooms/{chatId}/messages?from=...&to=...
   필요한 기간의 메시지만 조회

5. LLM이 messages 배열을 요약/분류/액션아이템 추출
```

Python 예시:

```python
import requests

base = "http://127.0.0.1:8780"

status = requests.get(f"{base}/api/status").json()
if not status["ready"]:
    requests.post(f"{base}/api/recover-sync").raise_for_status()

rooms = requests.get(f"{base}/api/rooms", params={"q": "업무", "limit": 10}).json()["rooms"]
room = rooms[0]

messages = requests.get(
    f"{base}/api/rooms/{room['chatId']}/messages",
    params={"from": "2026-07-20", "to": "2026-07-26", "limit": 1000},
).json()["messages"]

print(room["title"], len(messages))
```

## 프로젝트 구조

```text
kakao-cli-windows-share/
  kwin/                  # CLI, 메모리 키 복구, 복호화, 수집 로직
  backend/server.py      # localhost API 서버 + React 빌드 서빙
  web/                   # React + Vite UI
  data/
    output/              # v2_keys.json, 복호화 DB, messages_v2.sqlite
    config/              # automation_jobs.json, summary_style.json
    backup/              # reset-first-run.ps1 백업 결과
  reset-first-run.ps1
```

## 처음 상태로 테스트하기

현재 복구된 키와 결과물을 백업한 뒤, 처음 설치한 사람처럼 다시 테스트하려면:

```powershell
cd kakao-cli-windows-share
.\reset-first-run.ps1
python backend\server.py --port 8780
```

스크립트는 삭제하지 않고 아래 위치로 이동합니다.

```text
data\backup\first-run-YYYYMMDD-HHMMSS\
```

자동화 설정은 유지하고 DB 키/복호화 결과만 초기화하려면:

```powershell
.\reset-first-run.ps1 -KeepConfig
```

## CLI 직접 실행

```powershell
python -m kwin v2recover
python -m kwin v2decrypt
python -m kwin v2collect
python -m kwin v2sync
```

## 중요한 동작

- SQLCipher raw key는 같은 PC, 같은 계정, 같은 로컬 DB 파일 기준으로 대체로 유지됩니다.
- 로그아웃 후 같은 계정으로 다시 로그인한다고 해서 기존 DB 키가 항상 바뀌는 것은 아닙니다.
- 새 DB 파일이나 아직 메모리에 로드되지 않은 대화방은 `v2recover`를 다시 실행해야 할 수 있습니다.
- KakaoTalk에서 해당 대화방을 열어둔 상태일수록 키 후보를 찾을 가능성이 높습니다.
- API의 날짜/시간 파라미터는 KST 기준입니다.

## 오픈채팅 멤버 이름 관련

오픈채팅 멤버 이름은 KakaoTalk PC가 로컬 메타데이터에 캐시한 경우에만 매핑됩니다. `chatMembers`에는 있는데 `TalkUserDB.sqlite` 등에 닉네임이 없으면 오프라인 DB 리더가 임의로 이름을 만들어낼 수 없습니다.

## GitHub 공유/커밋 전 주의

커밋하면 안 되는 파일:

```text
data/
v2_keys.json
memory_inspection.txt
userid_result.txt
*.edb
*.sqlite
*.sqlite-*
node_modules/
web/dist/
```

공유하거나 커밋할 때는 `kakao-cli-windows-share` 폴더에서 개인 데이터와 빌드 산출물을 제거한 clean 상태로 만들어야 합니다.

`.gitignore`에 기본 반영되어 있습니다.

## 이용 안내

이 프로젝트는 Kakao와 관련이 없는 비공식 도구입니다. 본인이 소유하거나
접근 권한이 있는 데이터에만 사용해야 합니다. 사용자는 관련 법률과 서비스
약관을 준수할 책임이 있으며, 프로젝트 작성자는 사용 과정에서 발생하는
데이터 손실, 계정 문제 또는 기타 손해에 책임을 지지 않습니다.

## 라이선스

이 프로젝트는 [MIT License](LICENSE)로 배포됩니다.
