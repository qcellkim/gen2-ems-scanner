"""gen2-ems-scanner: ac_system_gen2 주간 태그 비교를 자동 수집해 Teams로 보낸다.

사용법:
    python scanner.py                    # 기본 (HEADLESS, SNS 전송)
    python scanner.py --headed           # 브라우저 보이게
    python scanner.py --pause            # 비교 단계 직전 일시정지 (selector 튜닝)
    python scanner.py --dry-run          # SNS POST 생략, payload만 stdout 출력
    python scanner.py --no-error-notify  # 실패 알림 끄기

설치 (1회):
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python -m playwright install chromium
    cp .env.example .env  # 후 TEAMS_WEBHOOK_URL, ANTHROPIC_API_KEY 채우기
"""

import argparse
import json
import logging
import os
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

import requests
#from anthropic import Anthropic
import ollama
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

BASE_DIR = Path(__file__).resolve().parent
DEBUG_DIR = BASE_DIR / "debug"
LOG_FILE = BASE_DIR / "log" / "scanner.log"

load_dotenv(BASE_DIR / ".env")

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ac_system_gen2 주간 태그 비교 자동화")
    parser.add_argument("--headed", action="store_true", help="브라우저 화면에 표시")
    parser.add_argument("--pause", action="store_true", help="비교 단계 직전 page.pause()")
    parser.add_argument("--dry-run", action="store_true", help="SNS 전송 생략")
    parser.add_argument("--no-error-notify", action="store_true", help="실패 시 SNS 알림 비활성화")
    return parser.parse_args()


def navigate_and_compare(page: Page, target_url: str, pause: bool) -> dict:
    """사용자 요청 1~7번 단계를 순서대로 수행."""
    logging.info("페이지 접속: %s", target_url)
    page.goto(target_url, wait_until="networkidle", timeout=30000)

    # 1. 태그 비교 탭
    logging.info("[1/7] 태그 비교 탭 클릭")
    page.get_by_role("button", name="태그 비교").first.click()
    page.wait_for_load_state("networkidle")

    # 2. ac_system_gen2 버튼
    logging.info("[2/7] ac_system_gen2 클릭")
    page.get_by_role("button", name="ac_system_gen2").first.click()
    page.wait_for_load_state("networkidle")

    # 3. weekly 버튼
    logging.info("[3/7] weekly 필터 클릭")
    page.get_by_role("button", name="weekly").first.click()
    page.wait_for_load_state("networkidle")

    # 4. Base (이전) 태그 검색 → 아래에서 2번째
    logging.info("[4/7] Base 태그 — 아래에서 2번째 선택")
    base_tag = _select_tag_from_dropdown(page, label="Base (이전)", from_bottom=2)

    # 5. Head (최신) 태그 검색 → 아래에서 1번째 (가장 아래)
    logging.info("[5/7] Head 태그 — 아래에서 1번째 선택")
    head_tag = _select_tag_from_dropdown(page, label="Head (최신)", from_bottom=1)

    if pause:
        logging.info("--pause: 비교 버튼 클릭 직전 일시정지. Inspector에서 selector를 검증하세요.")
        page.pause()

    # 6. 비교 버튼 (id="compareTagsBtn")
    logging.info("[6/7] 비교 버튼 클릭")
    compare_btn = page.locator("#compareTagsBtn")
    compare_btn.wait_for(state="visible", timeout=10000)
    try:
        compare_btn.scroll_into_view_if_needed(timeout=30000)
    except PWTimeout:
        pass
    try:
        compare_btn.click(timeout=5000)
    except PWTimeout:
        logging.warning("비교 버튼 일반 클릭 실패 — JS click으로 재시도")
        compare_btn.evaluate("el => el.click()")

    # 결과 영역 대기: 스피너 등장 → 사라짐 → 결과 컨테이너 visible
    spinner = page.locator("div.progress-spinner")
    try:
        spinner.wait_for(state="visible", timeout=5000)
        logging.info("로딩 스피너 감지 — 사라질 때까지 대기")
    except PWTimeout:
        logging.info("스피너 등장 감지 안 됨 — 즉시 결과 대기로 진행")

    try:
        spinner.wait_for(state="hidden", timeout=300000)
    except PWTimeout:
        raise RuntimeError("비교 로딩이 300초 내에 끝나지 않음 (progress-spinner 미사라짐)")

    try:
        page.wait_for_selector("#compareCommitSection", state="visible", timeout=30000)
    except PWTimeout:
        raise RuntimeError("결과 컨테이너 #compareCommitSection 가 30초 내에 나타나지 않음")

    # 7. 결과 추출
    logging.info("[7/7] 비교 결과 추출")
    return extract_comparison(page, base_tag, head_tag)


def _select_tag_from_dropdown(page: Page, label: str, from_bottom: int) -> str:
    """label 텍스트 하위의 input[type=text]를 클릭하고, tag-search-dropdown에서 아래에서 N번째 옵션 선택."""
    page.locator(f"xpath=//*[text()='{label}']/following::input[@type='text'][1]").click()

    # tag-search-item 등장 대기
    options = page.locator("div.tag-search-dropdown .tag-search-item")
    options.first.wait_for(state="attached", timeout=10000)
    total = options.count()
    logging.info("드롭다운 옵션 %d개 감지 (%s)", total, label)

    if total == 0:
        raise RuntimeError(f"드롭다운 옵션을 찾을 수 없음 ({label})")
    if from_bottom > total:
        raise RuntimeError(
            f"옵션이 {total}개뿐인데 아래에서 {from_bottom}번째 요청 ({label})"
        )

    target_index = total - from_bottom
    selected = options.nth(target_index)
    tag_text = selected.inner_text().strip()
    selected.click()
    page.wait_for_load_state("networkidle", timeout=10000)

    logging.info("선택된 태그: %s", tag_text)
    return tag_text


def extract_comparison(page: Page, base_tag: str, head_tag: str) -> dict:
    """비교 결과 영역에서 커밋/파일/원문 텍스트를 추출."""
    region = page.locator("#compareCommitSection").first
    if region.count() == 0:
        raise RuntimeError("결과 영역 #compareCommitSection 을 찾을 수 없음")
    logging.info("결과 영역 selector: #compareCommitSection")

    data = {
        "base": base_tag,
        "head": head_tag,
        "commits": [],
        "files": [],
        "raw": "",
        "url": None,
    }

    # 커밋 리스트 (구조화 추출 시도)
    for sel in ["li.commit", ".commit-item", "[data-commit]", "tr.commit-row"]:
        items = region.locator(sel)
        if items.count() > 0:
            data["commits"] = [
                items.nth(i).inner_text().strip()
                for i in range(min(items.count(), 30))
            ]
            logging.info("커밋 %d개 추출 (selector: %s)", len(data["commits"]), sel)
            break

    # 변경 파일
    for sel in [".file-diff", ".changed-file", "[data-file-path]"]:
        files = region.locator(sel)
        if files.count() > 0:
            data["files"] = [
                files.nth(i).inner_text().strip() for i in range(files.count())
            ]
            logging.info("파일 %d개 추출 (selector: %s)", len(data["files"]), sel)
            break

    # 원문 텍스트 (LLM fallback용)
    #data["raw"] = region.inner_text()[:3000]
    data["raw"] = region.inner_text()

    # 비교 결과 permalink
    permalink = page.locator('a[href*="compare"]').first
    if permalink.count() > 0:
        data["url"] = permalink.get_attribute("href")

    if not data["commits"] and not data["files"] and len(data["raw"].strip()) < 30:
        raise RuntimeError(
            f"비교 결과가 비어 있음 (commits=0, files=0, raw={len(data['raw'])}자). "
            "로딩이 끝나기 전에 추출되었거나 실제로 변경사항이 없는지 확인 필요"
        )

    return data


def summarize_with_llama(data: dict) -> str:
    """Ollama(Llama) 로컬 모델을 사용하여 한국어 자연어 요약 생성."""
    
    # 모델명 설정 (설치된 모델명에 맞춰 수정하세요: 예: llama3, llama3.1, llama3.2)
    model_name = "qwen2.5-coder:3b"

    payload = json.dumps(
        {
            "base": data["base"],
            "head": data["head"],
            "commits": data["commits"],
            "files": data["files"],
            "raw_excerpt": data["raw"][:2000],
        },
        ensure_ascii=False,
        indent=2,
    )

    prompt_content = (
        "반드시 모든 내용을 한국어로 답변하세요.\n"
        "다음은 프로젝트의 git 태그 비교 결과입니다.\n"
        "아래 형식으로 요약해주세요.\n"
        "형식:\n"
        "1) 핵심 변화 (가장 중요한 변경 5개 이내)\n"
        "2) 주요 변경 카테고리 -기능 / 버그 수정 / 리팩터 / 테스트 / 문서별- bullet 정리\n"
        f"비교 데이터:\n{payload}"
    )

    try:
        # Ollama 클라이언트 호출
        response = ollama.generate(
            model=model_name,
            prompt=prompt_content,
        )
        
        summary = response['response']
        logging.info("Llama 요약 생성 완료 (%d자)", len(summary))
        return summary

    except Exception as e:
        logging.error("Ollama 호출 중 오류 발생: %s", e)
        raise RuntimeError(f"Ollama 요약 생성 실패: {e}")


def build_sns_payload(data: dict, summary: str) -> dict:
    permalink = data.get("url") or os.getenv("TARGET_URL", "")
    text = (
        f"📊 *gen2 EMS Weekly 태그 비교 리포트* — `ac_system_gen2`\n"
        f"• Base: `{data['base']}`\n"
        f"• Head: `{data['head']}`\n"
        f"• 링크: {permalink}\n\n"
        f"*변경사항 요약*\n{summary}"
    )
    return {"text": text}

"""
def send_to_slack(payload: dict) -> None:
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook or "XXX/YYY/ZZZ" in webhook:
        raise RuntimeError("SLACK_WEBHOOK_URL이 비어있거나 템플릿 그대로임")
    response = requests.post(webhook, json=payload, timeout=10)
    response.raise_for_status()
    logging.info("Slack 전송 완료")

"""
def send_to_teams(payload: dict) -> None:
    webhook = os.getenv("TEAMS_WEBHOOK_URL")
    if not webhook or "YOUR_WEBHOOK_URL" in webhook:
        raise RuntimeError("TEAMS_WEBHOOK_URL이 비어있거나 템플릿 그대로임")

    teams_payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": payload.get("text", ""),
                            "wrap": True
                        }
                    ]
                }
            }
        ]
    }

    response = requests.post(webhook, json=teams_payload, timeout=10)
    response.raise_for_status()
    logging.info("Teams 전송 완료")


def notify_failure(exc: Exception) -> None:
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    #webhook = os.getenv("TEAMS_WEBHOOK_URL")
    if not webhook or "XXX/YYY/ZZZ" in webhook:
        logging.warning("실패 알림 스킵 — webhook 미설정")
        return
    msg = f" ❌ *gen2-ems-scanner 실행 실패*\n```\n{type(exc).__name__}: {exc}\n```"
    try:
        requests.post(webhook, json={"text": msg}, timeout=10).raise_for_status()
        logging.info("실패 알림 전송 완료")
    except Exception:
        logging.exception("실패 알림 전송도 실패")


def save_file(raw_data, file_name):
    # 파일로 저장
    with open(file_name, "w", encoding="utf-8") as f:
        json.dump(raw_data, f, ensure_ascii=False, indent=4)


def main() -> int:
    args = parse_args()
    DEBUG_DIR.mkdir(exist_ok=True)

    target_url = os.getenv("TARGET_URL", "https://172.23.1.181:20200/")
    headless_env = os.getenv("HEADLESS", "true").lower() != "false"
    headless = False if args.headed else headless_env

    logging.info("=" * 60)
    logging.info("scanner 시작 (headless=%s, dry_run=%s)", headless, args.dry_run)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(ignore_https_errors=True)
        page = ctx.new_page()

        try:
            data = navigate_and_compare(page, target_url, pause=args.pause)
            save_file(data, "data.json")
            summary = summarize_with_llama(data)
            save_file(summary, "summary.json")
            payload = build_sns_payload(data, summary)

            if args.dry_run:
                print("=== DRY RUN — Slack payload ===")
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                send_to_teams(payload)
                #send_to_slack(payload)

            logging.info("scanner 정상 종료")
            return 0

        except Exception as exc:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            screenshot = DEBUG_DIR / f"fail-{ts}.png"
            try:
                page.screenshot(path=str(screenshot), full_page=True)
                logging.error("실패 스크린샷 저장: %s", screenshot)
            except Exception:
                logging.exception("스크린샷 저장 실패")

            logging.error("scanner 실패:\n%s", traceback.format_exc())

            if not args.no_error_notify and not args.dry_run:
                notify_failure(exc)
            return 1

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
