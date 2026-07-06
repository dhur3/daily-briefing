# 매일 GitHub Actions가 이 스크립트를 실행합니다.
# 1) keywords.json에 있는 키워드별로 네이버 뉴스 + DART 공시를 모으고
# 2) docs/ 폴더에 정적 웹페이지를 새로 생성하고 (GitHub Pages가 이 폴더를 서빙)
# 3) (설정했다면) 이메일로도 요약을 보냅니다.

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ARCHIVE = DOCS / "archive"
KST = ZoneInfo("Asia/Seoul")

NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
DART_API_KEY = os.environ.get("DART_API_KEY", "")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
TO_EMAIL = os.environ.get("TO_EMAIL", "")


def strip_tags(text):
    # 네이버 API는 검색어를 <b>태그</b>로 감싸서 줍니다. 그리고 &quot; 같은 HTML 엔티티도 풀어줍니다.
    text = re.sub(r"<.*?>", "", text or "")
    text = (
        text.replace("&quot;", '"')
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
    )
    return text.strip()


def fetch_news(keyword, today_str):
    # 네이버 뉴스 검색 API. 무료. https://developers.naver.com 에서 발급.
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    params = {"query": keyword, "display": 30, "sort": "date"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        print(f"[news] {keyword} 조회 실패: {e}")
        return []

    results = []
    for it in items:
        try:
            pub = datetime.strptime(it["pubDate"], "%a, %d %b %Y %H:%M:%S %z")
            pub_kst = pub.astimezone(KST)
        except Exception:
            continue
        # 오늘 하루치만 (실행 시각 기준 최근 24시간)
        if pub_kst.strftime("%Y-%m-%d") != today_str:
            continue
        results.append(
            {
                "title": strip_tags(it.get("title")),
                "summary": strip_tags(it.get("description")),
                "source": "네이버뉴스",
                "date": pub_kst.strftime("%Y-%m-%d %H:%M"),
                "url": it.get("originallink") or it.get("link"),
            }
        )
    return results


def fetch_disclosures(keyword, today_str):
    # DART Open API. 무료. https://opendart.fss.or.kr 에서 발급.
    # 주의: 이 API는 자유 키워드 전문 검색이 아니라, 오늘 접수된 전체 공시 목록을 가져온 뒤
    # 공시 제목/회사명에 키워드가 들어있는 것만 걸러내는 방식입니다.
    # -> 회사 이름을 키워드로 쓰면 잘 걸러지고, "부동산 NCR 규제" 같은 정책 키워드는
    #    공시 제목에 그대로 안 쓰이는 경우가 많아 잘 안 잡힐 수 있습니다.
    if not DART_API_KEY:
        return []
    today_de = today_str.replace("-", "")
    results = []
    page_no = 1
    while True:
        params = {
            "crtfc_key": DART_API_KEY,
            "bgn_de": today_de,
            "end_de": today_de,
            "page_no": page_no,
            "page_count": 100,
        }
        try:
            r = requests.get(
                "https://opendart.fss.or.kr/api/list.json", params=params, timeout=15
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[dart] 조회 실패: {e}")
            break

        if data.get("status") != "000":
            break

        for it in data.get("list", []):
            title = it.get("report_nm", "")
            corp = it.get("corp_name", "")
            if keyword in title or keyword in corp:
                results.append(
                    {
                        "title": f"[{corp}] {title}",
                        "summary": "DART 전자공시시스템에 접수된 공시입니다.",
                        "source": "DART",
                        "date": it.get("rcept_dt", today_de),
                        "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it.get('rcept_no')}",
                    }
                )

        total_page = data.get("total_page", 1)
        if page_no >= total_page:
            break
        page_no += 1

    return results


def build_report(today_str):
    with open(ROOT / "keywords.json", encoding="utf-8") as f:
        keywords = json.load(f)["keywords"]

    report = {"date": today_str, "keywords": {}}
    for kw in keywords:
        news = fetch_news(kw, today_str)
        disclosures = fetch_disclosures(kw, today_str)
        report["keywords"][kw] = {"news": news, "disclosures": disclosures}
    return report


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>데일리 브리핑 데스크 — {date}</title>
<style>
body {{ margin:0; background:#101B24; color:#EDE7D8; font-family: sans-serif; }}
.desk {{ max-width: 900px; margin: 0 auto; padding: 30px 20px 60px; }}
h1 {{ font-size: 24px; border-bottom: 2px solid #EDE7D8; padding-bottom: 12px; margin-bottom: 18px; }}
.tabs {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom: 14px; }}
.tab-btn {{ background: transparent; border:1px solid rgba(237,231,216,0.3); color:#EDE7D8;
  padding:8px 14px; border-radius:20px; font-size:13.5px; cursor:pointer; }}
.tab-btn.active {{ background:#C99A3D; color:#16232E; border-color:#C99A3D; font-weight:600; }}
.search-row {{ margin-bottom: 22px; }}
.search-row input {{ width:100%; box-sizing:border-box; background:#16232E; border:1px solid rgba(237,231,216,0.2);
  color:#EDE7D8; padding:10px 12px; border-radius:6px; font-size:14px; }}
.view-toggle {{ display:flex; gap:8px; margin: 6px 0 18px; }}
.view-toggle button {{ background: transparent; border:1px solid rgba(237,231,216,0.25); color:#93A4AF;
  padding:6px 12px; border-radius:14px; font-size:12.5px; cursor:pointer; }}
.view-toggle button.active {{ color:#EDE7D8; border-color:#5B8DBE; }}
.summary-list {{ list-style:none; padding:0; margin:0; }}
.summary-list li {{ padding:9px 0; border-top:1px solid rgba(237,231,216,0.1); font-size:13.5px; }}
.summary-list .tag {{ display:inline-block; width:34px; font-size:10.5px; color:#16232E; text-align:center;
  border-radius:2px; margin-right:8px; }}
.summary-list .tag.news {{ background:#B5D4F4; }}
.summary-list .tag.disclosure {{ background:#FAC775; }}
.summary-list a {{ color:#EDE7D8; text-decoration:none; }}
.summary-list a:hover {{ color:#5B8DBE; }}
.dispatch {{ display:flex; gap:14px; padding:14px 0; border-top:1px solid rgba(237,231,216,0.12); }}
.stamp {{ flex-shrink:0; width:56px; text-align:center; font-size:11px; padding:4px 2px; border-radius:2px; color:#16232E; }}
.stamp.news {{ background:#B5D4F4; }}
.stamp.disclosure {{ background:#FAC775; }}
.d-title {{ font-weight:600; margin:0 0 4px; }}
.d-summary {{ font-size:13.5px; color:#C7CDD1; margin:0 0 6px; }}
.d-meta {{ font-size:12px; color:#93A4AF; }}
.d-meta a {{ color:#5B8DBE; margin-left:8px; }}
.empty {{ color:#93A4AF; font-size:14px; }}
.archive {{ margin-top:40px; font-size:13px; color:#93A4AF; }}
.archive a {{ color:#5B8DBE; margin-right:10px; }}
.tab-panel {{ display:none; }}
.tab-panel.active {{ display:block; }}
</style>
</head>
<body>
<div class="desk">
  <h1>데일리 브리핑 데스크 — {date}</h1>
  <div class="tabs">{tab_buttons}</div>
  <div class="search-row"><input id="search" placeholder="제목으로 검색..." oninput="filterItems()"></div>
  {panels}
  <div class="archive">지난 리포트: {archive_links}</div>
</div>
<script>
function showTab(kw) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.kw === kw));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.dataset.kw === kw));
  document.getElementById('search').value = '';
  filterItems();
}}
function showView(kw, view) {{
  const panel = document.querySelector('.tab-panel[data-kw="' + kw + '"]');
  panel.querySelectorAll('.view-toggle button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  panel.querySelector('.summary-view').style.display = view === 'summary' ? 'block' : 'none';
  panel.querySelector('.full-view').style.display = view === 'full' ? 'block' : 'none';
}}
function filterItems() {{
  const q = document.getElementById('search').value.trim().toLowerCase();
  document.querySelectorAll('.tab-panel.active [data-title]').forEach(el => {{
    const match = el.dataset.title.toLowerCase().includes(q);
    el.style.display = match ? '' : 'none';
  }});
}}
</script>
</body>
</html>
"""


def render_summary_list(items, empty_label):
    if not items:
        return f'<p class="empty">{empty_label}</p>'
    rows = []
    for it in items:
        cls = "disclosure" if it.get("_kind") == "disclosure" else "news"
        tag = "공시" if cls == "disclosure" else "뉴스"
        title_attr = it["title"].replace('"', "&quot;")
        rows.append(
            f'<li data-title="{title_attr}"><span class="tag {cls}">{tag}</span>'
            f'<a href="{it["url"]}" target="_blank">{it["title"]}</a></li>'
        )
    return f'<ul class="summary-list">{"".join(rows)}</ul>'


def render_items_html(items, empty_label, stamp_class):
    if not items:
        return f'<p class="empty">{empty_label}</p>'
    rows = []
    for it in items:
        title_attr = it["title"].replace('"', "&quot;")
        rows.append(
            f"""
            <div class="dispatch" data-title="{title_attr}">
              <div class="stamp {stamp_class}">{'공시' if stamp_class == 'disclosure' else '뉴스'}</div>
              <div class="d-body">
                <p class="d-title">{it['title']}</p>
                <p class="d-summary">{it['summary']}</p>
                <div class="d-meta">{it['source']} · {it['date']}
                  <a href="{it['url']}" target="_blank">원문 보기</a>
                </div>
              </div>
            </div>
            """
        )
    return "".join(rows)


def render_page(report, archive_dates):
    tab_buttons = []
    panels = []
    keywords = list(report["keywords"].keys())
    for i, kw in enumerate(keywords):
        data = report["keywords"][kw]
        active = "active" if i == 0 else ""
        tab_buttons.append(
            f'<button class="tab-btn {active}" data-kw="{kw}" onclick="showTab(this.dataset.kw)">{kw}</button>'
        )

        all_items = (
            [{**it, "_kind": "news"} for it in data["news"]]
            + [{**it, "_kind": "disclosure"} for it in data["disclosures"]]
        )

        panels.append(f"""
        <div class="tab-panel {active}" data-kw="{kw}">
          <div class="view-toggle">
            <button class="active" data-view="summary" onclick="showView('{kw}','summary')">간단 요약 보기</button>
            <button data-view="full" onclick="showView('{kw}','full')">전체 원문 목록</button>
          </div>
          <div class="summary-view">
            {render_summary_list(all_items, "오늘 수집된 내용이 없습니다.")}
          </div>
          <div class="full-view" style="display:none">
            {render_items_html(data["news"], "오늘 수집된 뉴스가 없습니다.", "news")}
            {render_items_html(data["disclosures"], "오늘 수집된 공시가 없습니다.", "disclosure")}
          </div>
        </div>
        """)

    archive_links = " ".join(
        f'<a href="archive/{d}.html">{d}</a>' for d in archive_dates
    ) or "-"
    return PAGE_TEMPLATE.format(
        date=report["date"],
        tab_buttons="".join(tab_buttons),
        panels="".join(panels),
        archive_links=archive_links,
    )


def send_email(report):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and TO_EMAIL):
        print("이메일 설정이 없어 발송을 건너뜁니다.")
        return

    lines = [f"[데일리 브리핑 데스크] {report['date']}\n"]
    for kw, data in report["keywords"].items():
        lines.append(f"■ {kw}")
        lines.append(f"  - 뉴스 {len(data['news'])}건, 공시 {len(data['disclosures'])}건")
        for it in data["news"][:5]:
            lines.append(f"  · [뉴스] {it['title']} ({it['source']})")
        for it in data["disclosures"][:5]:
            lines.append(f"  · [공시] {it['title']}")
        lines.append("")
    body = "\n".join(lines)

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"데일리 브리핑 데스크 {report['date']}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = TO_EMAIL

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            s.sendmail(GMAIL_ADDRESS, [TO_EMAIL], msg.as_string())
        print("이메일 발송 완료")
    except Exception as e:
        print(f"이메일 발송 실패: {e}")


def main():
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    report = build_report(today_str)

    DOCS.mkdir(exist_ok=True)
    ARCHIVE.mkdir(exist_ok=True)

    # 지난 리포트 목록 (최근 것부터, 최대 14개만 링크에 노출)
    existing = sorted(
        [p.stem for p in ARCHIVE.glob("*.html")], reverse=True
    )[:14]

    page = render_page(report, existing)
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (ARCHIVE / f"{today_str}.html").write_text(page, encoding="utf-8")

    with open(DOCS / "latest.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    send_email(report)


if __name__ == "__main__":
    main()
