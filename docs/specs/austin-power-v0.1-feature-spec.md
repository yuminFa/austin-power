---
topic: austin-power v0.1 — 가벼운 한국어 친화 MCP 기억 저장소
date: 2026-09-23
repos_touched: [austin-power]
status: draft
---

# austin-power v0.1 — 기능명세

> 상위 입력: `agent-memory-design.md`(2026-09-23 설계 초안, 작성자 로컬 문서) + 2026-09-23 설계 대화의 결정 사항
> 작성일: 2026-09-23 · 개정: self-review 2라운드 반영(미수렴 — 상한 도달, 보정가능 지적 전부 반영)

---

## 1. 목표 · 범위

- **무엇을**: 코딩 에이전트(Claude Code·Codex 등 MCP 클라이언트)가 세션을 넘어 쓰는 **기억 저장소**. 로컬 머신에 상주하는 HTTP MCP 서버 **하나**가 SQLite 파일 하나를 관리하고, 도구 5개(`save`·`search`·`get`·`recent`·`forget`)를 제공한다. 한국어 본문을 형태소 단위로 검색한다. 여기에 더해 (a) Claude Code **PostCompact hook**으로 압축 요약을 세션 기억으로 자동 저장(증류), (b) **SessionStart hook**으로 프로젝트 최근 기억을 주입, (c) JSONL **import** 명령, (d) agentmemory 변환 스크립트(`contrib/`)를 제공한다.
- **왜**: 기존 도구(agentmemory)를 실사용하며 관측한 문제 — 1MB 데이터에 상주 550MB, 프로세스 4계층, BM25 인덱스 세대 누적(654세대·6.8GB), 부분 종료 불가, 한국어 조사 때문에 검색이 빗나감. 이를 **FTS5 증분 인덱스 + 단일 프로세스 + 한국어 형태소 토크나이저**로 해결한다.
- **오픈소스 전제**: public repo(`github.com/yuminFa/austin-power`). 작성자 개인 환경(경로·계정·사내 정보)을 코드·문서·예시에 박지 않는다. 모든 경로·포트는 기본값 + 환경변수로 해석한다.
- **범위 밖 (v0.1에서 하지 않음)**:
  - SessionEnd 증류(기본 타임아웃 1.5초·최대 60초, 서버는 LLM을 부르지 않으므로 요약을 만들 수단이 없음) · UserPromptSubmit 자동 검색 주입
  - 코어 CLI 안의 agentmemory 전용 로직 — 변환기는 `contrib/` 독립 스크립트로만 둔다
  - 지식 그래프, 벡터 임베딩, 웹 뷰어, 서버 측 LLM 호출, 관측 자동 수집, 팀 동기화/클라우드 — 원 설계 §5 유지
  - 원격 접속(loopback 외 바인딩), OAuth
  - 서비스 자동 설치(launchd/systemd 파일을 시스템에 써 넣는 명령) — 예시 파일만 제공

### 1.1 원 설계 대비 변경점 (의도적)

| 원 설계 | v0.1 | 이유 |
|---|---|---|
| 상주 데몬 없음, 세션마다 stdio 서버 | **전역 HTTP 서버 1개**(상주) | 사용자 결정. Kiwi 모델(로딩 1.61s 실측)을 세션마다 올리지 않고 한 번만 로드. 대가: 상주 메모리 0이 아니게 됨(Kiwi 모델 상주 — 예산은 §4 U8) |
| TypeScript + better-sqlite3 | **Python + apsw** | Kiwi(`kiwipiepy`) 사용, apsw wheel이 SQLite를 번들해 사용자 환경의 SQLite 버전과 무관해짐, FTS5 Python 토크나이저 등록 가능 |
| `tokenize='unicode61'` | **Kiwi 기반 커스텀 토크나이저 `kiwi`** | 한국어 조사·어미 분리 |
| `id TEXT PRIMARY KEY` + `content_rowid='rowid'` | **`id INTEGER PRIMARY KEY`** | 명시적 INTEGER PRIMARY KEY가 없는 테이블은 `VACUUM`이 rowid를 재번호할 수 있어 external-content FTS가 어긋난다(SQLite `VACUUM` 문서) |
| 도구 4개 | **5개(`get` 추가)** | `search`는 발췌만 돌려주므로 전문을 읽을 경로가 필요 |
| `kind` 고정 5종 | **자유 슬러그** | 사용자마다 분류 체계가 다름(이관 대상 350건은 `architecture`·`workflow`·`fact`·`bug`·`preference`·`pattern` — jq 실측) |
| 증류는 에이전트가 자발적으로 | **+ PostCompact hook 자동 저장** | Claude Code가 압축 시 만든 요약(`compact_summary`)을 저장 → 추가 LLM 호출 0, 서버 LLM 금지 원칙 유지. 요구는 "압축 시점(PreCompact)에 증류"였으나 PreCompact 입력에는 요약이 없고(`transcript_path`·`trigger`·`custom_instructions`뿐) 컨텍스트 주입도 불가 → 같은 압축 시점의 **직후** 이벤트인 PostCompact로 충족 |
| 주입 상한 2,000 토큰 | **4,000자**(환경변수로 조정) | 토큰 수는 모델·언어별로 달라 서버가 셀 수 없음. 한국어·영어 혼합 기준 대략 2,000 토큰 수준 `[inferred]` |
| "DB는 sqlite3로 열면 됨" | **읽기는 누구나, 쓰기는 `kiwi`를 등록한 연결만** | `note`에 걸린 트리거가 `kiwi` 토크나이저를 요구. 쓰기 주체는 셋(서버·`import`·post-compact 폴백)이고 모두 같은 열기 루틴(2.4 `open_db`)을 거친다 |

## 2. 동작 명세

### 2.1 설정 해석 (`config`)

**홈 디렉터리** — 우선순위 순:
1. `AUSTIN_POWER_HOME` 환경변수
2. `XDG_CONFIG_HOME`이 있으면 `$XDG_CONFIG_HOME/austin-power`
3. Windows: `%APPDATA%\austin-power`
4. 그 외: `~/.config/austin-power`

- 결과는 항상 **절대경로**(`expanduser().resolve()`). 원 설계 §7의 "잘못된 cwd에서 빈 저장소가 생긴 사고" 방지를 위해 **상대경로는 cwd가 아니라 기준 디렉터리에 붙여** 해석한다: `AUSTIN_POWER_HOME`·`XDG_CONFIG_HOME`의 상대값은 사용자 홈(`~`) 기준, `AUSTIN_POWER_DB`의 상대값은 austin-power 홈 기준. 그래서 어느 디렉터리에서 실행해도 같은 파일을 가리킨다.
- 홈을 **만드는** 명령: `serve`·`token`·`setup *`·`import`·`hook post-compact`(폴백 시). 권한 `0700`(POSIX). **만들지 않는** 명령: `status`·`hook session-start`.

| 항목 | 기본값 | 환경변수 | CLI 옵션 |
|---|---|---|---|
| DB 파일 | `<home>/memory.db` | `AUSTIN_POWER_DB` | — |
| 토큰 파일 | `<home>/token` | — | — |
| 서버 잠금 파일 | `<home>/server.lock` | — | — |
| 호스트 | `127.0.0.1` | `AUSTIN_POWER_HOST` | `--host` (`serve`·`status`·`setup *`) |
| 포트 | `7760` | `AUSTIN_POWER_PORT` | `--port` (`serve`·`status`·`setup *`) |
| 주입 상한(자) | `4000` | `AUSTIN_POWER_INJECT_CHARS` | — |
| 로그 레벨 | `INFO` | `AUSTIN_POWER_LOG_LEVEL` | — |

- 우선순위: CLI 옵션 > 환경변수 > 기본값. 설정 파일(toml)은 v0.1에 두지 않는다.
- hook·클라이언트가 접속하는 URL은 `http://<host>:<port>/mcp`이고 `host`가 `::1`이면 `http://[::1]:<port>/mcp`로 쓴다.
- 케이스:
  - 포트가 정수가 아니거나 1~65535 밖 → exit 2 + 메시지(hook 명령은 경고 후 exit 0).
  - 호스트가 loopback(`127.0.0.1`·`::1`·`localhost`)이 아님 → `serve`·`setup`이 exit 2로 거부("v0.1은 로컬 전용").

### 2.2 토큰 (`auth`)

- 형식: `secrets.token_urlsafe(32)` (43자).
- **생성**(토큰 파일이 없을 때 `serve`·`token`·`setup *`): 같은 디렉터리에 임시 파일을 `os.open(tmp, O_CREAT|O_EXCL|O_WRONLY, 0o600)`으로 만들어 토큰을 쓰고 fsync → `os.link(tmp, token)`으로 게시(대상이 있으면 `FileExistsError` → 기존 파일 채택) → 임시 파일 삭제. **게시된 토큰 파일은 항상 완성된 내용**이라 동시 생성 경쟁에서 빈 파일을 읽는 일이 없다. `os.link`가 지원되지 않는 파일시스템(`OSError`이면서 `FileExistsError`가 아님)이면 `os.replace(tmp, token)`으로 게시한다 — 완성된 내용만 보이는 성질은 유지되고, 동시에 두 프로세스가 만들면 마지막 것이 남는다(드문 경우, 다음 읽기부터 일관).
- **읽기**: 앞뒤 공백 제거. 빈 파일 → 오류("토큰 파일이 비어 있음 — `austin-power token --rotate`"). CLI는 exit 1, hook은 2.8 경로 결정표의 "토큰 파일 없음"과 같이 취급.
- 권한 검사: POSIX에서 그룹/기타 권한 비트가 있으면 경고 로그(거부하지 않음).
- **회전** `token --rotate`: 임시 파일 + `os.replace`로 원자 교체. 실행 중 서버는 기동 시 읽은 토큰을 쓰므로 **서버 재시작 필요**, 클라이언트 재등록 필요 — 명령이 두 가지를 안내.
- **검증**: `hmac.compare_digest`. 헤더 스킴 `Bearer`는 대소문자 무시(RFC 7235), 토큰은 정확 일치.

### 2.3 HTTP 서버 (`serve`)

- 구성: `mcp` 2.x `MCPServer` → `streamable_http_app(stateless_http=True, json_response=True, transport_security=TransportSecuritySettings(allowed_hosts=[...], allowed_origins=[]))` → 자체 bearer 미들웨어로 감싼 ASGI 앱 → `uvicorn` 단일 프로세스·단일 워커. `[code-verified: mcp==2.2.0#mcp.server.mcpserver.MCPServer.streamable_http_app]`
- `allowed_hosts`: `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`.
- **stateless 선택 이유**: 전역 서버가 여러 세션을 받는다. stateful 세션은 클라이언트가 끊겨도 idle timeout(기본 1800s)까지 남아 메모리를 늘린다 — 이 프로젝트가 없애려는 문제.
- 엔드포인트:

| 경로 | 메서드 | 인증 | 동작 |
|---|---|---|---|
| `/mcp` | POST (그 외 메서드는 SDK 처리) | Bearer 필수 | MCP streamable HTTP |
| `/health` | GET | 없음 | `200 {"status":"ok","name":"austin-power","version":"<ver>"}` — 개수·경로 등 내부 정보 없음 |

- 인증 실패(헤더 없음·스킴 틀림·토큰 불일치) → `401 {"error":"unauthorized"}`, `WWW-Authenticate` 헤더 없음.
- Host 불일치 → `421`, Origin 헤더가 허용 목록 밖 → `403` (SDK `TransportSecuritySettings` 동작, evaluator가 소스로 확인). 순서: 미들웨어 바깥쪽이 bearer 검사이므로 **토큰 없는 요청은 Host와 무관하게 401**.
- **기동 순서**: 설정 해석 → 홈·토큰 준비 → **서버 잠금 획득**(아래) → `open_db()`(2.4 — Kiwi 로드·토크나이저 등록·마이그레이션·서명 확인·필요 시 재색인) → 포트 바인딩 → 준비 로그 1줄 `listening on http://127.0.0.1:7760/mcp`.
- **단일 인스턴스**: `<home>/server.lock`을 열어 POSIX `fcntl.flock(LOCK_EX|LOCK_NB)`, Windows `msvcrt.locking`으로 프로세스 수명 동안 잡는다. 실패 → exit 1 "이미 실행 중인 austin-power 서버가 있음(같은 홈)". 포트가 달라도 같은 홈이면 두 번째 서버는 뜨지 않는다. 잠금은 프로세스가 죽으면 OS가 푼다(고아 잠금 없음).
- 케이스:
  - 포트 사용 중(다른 프로그램) → exit 1 "포트 <p> 사용 중".
  - DB 열기 실패(권한·손상·상위 버전) → exit 1 + 경로 포함 메시지.
  - `SIGINT`/`SIGTERM` → uvicorn graceful shutdown → DB 연결 close.
  - 요청 본문 상한: SDK 기본 4 MiB.
  - 클라이언트가 응답 전에 끊김: 이미 시작된 DB 작업은 완료·커밋된다(작업은 트랜잭션 단위라 부분 적용 없음).

### 2.4 저장소 (`db`)

**`open_db(path, *, create=True)`** — 쓰기 연결을 여는 **유일한** 루틴. 서버·`import`·post-compact 폴백이 모두 이것을 쓴다.
1. Kiwi 로드(프로세스당 1회, 모듈 전역 캐시).
2. `create=True`이고 DB 파일의 상위 디렉터리가 없으면 생성(0700). `create=False`이고 파일이 없으면 `FileNotFoundError`.
3. `apsw.Connection(path)` → `register_fts5_tokenizer("kiwi", ...)`.
4. PRAGMA: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=<호출자 지정, 기본 5000>`.
5. **마이그레이션**: `BEGIN IMMEDIATE` → `user_version` 재확인 → 0이면 스키마 생성 + `meta.tokenizer_sig`에 현재 서명 기록 + `user_version=1` → `COMMIT`(새 DB는 재색인 없음). 두 프로세스가 동시에 열어도 쓰기 잠금 때문에 한쪽만 생성하고 다른 쪽은 재확인에서 1을 본다. `user_version > 1` → `SchemaTooNewError`(서버·import는 exit 1, 폴백은 경고).
6. **토크나이저 서명**(기존 DB만): 현재 서명 `kiwi/1/kiwipiepy-<importlib.metadata.version("kiwipiepy")>/model-<importlib.metadata.version("kiwipiepy_model")>`와 `meta.tokenizer_sig` 비교 → 다르면:
   - **재색인 권한이 있는 경우**에만 `BEGIN IMMEDIATE` 안에서 `INSERT INTO note_fts(note_fts) VALUES('rebuild')` + 서명 갱신 → `COMMIT`, 로그 `reindexed N notes: <old> -> <new>`. 권한 = 서버 잠금(`server.lock`)을 이 프로세스가 쥐고 있음. 서버는 기동 시 이미 쥐고 있고, `import`·폴백은 비차단으로 잠금을 **시도**해 얻으면(=서버 미실행) 재색인 후 작업이 끝날 때 푼다.
   - 권한이 없으면(서버가 실행 중인데 서명이 다름) `TokenizerMismatchError` — `import` exit 1 "서버와 토크나이저 버전이 다름 — 서버를 재시작하세요", 폴백은 경고.
7. **쓰기 트랜잭션마다 서명 재확인**: 모든 쓰기(`save`·`forget`·import 배치·폴백)는 `BEGIN IMMEDIATE` 직후 `SELECT value FROM meta WHERE key='tokenizer_sig'`를 읽어 자기 서명과 다르면 롤백 + `TokenizerMismatchError`(서버 도구 응답은 `storage error: TokenizerMismatchError`, 로그에 "재시작 필요"). 실행 중인 서버 아래에서 패키지가 업그레이드돼 다른 버전 프로세스가 재색인한 경우에도 옛 규칙으로 쓰는 일이 없다. 재색인은 DB 안 제자리 갱신이고 별도 파일·세대를 만들지 않는다.

**읽기 전용 연결** `open_db_readonly(path)`: `SQLITE_OPEN_READONLY`, 토크나이저 등록·마이그레이션·서명 확인 없음. `note` 조회 전용(`import --dry-run`이 사용). 파일이 없으면 `None`.

**모듈 경계**: upsert·삭제·조회 로직은 `austin_power.store`에 apsw만 의존하는 순수 함수로 둔다. MCP 도구 계층(`server`)과 폴백(`hook`)·`import`가 같은 함수를 호출한다 — 폴백 경로가 `mcp`·`uvicorn`을 import하지 않게.

**서버 안의 동시성**: 서버 프로세스는 연결 1개. 모든 DB 작업은 `threading.Lock` 하나로 직렬화하고 비동기 핸들러에서 `anyio.to_thread.run_sync`로 실행한다(apsw 연결 동시 사용 금지 준수, 이벤트 루프 비블로킹).

**프로세스 사이의 동시성**: SQLite WAL + `busy_timeout`에 맡긴다. 서버 5,000ms / import 5,000ms / 폴백 20,000ms(hook 타임아웃 30s 안). 타임아웃 초과(`BusyError`)는 2.6 오류 규칙대로 처리.

**스키마 (user_version=1)**:

```sql
CREATE TABLE note (
  id          INTEGER PRIMARY KEY,
  project     TEXT    NOT NULL DEFAULT '',
  kind        TEXT    NOT NULL,
  title       TEXT    NOT NULL,
  body        TEXT    NOT NULL,
  session_id  TEXT,
  created_at  INTEGER NOT NULL,   -- unix epoch seconds (UTC)
  updated_at  INTEGER NOT NULL,
  UNIQUE (project, title)
) STRICT;

CREATE INDEX note_project_updated ON note(project, updated_at DESC);

CREATE VIRTUAL TABLE note_fts USING fts5(
  title, body,
  content='note', content_rowid='id',
  tokenize='kiwi'
);

CREATE TRIGGER note_ai AFTER INSERT ON note BEGIN
  INSERT INTO note_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER note_ad AFTER DELETE ON note BEGIN
  INSERT INTO note_fts(note_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER note_au AFTER UPDATE ON note BEGIN
  INSERT INTO note_fts(note_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO note_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
```

- `project=''`는 "프로젝트 미지정". NULL은 UNIQUE에서 서로 다른 값이 되므로 쓰지 않는다.
- 외부 연결(`sqlite3` CLI 등, `kiwi` 미등록): `note` SELECT·`.backup`은 동작, `note` INSERT/UPDATE/DELETE와 `note_fts` 조회는 `no such tokenizer` 오류 — README에 명시.

### 2.5 토크나이저 `kiwi` (`tokenizer`)

`apsw.fts5.StringTokenizer`로 등록(문자 오프셋으로 yield → apsw가 UTF-8 바이트 오프셋으로 변환). 호출 인자 `flags`로 색인(`FTS5_TOKENIZE_DOCUMENT`)과 질의(`FTS5_TOKENIZE_QUERY`)를 구분한다. `[code-verified: apsw==3.53.4.0#apsw.fts5.StringTokenizer]` `[code-verified: apsw==3.53.4.0#apsw.FTS5_TOKENIZE_DOCUMENT]`

**규칙 (규칙버전 1)**:
1. 입력에서 **ASCII 런**(정규식 `[A-Za-z0-9_]+(?:[./-][A-Za-z0-9_]+)*`)의 위치를 찾는다.
2. 각 ASCII 런 → 원형을 **소문자로** 토큰 1개(`note_fts`, `better-sqlite3`, `v0.1`, `fts5`).
3. **색인 시에만**: 런이 구분자(`_` `.` `/` `-`)를 포함하면 구성 조각(소문자)을 **colocated 토큰**으로 추가(`note_fts` → `note_fts` + `note` + `fts`, 같은 위치). 질의 시에는 조각을 만들지 않는다 — 질의의 colocated 토큰은 "같은 위치의 대안"으로 해석돼 `note_fts` 질의가 `note`만 있는 문서까지 잡기 때문(reviewer 실측).
4. 입력에 비ASCII 문자가 있으면 **입력 전체를 Kiwi `tokenize()`에 한 번** 넣는다(조사를 떼어 단독으로 넣으면 문맥이 사라져 `를`→`르/NNG`, `가`→`가/VV`로 오분류됨 — 실측. 문장째 넣으면 `apsw가`→`apsw/SL`+`가/JKS`로 정확). 결과 형태소 중 **ASCII 런과 위치가 겹치는 것은 버리고**(그 자리는 규칙 2·3 토큰이 대신함), 나머지 중 태그가 `NN*`(명사)·`VV*`·`VA*`(동사·형용사, `form`=어간)·`XR`(어근)·`SH`(한자)·`SL`(외국어)·`SN`(숫자)인 형태소의 `form`을 소문자로 토큰화. 조사(`J*`)·어미(`E*`)·선어말어미(`EP`)·접사(`XP*`/`XS*`)·기호는 버린다. 비ASCII 문자가 없으면 Kiwi를 부르지 않는다.
5. 토큰은 **입력 위치 순서**로 낸다(ASCII 런 토큰과 Kiwi 토큰을 시작 위치 기준으로 병합).
6. 빈 문자열·공백만 → 토큰 0개(오류 아님).
7. 결과 예: `note_fts를 만들었다` → 색인 `[note_fts(+note,fts), 만들]` / 질의 `note_fts가` → `[note_fts]` → 매칭. `을 를` → 토큰 0개.

**질의 효과**: FTS5가 MATCH 문자열의 구절에 같은 토크나이저(질의 모드)를 적용 → `만들었습니다`로 찾으면 어간 `만들`이 매칭(probe 실측: `만들다` → `[만들]었습니다` 하이라이트).

**오류**: Kiwi가 예외를 던지면 FTS5 호출이 SQLite 오류로 실패 → 해당 쓰기·검색 트랜잭션 롤백 → 2.6 오류 규칙.

### 2.6 MCP 도구

**공통**
- 반환은 JSON 객체(구조화 결과). 시간은 ISO 8601 UTC 초 단위 문자열(`2026-09-23T03:12:45Z`).
- 문자열 입력은 앞뒤 공백 제거 후 검증.
- `kind`: 소문자화 후 정규식 `^[a-z0-9][a-z0-9_-]{0,31}$`. 도구 설명에 권장값(`fact`·`decision`·`pattern`·`gotcha`·`workflow`·`session`)을 적되 강제하지 않는다.
- `project`: 0~100자. 대소문자 보존, 비교는 정확 일치.
- **오류 규칙** (도구 오류 = `isError: true` + 메시지. 스택·본문·토큰은 싣지 않는다):

| 원인 | 메시지 | 부수효과 |
|---|---|---|
| 입력 검증 실패 | `invalid <field>: <이유>` | 없음 |
| 대상 없음(`get`) | `note <id> not found` | 없음 |
| `BusyError`(잠금 대기 초과) | `storage busy, retry later` | 롤백, 부분 적용 없음 |
| 그 외 SQLite 오류(디스크 가득 참·I/O·토크나이저 예외) | `storage error: <예외 클래스명>` | 롤백. 서버 로그에 ERROR + 스택 |

#### `save`

| 입력 | 타입 | 필수 | 규칙 |
|---|---|---|---|
| `title` | str | ✅ | 1~200자 |
| `body` | str | ✅ | 1~32,000자 |
| `project` | str | — | 공통 규칙, 기본 `''` |
| `kind` | str | — | 공통 규칙. **신규 행에서만** 기본 `fact` |
| `session_id` | str | — | 0~200자 |

- 동작(락 안, 한 트랜잭션 `BEGIN IMMEDIATE`): `SELECT id FROM note WHERE project=? AND title=?` →
  - 없음 → INSERT(`created_at = updated_at = now`) → `action: "created"`.
  - 있음 → UPDATE: `body` 교체, `updated_at = now`, `kind`·`session_id`는 **입력이 주어졌을 때만** 교체(생략하면 기존 값 유지), `id`·`created_at` 유지 → `action: "updated"`.
- 반환: `{"id": 12, "action": "created" | "updated"}`. 판정은 존재 조회로 하므로 같은 초 안의 재저장도 `updated`.
- 케이스: 빈 title/body·상한 초과·잘못된 kind → 검증 오류 / 동일 내용 재저장 → `updated`, 행 1개 유지.

#### `search`

| 입력 | 타입 | 필수 | 규칙 |
|---|---|---|---|
| `query` | str | ✅ | 1~500자 |
| `project` | str | — | 지정 시 정확 일치만 |
| `kind` | str | — | 공통 규칙 |
| `limit` | int | — | 1~50, 기본 10 |

- **질의 조립**: `query`를 공백으로 나눈 조각마다 토크나이저(질의 모드)를 직접 돌려 **토큰 0개인 조각(조사만·기호만 등)을 제거**. 남은 조각이 없으면 즉시 `{"match":"none","results":[]}`. 남은 조각을 `apsw.fts5query.quote()`로 인용(FTS5 문법 문자 무력화) → 공백으로 이어 **AND 질의**. 0건이고 조각이 2개 이상이면 `OR`로 이어 **1회 재시도**. `[code-verified: apsw==3.53.4.0#apsw.fts5query.quote]` — 토큰 0개 구절이 AND 전체를 0건으로 만든다는 사실은 reviewer probe로 관측(그래서 사전 제거).
- 정렬: `bm25(note_fts, 3.0, 1.0)` 오름차순(제목 가중 3배), 동점이면 `updated_at` 내림차순.
- `excerpt`: `snippet(note_fts, -1, '«', '»', '…', 24)` — 열 인자 `-1`은 매칭이 가장 좋은 열을 FTS5가 고른다. 제목만 맞으면 제목이 하이라이트되어 나온다.
- 반환: `{"match": "all" | "any" | "none", "results": [{"id","project","kind","title","excerpt","updated_at"}]}`.
- 케이스: 매칭 0건 → `none` / 특수문자만(`"***"`) → 조각 제거로 `none`(문법 오류 없음) / 없는 `project` → 빈 결과 / 조사만(`"을 를"`) → `none`.

#### `get`

- 입력 `id`(int ≥ 1, 필수). 반환 `{"id","project","kind","title","body","session_id","created_at","updated_at"}`.
- 케이스: 없는 id → `note <id> not found` / 0 이하 → 검증 오류.

#### `recent`

- 입력 `project`(선택)·`kind`(선택)·`limit`(1~50, 기본 10). 정렬 `updated_at DESC, id DESC`.
- 반환 `{"results":[{"id","project","kind","title","preview","updated_at"}]}`, `preview` = 본문 앞 200자(잘렸으면 끝에 `…`).
- 케이스: 빈 저장소·필터 결과 0 → 빈 배열.

#### `forget`

- 입력 `id`(int ≥ 1, 필수). 반환 `{"id": 12, "deleted": true | false}` — 없는 id는 `deleted: false`(멱등).

### 2.7 CLI (`austin-power`)

`argparse`, 진입점 `austin_power.cli:main`, `python -m austin_power` 동일.

| 명령 | 동작 | exit |
|---|---|---|
| `serve [--host] [--port]` | 2.3 서버 전경 실행 | 0 정상 종료 / 1 기동 실패 / 2 잘못된 인자 |
| `status [--host] [--port]` | `GET /health` 타임아웃 2s → `running: http://<host>:<port> (v<ver>)` 또는 `not running` | 0 / 1 |
| `token [--rotate]` | 토큰 출력(없으면 생성), 회전(2.2) | 0 / 1 |
| `setup claude [--host] [--port]` | **출력만**: `claude mcp add --transport http --scope user austin-power http://<host>:<port>/mcp --header "Authorization: Bearer <token>"` | 0 / 2 |
| `setup codex [--host] [--port]` | **출력만**: `codex mcp add austin-power --url http://<host>:<port>/mcp --bearer-token-env-var AUSTIN_POWER_TOKEN` + 셸 설정에 `export AUSTIN_POWER_TOKEN="$(austin-power token)"`을 넣으라는 안내. `[code-verified: codex-cli 0.153.4#codex mcp add --help]` | 0 / 2 |
| `setup hooks` | **출력만**: Claude Code `settings.json`의 `hooks.PostCompact`·`hooks.SessionStart` JSON 스니펫(2.8) | 0 |
| `import FILE [--dry-run]` | 2.9 | 0 / 1 / 2 (2.9) |
| `hook post-compact` · `hook session-start` | 2.8, stdin JSON | **항상 0** |
| `--version` | 버전 출력 | 0 |

- `setup`은 제3자 설정 파일을 **수정하지 않는다**. 토큰이 들어가는 출력에는 경고 한 줄을 붙인다.

### 2.8 Claude Code hooks (`hook`)

**공통**
- stdin JSON 파싱. 파싱 실패·필수 필드 없음 → stderr 한 줄 경고, **exit 0**(fail-open — hook 실패가 세션을 방해하지 않게).
- 서버 호출: 표준 라이브러리 `urllib`로 `POST /mcp` JSON-RPC `tools/call` 1회. stateless 서버라 `initialize` 없이 단독 요청이 처리된다(스크래치 probe에서 별도 요청으로 `tools/call` 200 관측). 헤더: `Authorization: Bearer <토큰>`, `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `MCP-Protocol-Version: 2025-06-18`. 타임아웃 5초.
- hook 경로는 `kiwipiepy`·`apsw`·`mcp`를 import하지 않는다(기동 지연 방지). 예외: post-compact 폴백.
- **project 해석**: `AUSTIN_POWER_PROJECT` 환경변수 > `git -C <cwd> rev-parse --show-toplevel`(타임아웃 2초)의 디렉터리 이름 > `cwd` 디렉터리 이름 > `''`.

**`hook post-compact`** — `PostCompact` 이벤트, 입력 `session_id`·`cwd`·`trigger`·`compact_summary`. `[code-verified: claude-code 2.1.280 binary#hook_event_name:"PostCompact"]`
- `compact_summary`가 비었거나 없음 → 아무것도 하지 않음.
- 저장 내용: `title = "session " + session_id[:180]`(세션당 고정 → 같은 세션 재압축은 **같은 행을 덮어씀**. 최신 요약이 이전 요약을 포함하므로 손실 없음), `body = compact_summary`(32,000자 초과 시 앞 31,000자 + `\n…(truncated)`), `kind = "session"`, `project` 공통 규칙, `session_id` 그대로(200자 초과 시 앞 200자).
- **경로 결정표**:

| 상황 | 동작 |
|---|---|
| 서버 호출 성공(`isError: false`) | 끝 |
| 연결 거부 또는 토큰 파일 없음 → `server.lock` 비차단 획득 **성공**(서버 미실행 확정) | **폴백**: `open_db(busy_timeout=20000)`로 직접 같은 upsert(`austin_power.store`), 끝나면 잠금 해제. 폴백은 `kiwipiepy`·`apsw`만 import |
| 연결 거부인데 `server.lock` 획득 **실패**(서버는 실행 중, 주소 설정 불일치) | 폴백하지 않음. 경고 `server running but unreachable at <url> — check AUSTIN_POWER_HOST/PORT` |
| 타임아웃(서버는 살아 있으나 느림) | 폴백하지 않음(이중 쓰기 방지), 경고 |
| HTTP 401/403/421/5xx, 또는 `isError: true` | 폴백하지 않음(토큰·설정 문제를 가리지 않음), 경고 |
| 폴백 실패(BusyError·스키마 상위 버전 등) | 경고 |

- 권장 설정: `"PostCompact": [{"hooks": [{"type": "command", "command": "austin-power hook post-compact", "timeout": 30}]}]`.

**`hook session-start`** — `SessionStart` 이벤트, 입력 `session_id`·`cwd`·`source`.
- `source == "compact"` → 출력 없음(압축 요약이 이미 컨텍스트에 있음).
- `project == ''` → 출력 없음(무관한 기억 주입 방지).
- 그 외: `recent(project=<해석>, limit=8)` → 결과가 있으면 stdout에 `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"<텍스트>"}}`. 텍스트: 헤더 `austin-power: recent memories for <project> (use the search/get tools for more)` + 항목별 `- [kind] title (#id, YYYY-MM-DD): preview`. 총 `AUSTIN_POWER_INJECT_CHARS`(기본 4,000자) 상한 — 넘치는 항목은 통째로 버린다.
- 결과 0건·서버 미실행·토큰 없음·오류·타임아웃 → 출력 없음. 이 hook은 **폴백하지 않는다**(DB·Kiwi 로드는 세션 시작을 늦춤).
- 권장 설정: `"SessionStart": [{"hooks": [{"type": "command", "command": "austin-power hook session-start", "timeout": 10}]}]`.

### 2.9 JSONL import (`import`)

- 입력: UTF-8 JSONL. 한 줄 = `{"title","body","project"?,"kind"?,"session_id"?,"created_at"?,"updated_at"?}`. 빈 줄은 건너뛴다. 파일이 없거나 읽을 수 없음 → exit 2.
- 행 단위 실패(해당 행만 건너뛰고 `line N: <이유>` 수집): UTF-8 디코딩 실패 / JSON 아님·객체 아님 / 2.6 `save` 검증 실패 / 시간 형식 오류.
- **시간**: ISO 8601(시간대 없으면 UTC로 간주) 또는 epoch 숫자(1e11 이상이면 밀리초로 보고 1000으로 나눔). 소수부는 **버림**(정수 초로 저장 — 열이 STRICT INTEGER). 허용 범위 1970-01-01 ~ 9999-12-31. `created_at`만 있으면 `updated_at = created_at`, `updated_at`만 있으면 `created_at = updated_at`. `updated_at < created_at` → 행 실패.
- **upsert** 키 `(project,title)`:
  - 없음 → INSERT(시간이 없으면 둘 다 현재 시각) → `created`.
  - 있음 & 입력에 시간이 **없음** → `skipped`(시간 없는 입력이 기존 기억을 덮지 않음).
  - 있음 & 입력 `updated_at` < 기존 `updated_at` → `skipped`(오래된 export가 최신 기억을 덮지 않음).
  - 있음 & 입력 `updated_at` == 기존 `updated_at`이고 `body`·`kind`·`session_id`가 모두 같음 → `skipped`(UPDATE하지 않아 트리거·재토큰화 없음).
  - 그 외(입력이 더 새로움, 또는 같은 시각인데 내용이 다름) → UPDATE(`created_at`은 기존 값 유지, `updated_at`은 입력 값, `kind`·`session_id`는 입력에 있을 때만 교체) → `updated`.
  - 같은 파일 안의 중복 키는 위 규칙을 순서대로 적용.
- 쓰기: `open_db()` 직접(서명 불일치·서버 실행 중이면 2.4 규칙대로 exit 1). **커밋 단위는 200행 또는 1초 중 먼저 오는 쪽**(Kiwi 토큰화가 트리거 안에서 돌아 트랜잭션이 길어지면 서버 요청이 `busy_timeout`을 넘길 수 있으므로 잠금 점유를 짧게).
- 배치 커밋 중 `BusyError` → 그 배치 롤백 → 같은 배치 1회 재시도 → 또 실패하면 치명 오류.
- 치명 오류(재시도 후 Busy·디스크 가득 참·서명 불일치 등) → `committed N rows before failure: <이유>` 출력 후 exit 1. 재실행은 위 upsert 규칙 덕분에 안전(이미 들어간 행은 `skipped`).
- `--dry-run`: `open_db_readonly()`로 검증 + 존재 조회만 해서 `created/updated/skipped` 예상치를 센다. DB 파일이 없으면 유효 행 전부 `created`. 스키마 버전이 1보다 크면 exit 1. 어떤 파일·스키마도 만들지 않는다.
- 출력: `created N, updated M, skipped S, failed K` + 실패 행 목록(최대 50줄, 초과분은 개수만). exit: 실패 0건이면 0, 1건 이상이면 1.

### 2.10 `contrib/agentmemory_to_jsonl.py` (코어 밖)

agentmemory 사용자가 옮겨올 수 있게 제공하는 독립 변환 스크립트(표준 라이브러리만). 출력은 2.9 JSONL(stdout 또는 `-o FILE`).
- 입력: agentmemory 저장소 디렉터리(인자, 기본 `~/.agentmemory/data/state_store.db` — 파일당 키 하나인 KV 디렉터리). `mem%3Amemories.bin`(JSON 객체 `{mem_id: {...}}`, 값 필드 `id,title,content,type,concepts,files,isLatest,sessionIds,createdAt,updatedAt`)과 `mem%3Asessions.bin`(값 필드 `id,project,cwd,...`)을 읽는다. `[verified-partial]` (로컬 저장소 jq 실측, 공개 포맷 문서 없음)
- 규칙(결정적):
  - `isLatest == false` 제외.
  - `kind` = `type` 소문자화 → `[a-z0-9_-]` 밖 문자는 `-`로 → 연속 `-` 축약 → 앞뒤 `-_` 제거 → 32자 절단 → 비면 `fact`.
  - `project` = `sessionIds`로 찾은 세션들의 `project` 중 최빈값(동률이면 사전순 첫째) → 없으면 frontmatter의 `project` → 없으면 `''`.
  - frontmatter: 본문이 선택적 `[...]` 접두 뒤 `---` 줄로 시작하고 다음 `---` 줄로 끝나는 블록. 블록 안 `key: value` 줄(들여쓰기 무시, 첫 번째로 나온 키가 이김)만 읽는다. YAML 전체 해석은 하지 않는다.
  - `title` = frontmatter `title` → 없으면 agentmemory `title`의 첫 비어 있지 않은 줄 → 없으면 본문 첫 비어 있지 않은 줄. 앞뒤 공백 제거, 200자 절단.
  - 충돌: 같은 `(project,title)`가 이미 나왔으면 접미 ` (2)`, ` (3)`…을 붙이되 접미 포함 200자가 되도록 본체를 먼저 자른다.
  - `body` = `content` + `concepts`가 비어 있지 않으면 `\n\nconcepts: a, b` + `files`가 비어 있지 않으면 `\nfiles: x, y`. 32,000자 초과 시 앞 31,000자 + `\n…(truncated)`.
  - 시간: `createdAt`·`updatedAt`(ISO 8601)을 그대로 전달.
- 손상 값(JSON 파싱 실패, 필드 누락) → 그 항목만 건너뛰고 stderr에 `skip <id>: <이유>`. 끝에 `converted N, skipped K`.

### 2.11 배포 산출물

- `pyproject.toml`: 배포명 `austin-power`, import명 `austin_power`, `requires-python >=3.11`, 의존성 `mcp>=2.2,<3`, `apsw>=3.53.4.0`, `kiwipiepy>=0.23`, `uvicorn>=0.31`. 빌드 백엔드 `hatchling`. 라이선스 MIT. 콘솔 스크립트 `austin-power`.
- 설치 안내: `uv tool install git+https://github.com/yuminFa/austin-power`(PyPI 배포 전), 이후 `uv tool install austin-power`.
- `examples/launchd/io.github.yuminfa.austin-power.plist`, `examples/systemd/austin-power.service`(user unit): 실행 파일 경로는 플레이스홀더 + `which austin-power`로 채우라는 주석. `KeepAlive` / `Restart=on-failure`.
- `README.md`(한국어 기본) + `README.en.md`(영어 요약): 한 줄 소개, 왜(실측 수치 비교, 특정 프로젝트 비방 없이), 설치·실행·등록(Claude Code·Codex·hooks), 한국어 검색 동작 예시, 도구 5개, 설정 표, 데이터 위치·**백업**(`sqlite3 memory.db ".backup backup.db"` — WAL에서 `memory.db`만 복사하면 최근 쓰기를 잃을 수 있음), 외부 도구로 쓰기 불가 안내, 설계 문서 링크.
- `docs/design.md`: 원 설계를 1.1 변경점 반영해 개정한 공개용 설계 문서.
- CI: GitHub Actions — ubuntu·macos × Python 3.11·3.13, `uv sync` → `ruff check` → `pytest`.

## 3. 접점 · 의존

- 신규 레포, 기존 코드 없음.
- `mcp==2.2.x` — `MCPServer`(v1 `FastMCP` 개명), `streamable_http_app(...)`, `TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts, allowed_origins)`. `[code-verified: mcp==2.2.0#mcp.server.mcpserver.MCPServer]` `[code-verified: mcp==2.2.0#mcp.server.transport_security.TransportSecuritySettings]`
- `apsw==3.53.4.0` — SQLite 3.53.4 번들, `Connection.register_fts5_tokenizer`, `apsw.fts5.StringTokenizer`, `apsw.fts5query.quote`, `FTS5_TOKENIZE_DOCUMENT/QUERY`, `FTS5_TOKEN_COLOCATED`. `[code-verified: apsw==3.53.4.0#apsw.fts5]`
- `kiwipiepy==0.23.2` + `kiwipiepy_model` — `Kiwi().tokenize()`의 `Token.form/.tag/.start/.len`. wheel: cp39·cp314 × macOS(x86_64·arm64)·manylinux(x86_64·aarch64)·win_amd64. `[verified-partial]` (probe에서 사용, abi3 여부는 파일명만 확인)
- Claude Code 2.1.280: `claude mcp add --transport http <name> <url> --header ... --scope user`(`--help`로 확인, evaluator 보고). PostCompact 입력 `compact_summary` `[code-verified: claude-code 2.1.280 binary#PostCompact]`. SessionStart `hookSpecificOutput.additionalContext` 식별자가 바이너리에 존재(evaluator 보고) — 동작은 e2e로 확인.
- Codex 0.153.4: `codex mcp add <name> --url <URL> --bearer-token-env-var <ENV>` `[code-verified: codex-cli 0.153.4#codex mcp add --help]`

## 4. 미결

- [x] U1. 토큰 0개 구절의 FTS5 동작 — 무매칭이며 AND 전체를 0건으로 만든다(reviewer probe 관측) → 2.6 `search`에서 사전 제거로 해결
- [ ] U2. Claude Code가 `--header` bearer로 이 서버에 붙어 도구를 호출하는지 — `blocking`(이관 단계 착수 전) / 구현 완료 후 실제 등록 → `claude mcp list` 연결 상태 + 도구 호출로 닫는다. 실패하면 2.3 인증 계약을 바꿔야 하므로 코어 구현은 진행 가능하되 이관은 막힌다
- [x] U3. Codex HTTP MCP 설정 — `codex mcp add --url --bearer-token-env-var`(2.7)
- [ ] U4. 한자·가나 단독 입력의 Kiwi 태그(2.5 규칙 4) — `deferrable` / TDD에서 관측 후 태그 목록 보정
- [ ] U5. repo description 설정 — `deferrable` / `gh`가 회사 계정만 로그인돼 있어 사용자가 개인 계정으로 설정(문구는 이 작업에서 준비)
- [x] U6. stateless에서 `initialize` 없이 `tools/call` 단독 요청 — 스크래치 probe에서 별도 요청으로 200 관측. 통합 테스트로 회귀 고정
- [ ] U7. `compact_summary` 실제 길이 분포 — `deferrable` / 절단이 잦으면 상한 재조정
- [x] U8. 서버 상주 메모리(RSS) 예산 — `deferrable` / `scripts/measure_rss.py`로 실측(2026-09-23, macOS arm64): 기동 직후 426.2MB → 첫 호출(Kiwi 지연 로딩) 후 571.2MB → 1,000회 호출 후 571.7MB. 합격 기준(기동 직후 대비 20% 이하)은 **미달성**(growth_pct 34.2%). 다만 첫 호출→1,000회 호출 구간은 +0.09%로, 세션·캐시가 호출량에 비례해 누적되지는 않음이 관측됨 — 34.2%는 거의 전부 `tokenizer.get_kiwi()`의 1회성 지연 로딩(첫 Korean-text 호출 시 Kiwi 모델 적재) 때문. 기동 직후 RSS를 "가벼운 것"으로 볼지, 실사용에서는 어차피 첫 호출에 Kiwi가 로드되므로 워밍업 후 수치를 기준선으로 재정의할지는 이 작업 범위 밖의 설계 판단으로 남김(decision_needed)

## 5. 검증 관심사

- [ ] 멱등성: `save` 같은 키 반복(같은 초 포함) → `updated`, 행 1개 / `forget` 반복 → `deleted:false` / `import` 같은 파일 재실행 → 전부 `skipped`, 행·`updated_at` 변화 없음
- [ ] 동시성(프로세스 내): 여러 세션 동시 `save`/`search` → `ThreadingViolationError` 없음, 이벤트 루프 비블로킹
- [ ] 동시성(프로세스 간): `import` 실행 중 서버 `save` → BusyError 없이 완료(커밋 단위 짧음) / 폴백 쓰기 vs 서버 재색인 경합 → 폴백 20s 대기 안에 처리 또는 경고 / 두 번째 `serve` → 잠금 실패 exit 1 / 동시 최초 마이그레이션 → 스키마 1회 생성
- [ ] FTS 동기화: insert/update/delete 후 `INSERT INTO note_fts(note_fts) VALUES('integrity-check')` 통과
- [ ] 재색인: 서명 변경 시 1회 rebuild, 이후 기동에선 없음 / 어떤 쓰기 주체든 불일치 시 먼저 rebuild
- [ ] 토크나이저 정밀도: `note_fts가`↔`note_fts를` 문서 매칭, `FTS5는`→`fts5`, `apsw가`→`apsw` / `note_fts` 질의가 `note`만 있는 문서를 찾지 않음 / `fts` 질의는 `note_fts` 문서를 찾음 / 조사 변화(`인덱스를`·`인덱스가`) 동일 매칭
- [ ] 경계값: title 200/201자, body 32000/32001자, limit 0/1/50/51, 주입 상한 경계
- [ ] 인증 경계: 토큰 없음/틀림/`bearer` 소문자 스킴, Host 위조, 외부 Origin — `/health`만 무인증
- [ ] 민감정보: `/health`·로그·오류 메시지에 토큰·본문 없음 / 토큰 파일 0600 / **자동 저장된 세션 요약에 비밀값이 섞일 수 있음 — README에 명시하고 삭제 경로(`forget`) 안내**
- [ ] 경로: 상대경로 환경변수 → 절대경로 / 홈 생성 권한 0700 / 조회성 명령은 홈을 만들지 않음
- [ ] 쿼리 인젝션: FTS5 문법 문자(`"` `*` `:` `^` `-` `NEAR` `AND`)가 섞인 질의가 오류 없이 처리
- [x] 메모리: U8 기준 — 실측 완료, 20% 기준 미달성(사유·수치는 U8 항목 참고)
- [ ] 이식성: 코드·문서·예시에 작성자 개인 경로·계정·사내 정보 없음(grep으로 확인)
- [ ] fail-open: hook이 어떤 입력·상황에서도 exit 0, stdout에는 정상 JSON 또는 아무것도 없음
