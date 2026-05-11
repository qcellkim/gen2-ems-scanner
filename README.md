# gen2-ems-scanner

`ac_system_gen2` 프로젝트의 주간 태그 비교를 자동 수집해 TEAMS로 전송한다.

## 동작 흐름

매주 월요일 09:00 (launchd) 또는 수동 실행 시:

1. `https://172.23.1.181:20200/` 접속
2. **ac_system_gen2** 클릭
3. **weekly** 필터 클릭
4. **Base (이전)** 태그 검색 → 아래에서 2번째 항목 선택
5. **Head (최신)** 태그 검색 → 아래에서 1번째(가장 아래) 항목 선택
6. **비교** 버튼 클릭
7. 비교 결과(커밋·파일·원문)를 추출
8. Ollama API로 한국어 자연어 요약 생성
9. TEAMS webhook으로 전송

## 설치 (1회)

```bash
cd /Users/euna/claude-project/gen2-ems-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

`.env`를 열어 다음 값 입력:

| 변수 | 값 |
|---|---|
| `TEAMS_WEBHOOK_URL` | QA 채널 webhook URL |
| `TARGET_URL` | 기본값 그대로 두면 됨 |
| `HEADLESS` | `true`(기본) / `false` |

## 실행

```bash
# 기본 실행 (백그라운드 브라우저 + Slack 전송)
python scanner.py

# 브라우저 화면에 보이게
python scanner.py --headed

# 비교 단계 직전 일시정지 (selector 튜닝/디버깅용)
python scanner.py --headed --pause

# Slack 전송 생략, payload만 stdout 출력
python scanner.py --dry-run

# 실패 시 Slack 알림 끄기
python scanner.py --no-error-notify
```

## 첫 실행 시 — Selector 튜닝 (필수)

코드의 dropdown/option/결과영역 selector는 일반적인 후보 여러 개를 시도하지만, **실제 페이지 구조에 맞춰 한 번 검증**해야 한다.

```bash
python scanner.py --headed --pause --dry-run
```

브라우저가 비교 버튼 클릭 직전에 멈춘다. Playwright Inspector에서:
- 드롭다운 옵션의 실제 selector 확인 → `scanner.py`의 `_select_tag_from_dropdown` 내 `option_selectors` 리스트 보정
- 비교 결과 영역 → `extract_comparison`의 `region_candidates`, 커밋/파일 selector 보정
- 코드 내 `# TODO: verify selector` 주석 부분 참고

## 주간 자동 실행 — launchd 등록 (macOS)

```bash
cp launchd/com.gen2ems.scanner.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.gen2ems.scanner.plist

# 즉시 1회 트리거하여 동작 확인
launchctl start com.gen2ems.scanner

# 상태 확인
launchctl list | grep gen2ems
```

해제할 때:
```bash
launchctl unload ~/Library/LaunchAgents/com.gen2ems.scanner.plist
```

**전제 조건**: 실행 시점에 사내망(VPN)이 연결되어 있어야 `172.23.1.181`에 도달 가능하다. VPN 자동 연결은 이 스크립트 범위 외.

## 로그 / 디버그

| 파일 | 내용 |
|---|---|
| `scanner.log` | 실행 로그 (INFO 이상) |
| `scanner.out.log` / `scanner.err.log` | launchd stdout/stderr |
| `debug/fail-{timestamp}.png` | 실패 시 전체 페이지 스크린샷 |

## 검증 순서

1. `python scanner.py --headed --pause` — 1~4단계까지 자동 진행 후 일시정지, 화면에서 selector 확인
2. `python scanner.py --headed --dry-run` — 1~6단계 + 추출 + 요약까지 진행, payload를 stdout으로 확인
3. `python scanner.py` — 실제 TEAMS 전송, 채널에서 메시지 확인
4. `launchctl start com.gen2ems.scanner` — launchd 즉시 실행으로 스케줄 동작 확인

## 파일 구조

```
gen2-ems-scanner/
├── scanner.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
├── launchd/
│   └── com.gen2ems.scanner.plist
└── debug/                    # 자동 생성
```
