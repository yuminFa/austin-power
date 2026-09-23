# austin-power

코딩 에이전트를 위한 가볍고 한국어에 강한 기억 저장소 (MCP)

[English](README.en.md) · [설계 문서](docs/design.md) · MIT License

## 왜 만들었나

기존 범용 메모리 서버를 실사용하며 관측한 문제(1MB 데이터 기준 실측)를 SQLite FTS5 증분 색인 + 단일 프로세스 + 한국어 형태소 토크나이저로 해결합니다.

| 항목 | 기존 범용 메모리 서버 사용 경험 | austin-power |
|---|---|---|
| 상주 프로세스 | 세션마다 여러 개 (계층 4단) | 전역 HTTP 서버 **1개**(+ 유휴 시 스스로 종료되는 Kiwi 워커) |
| 상주 메모리(RSS, 데이터 1MB 기준) | 약 550MB | **서버 프로세스 자체 69.5MB** / **사용 중 최대 580MB**(아래 "메모리: 유휴 vs 사용 중" 참고) |
| 인덱스 방식 | 세대별 스냅샷 누적(654세대·6.8GB 관측, 재사용 없이 계속 쌓임) | SQLite FTS5, 증분 갱신 |
| 재색인 | 전체 재구축이 세대로 계속 누적 | 트리거로 변경분만 갱신, 전체 재색인 없음 |
| 한국어 검색 | 조사가 그대로 토큰이 되어 검색이 빗나감 | Kiwi 형태소 분석 + 식별자 원형 보존 |
| 종료 | 부분 종료 불가 관측 | 단일 프로세스라 `kill`/`austin-power status` 로 상태 파악 |

### 메모리: 유휴 vs 사용 중

Kiwi 모델(~400MB)은 서버 프로세스에 상주하지 않고 별도 자식 프로세스(`kiwi_worker`)에서만 적재됩니다. 워커는 **한국어 분석이 처음 필요할 때**(저장·검색) 뜨고, 요청이 없으면 `AUSTIN_POWER_KIWI_IDLE`초 뒤 스스로 내려갑니다. 그래서 메모리 수치는 둘로 나뉩니다:

- **유휴**(서버 프로세스만 — 기동 직후이거나 워커가 유휴 종료된 뒤) — 약 **69.5MB**
- **사용 중**(서버 + 워커 합계, `save`/`search` 1,000회 호출 중 관측한 최대치) — 약 **580MB**
- 워커는 `AUSTIN_POWER_KIWI_IDLE`초(기본 600, [설정](#설정) 참고) 동안 요청이 없으면 스스로 종료해 그 400MB를 OS에 돌려줍니다. 유휴 종료 후 다음 호출은 워커를 다시 띄우고 모델을 재적재하느라 약 **0.87초** 걸립니다.
- 토크나이저 규칙이 바뀌어 전체 재색인이 필요할 때(노트 1,000개 기준)는 서버 재기동~정상화까지 약 **1.22초**(워커 재기동·모델 재적재·FTS5 재색인 전부 포함).
- 실측(2026-09-23, macOS arm64, `scripts/measure_rss.py`): 위 4개 수치 모두 목표(서버 단독 ≤120MB · 사용 중 합계 ≤650MB · 유휴 후 첫 호출 ≤3s · 재색인 1,000개 ≤15s) 통과.

## 특징

- 단일 SQLite 파일 — 서버·CLI·hook이 모두 같은 `memory.db` 하나만 본다.
- FTS5 증분 색인 — 트리거가 바뀐 행만 갱신, 전체 재색인 없음.
- Kiwi 형태소 분석 + 식별자 원형 보존 — 한국어 조사/어미는 제거하고, `note_fts` 같은 코드 식별자는 원형 그대로 검색된다.
- 서버는 LLM을 호출하지 않는다 — 검색·저장은 순수 SQL, 요약도 클라이언트(Claude Code)가 이미 만든 것을 받아 저장할 뿐이다.
- 세션 압축(PostCompact) 시 요약을 자동 저장 — 추가 LLM 호출 없이 세션 기억이 쌓인다.

## 한국어 검색 예시

아래 매치는 실제 `kiwi` FTS5 토크나이저로 확인한 것입니다(조사는 버리고, 식별자는 원형을 보존합니다).

| 저장한 문장 | 검색어 | 왜 매치되는가 |
|---|---|---|
| `인덱스를 다시 만들었다` | `인덱스가` | 조사(을/를/이/가)를 버리고 어간 `인덱스`로 매치 |
| `인덱스를 다시 만들었다` | `만들다` | 용언 `만들었다`가 어간 `만들`로 정규화되어 매치 |
| `note_fts를 통째로 다시 만들었다` | `note_fts` | 코드 식별자는 원형이 그대로 보존되어(조사만 제거) 매치 |
| `note_fts를 통째로 다시 만들었다` | `fts` | 색인 시 `note_fts`가 `note`/`fts` 부분 토큰으로도 함께 저장되어 매치 |

## 설치·실행

```sh
uv tool install git+https://github.com/yuminFa/austin-power
austin-power serve
```

`austin-power serve`는 전경(foreground) 실행입니다. 상주시키려면 예시 서비스 파일을 참고하세요: [launchd (macOS)](examples/launchd/io.github.yuminfa.austin-power.plist) · [systemd --user (Linux)](examples/systemd/austin-power.service).

## Claude Code / Codex에 등록하기

`austin-power setup <target>`은 **출력만 하고 다른 도구의 설정 파일을 수정하지 않습니다.**

```sh
$ austin-power setup claude
claude mcp add --transport http --scope user austin-power http://127.0.0.1:7760/mcp --header "Authorization: Bearer <TOKEN>"
```

> ⚠️ 이 명령에는 토큰이 그대로 들어갑니다. 셸 히스토리에 남는 것이 걱정되면 `austin-power token`으로 값만 따로 확인하세요.

```sh
$ austin-power setup codex
codex mcp add austin-power --url http://127.0.0.1:7760/mcp --bearer-token-env-var AUSTIN_POWER_TOKEN
# add to your shell profile:
export AUSTIN_POWER_TOKEN="$(austin-power token)"
```

Claude Code hook을 등록하려면 `austin-power setup hooks`가 출력하는 JSON을 `~/.claude/settings.json`(전역) 또는 프로젝트의 `.claude/settings.json` **최상위**에 병합합니다 — 출력에 `hooks` 키가 이미 들어 있으니 그 안에 한 번 더 감싸지 마세요:

```json
{
  "hooks": {
    "PostCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "austin-power hook post-compact",
            "timeout": 30
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "austin-power hook session-start",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

`PostCompact`는 세션 압축 요약을 자동 저장(증류)하고, `SessionStart`는 같은 프로젝트의 최근 기억을 세션 시작 컨텍스트에 주입합니다.

## 도구 5개

| 도구 | 설명 |
|---|---|
| `save` | 기억 저장. 같은 `(project, title)`이면 덮어씀(upsert) |
| `search` | 한국어 형태소 인식 전문 검색. 발췌(excerpt)만 반환 |
| `get` | id로 기억 하나의 전체 본문 조회 |
| `recent` | 프로젝트별 최근 갱신 기억 목록 |
| `forget` | id로 기억 삭제 |

## 설정

환경변수는 `austin-power` CLI/서버/hook이 공통으로 읽습니다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `AUSTIN_POWER_HOME` | `~/.config/austin-power` | 데이터 홈 디렉터리 (`XDG_CONFIG_HOME`가 있으면 그 아래) |
| `AUSTIN_POWER_DB` | `<home>/memory.db` | SQLite 파일 경로 |
| `AUSTIN_POWER_HOST` | `127.0.0.1` | 서버 바인드 호스트 (v0.1은 loopback만 허용) |
| `AUSTIN_POWER_PORT` | `7760` | 서버 포트 |
| `AUSTIN_POWER_INJECT_CHARS` | `4000` | SessionStart hook이 주입하는 컨텍스트 상한(자) |
| `AUSTIN_POWER_LOG_LEVEL` | `INFO` | 서버 로그 레벨 |
| `AUSTIN_POWER_KIWI_IDLE` | `600` | Kiwi 워커 프로세스의 유휴 종료 대기(초). `0`이면 유휴 종료하지 않음 |
| `AUSTIN_POWER_PROJECT` | (없음) | hook의 project 자동 판별(git repo 이름)을 덮어씀 |
| `AUSTIN_POWER_TOKEN` | (없음) | Codex 쪽에서만 읽는 환경변수 이름(서버는 읽지 않음) — `austin-power setup codex` 출력 참고 |

토큰 파일은 `<home>/token`, 서버 락 파일은 `<home>/server.lock`에 있습니다.

## 데이터 위치와 백업

- 데이터는 전부 `$AUSTIN_POWER_HOME`(기본 `~/.config/austin-power`) 아래 `memory.db` 한 파일에 있습니다.
- 백업: `sqlite3 memory.db ".backup backup.db"`
- ⚠️ WAL 모드이므로 서버가 켜져 있는 동안 `memory.db` 파일만 그냥 복사하면 최근 쓰기를 잃을 수 있습니다. 위 `.backup` 명령을 쓰세요.
- 이 DB는 `kiwi` FTS5 토크나이저를 등록한 연결로만 써야 합니다 — 읽기는 어떤 sqlite3 클라이언트로도 가능하지만, 다른 도구로 직접 `INSERT`/`UPDATE`하면 안 됩니다(색인이 어긋납니다).
- 세션 압축 요약(`session` kind)에 비밀값이 섞여 들어갈 수 있습니다. 발견하면 `forget` 도구로 지우세요.

## agentmemory에서 옮겨 오기

agentmemory를 쓰고 있었다면, 독립 변환 스크립트로 JSONL을 만들어 그대로 import할 수 있습니다:

```sh
python contrib/agentmemory_to_jsonl.py -o am.jsonl
austin-power import am.jsonl
```

세션 압축 요약(`mem%3Asummaries.bin`)도 함께 변환됩니다(`--no-summaries`로 끌 수 있음). 자세한 변환 규칙은 [`contrib/README.md`](contrib/README.md)를 참고하세요.

## 한계 (v0.1)

- 로컬 전용(loopback 바인딩만, 원격 접속 불가)
- SessionEnd 증류 없음(서버가 LLM을 호출하지 않아 요약을 만들 수단이 없음)
- Windows 미검증

## 라이선스

MIT. 자세한 설계는 [`docs/design.md`](docs/design.md)를 참고하세요.
