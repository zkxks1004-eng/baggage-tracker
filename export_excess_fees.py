"""메인매트릭스의 사본 탭 AD~AF(초과요금: 중량/개수/규격) 셀을 표기규칙대로 구조화해
API 엔드포인트로 내보낸다.

셀 표기규칙(2026-09-11 확정, project_baggage_copytab_restructure 메모):
"존명: 구간 금액 통화 / 구간 금액 통화; 존명: ..." — 목적 자체가 이 규칙을 정규식으로
풀어 (항공사·노선·클래스·존·구간·금액·통화) DB 테이블로 만드는 것이었다.

주의 — 규칙을 100% 안 따르는 셀이 실제로 있다(2026-09-16 검증, 전체 스캔 결과 하단
참고): 조건 설명이 금액 뒤에 괄호로 덧붙거나, 지명에 쓰인 '/'가 구간 구분자와 충돌하는
경우 등. 이런 값은 절대 억지로 숫자화하지 않고 raw 원문 그대로 남기고 unparsed로
표시한다 — "정확성이 가장 중요하다"는 프로젝트 원칙 그대로.

AG(사전구매)는 이 표기규칙을 아예 안 따르는 자유서술 텍스트라(실측 확인) 구조화하지
않고 원문만 내보낸다.
"""
import csv
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

SHEET_ID = "12WL67NcJZq09hT_nx07y-zZKCOP5AyZAHgqjZAy8nSo"
SHEET_NAME = "메인매트릭스의 사본"

# index.html의 COL 매핑과 동일한 열 순서(A=0 기준)
COL = {
    "CODE": 1, "NAME_EN": 2, "NAME_KR": 3,
    "TICKET_DATE": 4, "AIRCRAFT": 5, "BAG_TYPE": 6, "ROUTE": 7, "CLASS": 8, "BRAND": 9,
    "PAX": 10, "TIER": 11, "CODESHARE": 12, "CONCEPT": 13,
    "STATUS": 25, "LAST_CHECK": 26, "SOURCE": 27, "NOTE": 28,
    "EXCESS_WEIGHT": 29, "EXCESS_COUNT": 30, "EXCESS_SIZE": 31, "PREBUY": 32,
}

# (code, route, class, fare_brand)만으로는 행이 안 겹친다는 보장이 없다 — 실측 확인(2026-09-16):
# 같은 노선·클래스·브랜드라도 발권일 구간(ticket_date, 예: VN·TW), 기내/위탁 구분(bag_type, 예:
# LH·CX·JL), 기종(aircraft, VN만 사용)이 다르면 별개 행이다. 아래 11개 필드를 전부 합쳐야
# 1,691행 전체에서 중복이 0건이 된다 — 일부만 키로 쓰면 서로 다른 행이 덮어써진다.
IDENTITY_FIELDS = (
    "airline_code", "route", "class", "fare_brand",
    "ticket_date", "aircraft", "bag_type", "passenger_type", "tier", "codeshare", "concept",
)

TIER_RE = re.compile(r"^(?P<segment>.*?)\s*(?P<amount>[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?P<currency>[A-Za-z가-힣]+)\s*$")
FREE_RE = re.compile(r"무료\s*$")


def fetch_rows():
    url = ("https://docs.google.com/spreadsheets/d/{}/gviz/tq"
           "?tqx=out:csv&sheet={}").format(SHEET_ID, urllib.parse.quote(SHEET_NAME))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    return rows[2:]  # 1행 헤더, 2행 형식 예시


def _parse_tier(tier_chunk):
    if FREE_RE.search(tier_chunk):
        segment = FREE_RE.sub("", tier_chunk).strip() or None
        return {"segment": segment, "amount": 0, "currency": None, "free": True}
    m = TIER_RE.match(tier_chunk)
    if m:
        return {
            "segment": m.group("segment").strip() or None,
            "amount": float(m.group("amount").replace(",", "")),
            "currency": m.group("currency"),
        }
    return None


def parse_fee_cell(raw):
    """구조화 가능한 만큼만 구조화하고, 규칙을 안 따르는 부분은 raw+unparsed로 남긴다."""
    raw = (raw or "").strip()
    if not raw or raw == "-":
        return {"raw": raw, "has_fee": False, "zones": [], "unparsed": False}

    zones = []
    unparsed = False
    for zone_chunk in raw.split(";"):
        zone_chunk = zone_chunk.strip()
        if not zone_chunk:
            continue
        m = re.match(r"^([^:：]+)[:：]\s*(.+)$", zone_chunk)
        zone_name, rest = (m.group(1).strip(), m.group(2).strip()) if m else (None, zone_chunk)

        tiers = []
        for tier_chunk in rest.split("/"):
            tier_chunk = tier_chunk.strip()
            if not tier_chunk:
                continue
            parsed = _parse_tier(tier_chunk)
            if parsed:
                tiers.append(parsed)
            else:
                unparsed = True
                tiers.append({"raw": tier_chunk, "unparsed": True})
        zones.append({"zone": zone_name, "tiers": tiers})

    return {"raw": raw, "has_fee": True, "zones": zones, "unparsed": unparsed}


def build_records():
    records = []
    for row in fetch_rows():
        if len(row) <= COL["PREBUY"]:
            continue
        code = row[COL["CODE"]].strip()
        if not code:
            continue
        records.append({
            "airline_code": code,
            "airline_name_en": row[COL["NAME_EN"]].strip(),
            "airline_name_kr": row[COL["NAME_KR"]].strip(),
            "route": row[COL["ROUTE"]].strip(),
            "class": row[COL["CLASS"]].strip(),
            "fare_brand": row[COL["BRAND"]].strip(),
            # 식별용 — IDENTITY_FIELDS 설명 참고. 대부분 빈 문자열("")이고, 값이 있을 때만 진짜
            # 별개 행을 구분한다(예: VN 기종별, TW/VN 발권일 구간별, LH/CX/JL 기내·위탁 구분).
            "ticket_date": row[COL["TICKET_DATE"]].strip(),
            "aircraft": row[COL["AIRCRAFT"]].strip(),
            "bag_type": row[COL["BAG_TYPE"]].strip(),
            "passenger_type": row[COL["PAX"]].strip(),
            "tier": row[COL["TIER"]].strip(),
            "codeshare": row[COL["CODESHARE"]].strip(),
            "concept": row[COL["CONCEPT"]].strip(),
            "excess_weight_fee": parse_fee_cell(row[COL["EXCESS_WEIGHT"]]),
            "excess_count_fee": parse_fee_cell(row[COL["EXCESS_COUNT"]]),
            "excess_size_fee": parse_fee_cell(row[COL["EXCESS_SIZE"]]),
            # AG는 표기규칙을 안 따르는 자유서술이라 구조화하지 않고 원문만 담는다.
            "prebuy_discount_raw": row[COL["PREBUY"]].strip(),
            "confidence_status": row[COL["STATUS"]].strip(),
            "last_checked": row[COL["LAST_CHECK"]].strip(),
            "source_url": row[COL["SOURCE"]].strip(),
            "note": row[COL["NOTE"]].strip(),
        })
    return records


def unparsed_count(records):
    n = 0
    for r in records:
        for key in ("excess_weight_fee", "excess_count_fee", "excess_size_fee"):
            if r[key].get("unparsed"):
                n += 1
    return n


def send(records, endpoint_url):
    payload = json.dumps({"records": records}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint_url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, resp.read().decode("utf-8")


if __name__ == "__main__":
    records = build_records()
    bad = unparsed_count(records)

    endpoint = os.environ.get("EXCESS_FEE_ENDPOINT")
    if not endpoint:
        out_path = os.path.join(os.path.dirname(__file__), "excess_fees_export.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        print(f"--- [DRY RUN] EXCESS_FEE_ENDPOINT 미설정, 파일로 저장: {out_path} ---")
    else:
        status, body = send(records, endpoint)
        print(status, body[:300])

    print(f"{len(records)}행 처리, 셀 단위 파싱 실패(unparsed) {bad}건 — "
          f"unparsed 항목은 raw 원문이 그대로 들어있으니 수기 검토 대상으로 쓸 것.")
