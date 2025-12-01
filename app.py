from asyncio import open_connection
import os
import math
import json        # ⬅⬅⬅ 요기!! 딱 여기 넣으면 됨
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request, render_template, jsonify, redirect, render_template_string
from datetime import datetime, timedelta
from psycopg2.errors import UndefinedColumn
from jinja2 import TemplateNotFound

from psycopg2 import sql
import re
from collections import defaultdict
import random

app = Flask(__name__)

# =========================
# 환경 변수 / 설정
# =========================

DB_HOST = os.getenv("DB_HOST", "dpg-cs366qo8fa8c73e13v4g-a")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "nyamnyam_user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "ETGklkjHIMpvYm7SR8jfDgISfhkpaF7Y")
DB_NAME = os.getenv("DB_NAME", "nyamnyam")

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "f2f3a9c2b5d912ae8a0c5ff0548b0aa6")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")


BASE_SERVER_URL = os.getenv("SERVER_BASE_URL")

# ==== ALIGO 설정 ====
ALIGO_API_KEY = os.getenv("ALIGO_API_KEY", "")        # 알리고 API Key
ALIGO_USER_ID = os.getenv("ALIGO_USER_ID", "")        # 알리고 User ID
ALIGO_SENDER_KEY = os.getenv("ALIGO_SENDER_KEY", "")  # 승인된 발신프로필 SenderKey
ALIGO_SENDER = os.getenv("ALIGO_SENDER", "")          # 알리고에 등록된 발신번호 (카톡채널)
ALIGO_TESTMODE = os.getenv("ALIGO_TESTMODE", "N")     # 테스트 모드면 "Y"


# =========================
# DB 연결
# =========================

def get_conn():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        dbname=DB_NAME,
    )


# =========================
# 테이블 생성 쿼리
# =========================

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    preferred_distance_km INTEGER DEFAULT 1.5,
    preferred_price_range VARCHAR(50),
    preferences_categories VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW()
);
"""

CREATE_RESTAURANTS_TABLE = """
CREATE TABLE IF NOT EXISTS restaurants (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100),
    address VARCHAR(255),
    lat DOUBLE PRECISION,
    lon DOUBLE PRECISION,
    rating DOUBLE PRECISION DEFAULT 0,
    num_reviews INTEGER DEFAULT 0
);
"""

CREATE_REVIEWS_TABLE = """
CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    restaurant_id INTEGER REFERENCES restaurants(id) ON DELETE CASCADE,
    review_text TEXT,
    rating INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

CREATE_USER_FEEDBACK_TABLE = """
CREATE TABLE IF NOT EXISTS user_feedback (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    restaurant_name VARCHAR(255) NOT NULL,
    category VARCHAR(100),
    rating INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);
"""

CREATE_RECOMMENDATION_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS recommendation_logs (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    restaurant_name VARCHAR(255) NOT NULL,
    time_of_day VARCHAR(10),
    created_at TIMESTAMP DEFAULT NOW()
);
"""

CREATE_CLICK_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS click_logs (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    restaurant_id INTEGER REFERENCES restaurants(id),
    restaurant_name VARCHAR(255),
    time_of_day VARCHAR(10),
    clicked_at TIMESTAMP DEFAULT NOW()
);
"""


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # 0) 유저 / 피드백 / 로그 / 리뷰 테이블은 일단 생성 (없으면)
    cur.execute(CREATE_USERS_TABLE)
    cur.execute(CREATE_USER_FEEDBACK_TABLE)
    cur.execute(CREATE_RECOMMENDATION_LOGS_TABLE)
    cur.execute(CREATE_CLICK_LOGS_TABLE)
    cur.execute(CREATE_REVIEWS_TABLE)

    # 1) restaurants 테이블은 조건 따지지 말고 항상 새로 만든다
    #    (지금은 데이터도 없고, 스키마 꼬인 상태라 강제 초기화가 제일 안전)
    cur.execute("DROP TABLE IF EXISTS restaurants CASCADE;")
    cur.execute(CREATE_RESTAURANTS_TABLE)

    # 2) restaurants (name, address) 유니크 인덱스 – upsert용
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurants_name_address
        ON restaurants (name, address);
    """)

    # 3) user_feedback에 추가 컬럼들 (없으면 추가)
    # 3-1) source 컬럼
    try:
        cur.execute("ALTER TABLE user_feedback ADD COLUMN source VARCHAR(50);")
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    # 3-2) comment 컬럼 (피드백 폼에서 쓰는 한 줄 후기)
    try:
        cur.execute("ALTER TABLE user_feedback ADD COLUMN comment TEXT;")
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    # 3-3) time_of_day 컬럼 (빠른 피드백 시간대 저장용)
    try:
        cur.execute("ALTER TABLE user_feedback ADD COLUMN time_of_day VARCHAR(10);")
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    # 4) recommendation_logs에 restaurant_id 컬럼 (없으면 추가)
    try:
        cur.execute("""
            ALTER TABLE recommendation_logs
            ADD COLUMN restaurant_id INTEGER REFERENCES restaurants(id);
        """)
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    # 5) user_feedback에도 restaurant_id 컬럼 (없으면 추가)
    try:
        cur.execute("""
            ALTER TABLE user_feedback
            ADD COLUMN restaurant_id INTEGER REFERENCES restaurants(id);
        """)
    except psycopg2.errors.DuplicateColumn:
        conn.rollback()

    conn.commit()
    cur.close()
    conn.close()


# =========================
# Kakao / Google 관련 상수
# =========================

CATEGORY_MAP = {
    "한식": ["한식", "백반", "국밥", "찌개", "국수", "분식"],
    "일식": ["일식", "초밥", "라멘", "돈카츠", "우동", "오마카세"],
    "중식": ["중식", "짜장", "짬뽕", "탕수육", "마라"],
    "양식": ["양식", "스테이크", "파스타", "피자", "버거"],
    "카페": ["카페", "디저트", "커피", "베이커리"],
    "주점": ["술집", "포차", "호프", "바", "펍"],
    "기타": []
}

GOOGLE_CATEGORY_KR = {
    # 한식/아시아
    "Korean Restaurant": "한식당",
    "Korean Barbecue Restaurant": "한식당/고기집",
    "Barbecue Restaurant": "바비큐/구이",
    "Asian Restaurant": "아시아 음식",
    "Korean Food": "한식당",

    # 일식
    "Japanese Restaurant": "일식당",
    "Sushi Restaurant": "초밥/스시",
    "Ramen Restaurant": "라멘/면요리",
    "Izakaya Restaurant": "이자카야",
    "Tempura Restaurant": "덴푸라/튀김",
    "Okonomiyaki Restaurant": "오코노미야키",
    "Japanese Curry Restaurant": "일식 카레",

    # 중식
    "Chinese Restaurant": "중식당",
    "Dim Sum Restaurant": "딤섬/중식당",
    "Szechuan Restaurant": "사천요리",

    # 양식/파스타
    "Italian Restaurant": "이탈리안",
    "Pizza Restaurant": "피자",
    "Pasta Restaurant": "파스타",
    "Steak House": "스테이크하우스",
    "European Restaurant": "유럽식 레스토랑",
    "French Restaurant": "프렌치 레스토랑",
    "Spanish Restaurant": "스페인 요리",

    # 패스트푸드/치킨/버거
    "Fast Food Restaurant": "패스트푸드",
    "Hamburger Restaurant": "버거",
    "Chicken Restaurant": "치킨",
    "Fried Chicken Restaurant": "치킨",

    # 카페/디저트
    "Cafe": "카페",
    "Coffee Shop": "카페",
    "Bakery": "베이커리",
    "Dessert Shop": "디저트",

    # 해산물/스시
    "Seafood Restaurant": "해산물요리",
    "Fish & Chips Restaurant": "생선요리",

    # 기타
    "Noodle Shop": "면요리",
    "Noodle Restaurant": "면요리",
    "Sandwich Shop": "샌드위치",
    "BBQ Restaurant": "바비큐/구이",
    "Buffet Restaurant": "뷔페",
    "Vegan Restaurant": "비건/채식",
    "Vegetarian Restaurant": "채식 식당",
    "Bar": "바/펍",
    "Pub": "펍",
    "Wine Bar": "와인바",
    "Beer Hall": "맥주집",

    # 가장 일반적인 표현
    "Restaurant": "음식점",
}

def translate_category_to_kr(en_cat: str) -> str:
    """
    Google Places에서 넘어오는 primaryTypeDisplayName(영문)을
    최대한 한글 카테고리로 변환한다.
    """
    if not en_cat:
        return ""

    # 1차: 사전 매핑
    if en_cat in GOOGLE_CATEGORY_KR:
        return GOOGLE_CATEGORY_KR[en_cat]

    lower = en_cat.lower()

    # 2차: 키워드 기반 대략 매핑
    if "sushi" in lower:
        return "초밥/스시"
    if "ramen" in lower:
        return "라멘/면요리"
    if "noodle" in lower:
        return "면요리"
    if "bbq" in lower or "barbecue" in lower:
        return "바비큐/구이"
    if "korean" in lower:
        return "한식당"
    if "japanese" in lower:
        return "일식당"
    if "chinese" in lower or "szechuan" in lower:
        return "중식당"
    if "pizza" in lower:
        return "피자"
    if "pasta" in lower:
        return "파스타"
    if "steak" in lower:
        return "스테이크하우스"
    if "chicken" in lower:
        return "치킨"
    if "burger" in lower or "hamburger" in lower:
        return "버거"
    if "cafe" in lower or "coffee" in lower:
        return "카페"
    if "seafood" in lower or "fish" in lower:
        return "해산물요리"
    if "buffet" in lower:
        return "뷔페"
    if "dessert" in lower or "bakery" in lower:
        return "디저트"
    if "bar" in lower or "pub" in lower:
        return "바/펍"

    # 남은 영어 카테고리는 그대로 두되, 'Restaurant'만 제거해서 노출
    if "restaurant" in lower:
        # 예: "Something Restaurant" -> "Something"
        cleaned = re.sub(r"[Rr]estaurant", "", en_cat).strip()
        return cleaned if cleaned else "음식점"

    return en_cat


# =========================
# Kakao API / Google Places API
# =========================

def kakao_keyword_search(query, x, y, radius=1500, size=3):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {
        "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"
    }
    params = {
        "query": query,
        "x": x,
        "y": y,
        "radius": radius,
        "size": size,
    }
    resp = requests.get(url, headers=headers, params=params, timeout=2)
    resp.raise_for_status()
    return resp.json()


def kakao_category_search(category_group_code, x, y, radius=1500, size=15):
    url = "https://dapi.kakao.com/v2/local/search/category.json"
    headers = {
        "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"
    }
    params = {
        "category_group_code": category_group_code,
        "x": x,
        "y": y,
        "radius": radius,
        "size": size,
        "sort": "distance"
    }
    resp = requests.get(url, headers=headers, params=params, timeout=5)
    resp.raise_for_status()
    return resp.json()


def match_kakao_place_by_location(name, lat, lon, radius=300):
    """
    Google Places에서 받은 가게 이름 + 좌표를 가지고
    카카오맵 place_id를 찾는다.

    1순위: 키워드 검색(query=정제된 이름, sort=distance)  - 카테고리 제한 X
    2순위: 주변 음식점(FD6) + 카페(CE7) 카테고리 검색
    """
    if not KAKAO_REST_API_KEY:
        return None, None, None

    # 1) 이름 정제: 너무 긴 이름, 파이프(|) 등 잘라주기
    clean_name = None
    if name:
        # '월화고기 상암점 | Sangam korean bbq restaurant | ...' 이런 형태 방지
        clean_name = re.split(r'[|ㆍ·\-]', str(name))[0].strip()
        # 너무 길면 Kakao가 400 던질 수 있으니 자르기 (안전하게 40자)
        if len(clean_name) > 40:
            clean_name = clean_name[:40]

    headers = {
        "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"
    }

    # ✅ 1) 이름 기반 키워드 검색 (카테고리 제한 없이 먼저 시도)
    if clean_name:
        url = "https://dapi.kakao.com/v2/local/search/keyword.json"
        params = {
            "query": clean_name,
            "x": lon,
            "y": lat,
            "radius": radius,
            "sort": "distance",
            # ❌ 기존: "category_group_code": "FD6"
            # → 삭제해서 FD6/CE7/기타 모두 검색되게
        }
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=2)
            resp.raise_for_status()
            data = resp.json()
            docs = data.get("documents", [])
            if docs:
                doc = docs[0]
                place_name = doc.get("place_name")
                place_id = doc.get("id")
                address = doc.get("road_address_name") or doc.get("address_name")
                return place_name, place_id, address
        except Exception as e:
            print("[KAKAO_MATCH_KEYWORD_ERROR]", e)

    # ✅ 2) 이름 기반 검색 실패 시, 주변 음식점(FD6) + 카페(CE7) 카테고리로 시도
    for cat in ("FD6", "CE7"):
        try:
            # 반경은 최소 300m 이상으로 넉넉하게
            cat_data = kakao_category_search(
                cat,
                x=lon,
                y=lat,
                radius=max(radius, 300),
                size=3,  # 여러 개 받아오되, kakao_category_search가 거리순 정렬
            )
            docs = cat_data.get("documents", [])
            if not docs:
                continue

            doc = docs[0]  # 가장 가까운 1개
            place_name = doc.get("place_name")
            place_id = doc.get("id")
            address = doc.get("road_address_name") or doc.get("address_name")
            return place_name, place_id, address
        except Exception as e:
            print(f"[KAKAO_MATCH_CATEGORY_ERROR_{cat}]", e)

    return None, None, None




def get_kakao_basic_info(place_id):
    url = "https://place.map.kakao.com/main/v/{place_id}".format(place_id=place_id)
    headers = {
        "Referer": "https://map.kakao.com/"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=2)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print("[KAKAO_BASIC_INFO_ERROR]", e)
        return None

    basic_info = data.get("basicInfo", {})
    addr = basic_info.get("address", {}).get("newAddr", "")
    open_info = basic_info.get("openInfo", {}).get("openInfo", "")

    is_open = None
    try:
        time_info = basic_info.get("openInfo", {})
        is_open = time_info.get("openFlag")
    except Exception:
        pass

    return {
        "address": addr,
        "open_info": open_info,
        "is_open": is_open,
    }


def search_google_places(lat, lon, radius_m=1500, max_results=20):
    """
    Google Places 'searchNearby'로 (lat, lon) 주변 음식점 목록을 가져온다.
    - 반환 형식: [
        {
          name, lat, lon, rating,
          address, open_info, category,
          photo_url, distance_km, reviews,
          user_rating_count, open_now, open_in_1h
        }, ...
      ]
    """
    if not GOOGLE_PLACES_API_KEY:
        print("⚠ GOOGLE_PLACES_API_KEY가 설정되어 있지 않습니다.")
        return []

    url = "https://places.googleapis.com/v1/places:searchNearby"

    field_mask = ",".join([
        "places.id",
        "places.displayName",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.shortFormattedAddress",
        "places.currentOpeningHours",
        "places.regularOpeningHours",
        "places.primaryTypeDisplayName",
        "places.photos",
        "places.reviews",
    ])

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": field_mask,
    }

    body = {
        "includedTypes": ["restaurant"],
        "maxResultCount": max_results,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": radius_m
            }
        }
    }

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=5)
        # 여기서 상태코드 먼저 확인
        if not resp.ok:
            print(
                "[GOOGLE_PLACES_ERROR]",
                "status=", resp.status_code,
                "body=", resp.text[:500]  # 길면 500자까지만
            )
            return []
        data = resp.json()
        raw_places = data.get("places", [])
    except Exception as e:
        print("[GOOGLE_PLACES_EXCEPTION]", e)
        return []

    results = []

    for p in raw_places:
        display_name = p.get("displayName", {})
        name = display_name.get("text", "")

        loc = p.get("location", {})
        plat = loc.get("latitude")
        plon = loc.get("longitude")

        rating = p.get("rating", 0.0)
        user_rating_count = p.get("userRatingCount", 0)
        address = p.get("shortFormattedAddress") or ""

        # ── 영업시간 / 영업 여부 (현재 + 1시간 뒤) ─────────────────
        open_info = ""
        open_now = None
        open_in_1h = None

        opening = p.get("currentOpeningHours") or p.get("regularOpeningHours")
        if opening:
            # 1) human readable 텍스트 (카드에 노출용)
            weekday_desc = opening.get("weekdayDescriptions") or []
            closed_days_en = []
            open_ranges = []

            for line in weekday_desc:
                if ":" in line:
                    day_part, rest = line.split(":", 1)
                    day_en = day_part.strip()
                    info = rest.strip()
                else:
                    day_en = ""
                    info = line.strip()

                if "Closed" in info or "closed" in info:
                    closed_days_en.append(day_en)
                else:
                    if info:
                        open_ranges.append(info)

            day_map = {
                "Monday": "월요일",
                "Tuesday": "화요일",
                "Wednesday": "수요일",
                "Thursday": "목요일",
                "Friday": "금요일",
                "Saturday": "토요일",
                "Sunday": "일요일",
            }
            if closed_days_en:
                closed_kr = ", ".join(day_map.get(d, d) for d in closed_days_en)
            else:
                closed_kr = "별도 휴무일 정보 없음"

            if open_ranges:
                hours_text = open_ranges[0]
            else:
                hours_text = "영업 시간 정보 없음"

            open_info = f"휴무 요일: {closed_kr}, 영업 시간: {hours_text}"

            # 2) periods 기반 현재/1시간 뒤 영업 여부 계산
            periods = opening.get("periods") or []
            # 한국 서비스용이라 KST(+9) 기준으로 계산
            now_kst = datetime.utcnow() + timedelta(hours=9)
            if periods:
                open_now = _is_open_at(periods, now_kst)
                open_in_1h = _is_open_at(periods, now_kst + timedelta(hours=1))

            # Google이 openNow도 주면 그대로 활용 (보정용)
            if open_now is None and "openNow" in opening:
                open_now = bool(opening.get("openNow"))

        # ───────────────────────────────────────────────────────────

        raw_cat = p.get("primaryTypeDisplayName") or ""
        if isinstance(raw_cat, dict):
            en_cat = raw_cat.get("text", "")
        else:
            en_cat = str(raw_cat) if raw_cat is not None else ""
        category = translate_category_to_kr(en_cat)

        # 사진 여러 장
        photos = p.get("photos") or []
        photo_urls = []
        for ph in photos[:5]:
            photo_name = ph.get("name")
            if not photo_name:
                continue
            url = (
                f"https://places.googleapis.com/v1/{photo_name}/media"
                f"?maxWidthPx=400&maxHeightPx=300&key={GOOGLE_PLACES_API_KEY}"
            )
            photo_urls.append(url)

        photo_url = photo_urls[0] if photo_urls else None

        reviews_raw = p.get("reviews") or []
        reviews = []
        for rv in reviews_raw:
            text_info = rv.get("text", {})
            txt = text_info.get("text", "")
            if txt:
                reviews.append(txt)

        dist_km = 0.0
        try:
            if plat is not None and plon is not None:
                dist_km = calculate_distance(lat, lon, plat, plon)
        except Exception:
            dist_km = 0.0

        results.append(
            {
                "name": name,
                "lat": plat,
                "lon": plon,
                "rating": rating,
                "address": address,
                "open_info": open_info,
                "category": category,
                "photo_url": photo_url,
                "photo_urls": photo_urls,
                "distance_km": dist_km,
                "reviews": reviews,
                "user_rating_count": user_rating_count,
                "open_now": open_now,
                "open_in_1h": open_in_1h,
            }
        )

    return results


# ================== ALIGO 공통 유틸 =========================

def get_aligo_token():
    """알리고 토큰 발급"""
    if not ALIGO_API_KEY or not ALIGO_USER_ID:
        app.logger.error("[ALIGO] APIKEY / USERID 미설정")
        return None

    url = "https://kakaoapi.aligo.in/akv10/token/create/30/s/"
    data = {
        "apikey": ALIGO_API_KEY,
        "userid": ALIGO_USER_ID,
    }

    try:
        r = requests.post(url, data=data, timeout=5)
        r.raise_for_status()
        js = r.json()
    except Exception as e:
        app.logger.exception("[ALIGO] token 요청 실패: %s", e)
        return None

    if js.get("code") != 0:
        app.logger.error("[ALIGO] token 발급 실패: %s", js)
        return None

    return js.get("token")


def send_alimtalk(
    template_code,
    receiver,
    subject,
    message,
    btn_mobile_url=None,
    btn_pc_url=None,
    btn_name="자세히 보기",
    button_json=None,
    emtitle=None,   # ✅ 강조표기 템플릿용
):
    """알리고 알림톡 공통 발송 함수"""
    token = get_aligo_token()
    if not token:
        return False, {"msg": "TOKEN_ERROR"}

    if not (ALIGO_SENDER_KEY and ALIGO_SENDER):
        app.logger.error("[ALIGO] SENDER_KEY / SENDER 미설정")
        return False, {"msg": "CONFIG_ERROR"}

    url = "https://kakaoapi.aligo.in/akv10/alimtalk/send/"

    payload = {
        "apikey": ALIGO_API_KEY,
        "userid": ALIGO_USER_ID,
        "senderkey": ALIGO_SENDER_KEY,
        "token": token,
        "tpl_code": template_code,
        "sender": ALIGO_SENDER,
        "receiver_1": receiver,
        "subject_1": subject,   # 부제(자유롭게 써도 됨)
        "message_1": message,   # 템플릿 본문과 100% 일치해야 함
        "failover": "N",
    }

    # ✅ 강조표기 타이틀(템플릿에서 emtitle_1 로 지정된 부분)
    if emtitle:
        payload["emtitle_1"] = emtitle

    # 테스트 모드
    if ALIGO_TESTMODE.upper() == "Y":
        payload["testMode"] = "Y"

    # 1) 버튼 JSON 직접 지정 (AC 채널추가 등)
    if button_json is not None:
        payload["button_1"] = json.dumps(button_json, ensure_ascii=False)

    # 2) 버튼 JSON 없고, 웹링크 버튼 쓰는 경우 (reco/feedback)
    elif btn_mobile_url or btn_pc_url:
        mobile = btn_mobile_url or btn_pc_url
        pc = btn_pc_url or btn_mobile_url
        button_obj = {
            "button": [
                {
                    "name": btn_name,
                    "linkType": "WL",
                    "linkM": mobile,
                    "linkP": pc,
                }
            ]
        }
        payload["button_1"] = json.dumps(button_obj, ensure_ascii=False)

    try:
        r = requests.post(url, data=payload, timeout=5)
        r.raise_for_status()
        js = r.json()
    except Exception as e:
        app.logger.exception("[ALIGO] 발송 예외: %s", e)
        return False, {"msg": "EXCEPTION"}

    app.logger.info("[ALIGO] 발송 결과: %s", js)
    return js.get("code") == 0, js

# ================== 알림톡 3종 래퍼 =========================

def send_welcome_message(phone: str):
    """웰컴 알림톡 – 강조표기(emtitle_1)가 제목 역할"""
    if not phone:
        return False, {"msg": "NO_PHONE"}

    # subject_1 은 '부제'라 템플릿과 일치하지 않아도 됨.
    # 템플릿엔 없어도 됨. 안 쓰고 싶으면 "" 처리해도 OK.
    subject = ""

    # ✔ 템플릿의 강조표기 타이틀 (실제 제목)
    emtitle = "냠냠, 환영해요!"

    # ✔ 템플릿 본문 100% 동일하게
    message = (
        "냠냠이 서비스를 신청 해주셔서 감사 드립니다(축하)\n"
        "\n"
        "앞으로 고객님께서 신청하신 취향/시간대 별로 주변 맛집을 골라서 추천 드릴 예정입니다!\n"
        "\n"
        "우리 같이 맛있는 생활 해봐요. 냠냠(밥)"
    )

    # ✔ 채널추가(AC) 버튼 (버튼명 1글자도 틀리면 불일치)
    channel_add_button = {
        "button": [
            {
                "name": "채널추가",          # 붙여쓰기 매우 중요
                "linkType": "AC",
                "linkTypeName": "채널 추가"
            }
        ]
    }

    # send_alimtalk 내부는 emtitle_1 지원하도록 이미 구성돼 있어야 함
    return send_alimtalk(
        "UD_8456",        # 템플릿코드
        phone,
        subject,          # 부제 (템플릿 일치 검사 없음)
        message,          # 본문
        button_json=channel_add_button,
        emtitle=emtitle   # ← 강조표기 타이틀 전달
    )


def send_reco_message(phone: str, time_label: str):
    """2) 맛집 추천 알림톡 (템플릿코드 UD_8535)"""
    if not phone or not time_label:
        return False, {"msg": "PARAM_ERROR"}

    base_url = BASE_SERVER_URL.rstrip("/") if BASE_SERVER_URL else ""
    link = f"{base_url}/reco?phone={phone}&time={time_label}"

    # 템플릿 제목과 동일하게 맞춰 둔 subject
    subject = "오늘의 추천 맛집이 도착했어요"

    # ⚠ 여기서 더 이상 #{time}, #{phone_number}를 넣지 않고
    #    실제 값(time_label, phone)으로 치환해서 보냄
    message = (
        f"냠냠, 오늘의 추천 {time_label} 맛집이 도착했어요!(밥)\n"
        "\n"
        "오늘은 어떤 음식을 먹어 볼까요?\n"
    )

    return send_alimtalk(
        "UD_8535",
        phone,
        subject,
        message,
        btn_mobile_url=link,
        btn_pc_url=link,
        btn_name="과연 오늘의 밥은?",
    )


def send_feedback_message(phone: str, time_label: str):
    """3) 피드백 요청 알림톡 (템플릿코드 UD_8446)"""
    if not phone or not time_label:
        return False, {"msg": "PARAM_ERROR"}

    base_url = BASE_SERVER_URL.rstrip("/") if BASE_SERVER_URL else ""
    link = f"{base_url}/feedback-form?phone={phone}&time={time_label}"

    # subject_1 = 부제(템플릿 일치 검사 안 함) → 편한 문구나 빈 문자열 가능
    subject = ""

    # ✅ 템플릿의 '강조표기 타이틀(emtitle_1)' 과 1글자도 동일하게
    #    (알리고 템플릿 설정 화면에서 강조표기 문구 그대로 복사해서 넣어줘야 함)
    emtitle = "오늘 방문하신 맛집은 어떠셨나요?"

    # ✅ 템플릿 본문과 완전히 동일하게 (띄어쓰기/마침표/줄바꿈까지)
    # 아래 문장은 예시라서, 실제 템플릿에 등록한 본문이랑 다르면
    # 텍스트를 그대로 복붙해서 교체해줘야 함.
    message = (
        "피드백과 리뷰를 남겨 주시면 다음 추천때 냠냠이가 고객님의 취향에 알맞는 음식점을 잘 찾아드려요!"
    )

    # 버튼은 템플릿에서 WL + "피드백 남기러가기!" 로 등록되어 있음.
    # send_alimtalk 안에서 btn_* 인자를 받아 WL 버튼 JSON으로 변환해서 보냄.
    return send_alimtalk(
        "UD_8446",
        phone,
        subject,
        message,
        btn_mobile_url=link,
        btn_pc_url=link,
        btn_name="피드백 남기러가기!",
        emtitle=emtitle,  # ✅ 강조표기 파라미터로 보냄
    )



# =========================
# 유저 선호도 관련 함수
# =========================

def get_user_prefs(phone, cur):
    """
    user_feedback 테이블 기준으로, 이 사용자가
    카테고리별로 준 평균 rating 딕셔너리 반환.
    예: {"한식": 4.5, "양식": 3.0}
    """
    try:
        cur.execute(
            """
            SELECT category, AVG(rating)
            FROM user_feedback
            WHERE phone_number = %s
            GROUP BY category;
            """,
            (phone,),
        )
    except UndefinedColumn:
        return {}

    rows = cur.fetchall()
    prefs = {}
    for c, r in rows:
        if c:
            prefs[c] = float(r)
    return prefs


def get_user_prefs_by_time(phone, time_of_day, cur):
    """
    특정 시간대(time_of_day)에 대한 카테고리별 평균 rating.
    데이터가 없으면 전체(get_user_prefs) 기준으로 fallback.
    """
    if not time_of_day:
        return get_user_prefs(phone, cur)

    try:
        cur.execute(
            """
            SELECT category, AVG(rating)
            FROM user_feedback
            WHERE phone_number = %s
              AND time_of_day = %s
            GROUP BY category;
            """,
            (phone, time_of_day),
        )
    except UndefinedColumn:
        # time_of_day 컬럼이 없으면 기존 전체 선호도로 대체
        return get_user_prefs(phone, cur)

    rows = cur.fetchall()
    if not rows:
        return get_user_prefs(phone, cur)

    prefs = {}
    for c, r in rows:
        if c:
            prefs[c] = float(r)
    return prefs


def get_user_restaurant_prefs(phone, cur):
    """
    user_feedback 기준으로, 이 사용자가
    특정 가게별로 준 평균 rating 딕셔너리 반환.
    예: {"김영섭초밥": 1.0, "맛있는파스타": 4.8}
    """
    try:
        cur.execute(
            """
            SELECT restaurant_name, AVG(rating)
            FROM user_feedback
            WHERE phone_number = %s
            GROUP BY restaurant_name;
            """,
            (phone,),
        )
    except UndefinedColumn:
        return {}

    rows = cur.fetchall()
    prefs = {}
    for name, r in rows:
        if name:
            prefs[name] = float(r)
    return prefs


# =========================
# 거리 계산
# =========================

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    rlat1, rlon1, rlat2, rlon2 = map(
        math.radians, [lat1, lon1, lat2, lon2]
    )
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    rlat1, rlon1, rlat2, rlon2 = map(
        math.radians, [lat1, lon1, lat2, lon2]
    )
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _is_open_at(periods, dt):
    """
    Google Places currentOpeningHours.regularOpeningHours 의 periods 구조를 이용해서
    dt 시각에 영업 중인지 판단한다.

    periods 예시 (v1):
    [
      {
        "open": {"day": 0, "hour": 11, "minute": 0},
        "close": {"day": 0, "hour": 21, "minute": 0}
      },
      ...
    ]
    day: 0=월요일, 6=일요일 (문서 기준)
    """
    if not periods or not isinstance(periods, list):
        return None

    # dt를 "주 시작(월요일 00:00) 기준 분 단위" 로 변환
    # (서버는 UTC 기준, 한국 식당만 쓴다 가정하고 KST(+9)로 보정해서 search_google_places 쪽에서 넘김)
    week_minutes = dt.weekday() * 1440 + dt.hour * 60 + dt.minute

    for period in periods:
        open_info = period.get("open") or {}
        if "day" not in open_info or "hour" not in open_info or "minute" not in open_info:
            continue

        o_day = open_info["day"]
        o_hour = open_info["hour"]
        o_minute = open_info["minute"]
        open_min = o_day * 1440 + o_hour * 60 + o_minute

        close_info = period.get("close")
        if close_info and "hour" in close_info and "minute" in close_info:
            c_day = close_info.get("day", o_day)
            c_hour = close_info["hour"]
            c_minute = close_info["minute"]
            close_min = c_day * 1440 + c_hour * 60 + c_minute
        else:
            # 닫힘 정보 없으면 24시간 영업으로 간주 (해당 요일 기준)
            close_min = open_min + 24 * 60

        # 영업 시간이 다음 주로 넘어가는 케이스(심야 영업 등) 처리
        if close_min <= open_min:
            close_min += 7 * 1440

        # 주 단위로 한 번, +7일 뒤로 한 번 검사해서 wrap-around 처리
        for base in (week_minutes, week_minutes + 7 * 1440):
            if open_min <= base < close_min:
                return True

    return False

import re

# =========================
# 리뷰 분석 / 대표메뉴 / 요약
# =========================

def extract_menu_from_review(review_text):
    """
    리뷰 텍스트 안에서 '대표 메뉴' 후보를 뽑는다.
    - 한글 메뉴 키워드
    - 영어 메뉴 키워드 → 한글 매핑
    - 딘타이펑(만두/딤섬), 양국(양고기) 케이스 강화
    """
    if not review_text:
        return []

    text = str(review_text)

    # 1) 한글 메뉴 키워드
    menu_keywords = [
        # 한식/분식
        "김치찌개", "된장찌개", "불고기", "삼겹살", "갈비",
        "냉면", "비빔밥", "떡볶이", "라볶이", "튀김", "순대", "김밥",
        "칼국수", "국수",

        # 일식
        "초밥", "스시", "라멘", "우동", "돈카츠", "텐동",

        # 중식 + 딤섬 계열 (딘타이펑 대응)
        "짜장면", "짬뽕", "탕수육", "마라탕",
        "만두", "샤오롱바오", "소롱포", "딤섬",

        # 양고기 계열 (양국 등)
        "양고기", "양꼬치", "양갈비",

        # 양식/기타
        "파스타", "피자", "리조또", "스테이크",
        "치킨", "버거", "뷔페",
    ]

    found = []
    for kw in menu_keywords:
        if kw in text:
            found.append(kw)

    # 2) 영어 메뉴 키워드 → 한글 매핑
    lower = text.lower()
    eng_map = {
        "sushi": "초밥",
        "ramen": "라멘",
        "udon": "우동",
        "pasta": "파스타",
        "pizza": "피자",
        "steak": "스테이크",
        "bbq": "바비큐",
        "barbecue": "바비큐",
        "burger": "버거",
        "sandwich": "샌드위치",
        "chicken": "치킨",
        "noodle": "면요리",
        "noodles": "면요리",
        "curry": "카레",
        "coffee": "커피",
        "buffet": "뷔페",

        # 딤섬/만두 계열 (딘타이펑)
        "dumpling": "만두",
        "dumplings": "만두",
        "xiao long bao": "샤오롱바오",
        "xiaolongbao": "샤오롱바오",
        "xialongbao": "샤오롱바오",
        "dim sum": "딤섬",
        "dimsum": "딤섬",

        # 양고기 계열 (양국)
        "lamb": "양고기",
        "mutton": "양고기",
    }

    for eng, kor in eng_map.items():
        if eng in lower:
            found.append(kor)

    # 3) 중복 제거 + 너무 많으면 상위만 사용
    found_unique = list(dict.fromkeys(found))
    return found_unique[:4]




def _normalize_category_kr(category: str | None) -> str | None:
    """
    카테고리가 영어(diner, Sushi Restaurant 등)이면
    화면에는 '음식점' 정도로만 보여주고,
    한글이 포함돼 있으면 그대로 사용.
    """
    if not category:
        return None

    # 한글이 하나라도 있으면 그대로 사용
    if re.search(r"[가-힣]", category):
        return category

    # 혹시 모를 대표적인 영어 카테고리들 매핑
    mapping = {
        "Sushi Restaurant": "초밥집",
        "Korean Restaurant": "한식",
        "Japanese Restaurant": "일식",
        "Chinese Restaurant": "중식",
        "Barbecue Restaurant": "바비큐",
        "Diner": "음식점",
        "Restaurant": "음식점",
    }
    return mapping.get(category, "음식점")


def build_menu_text(name, category):
    cat = _normalize_category_kr(category)
    # '음식점' 같이 포괄적이면 가게 이름 기준 문구
    if cat and cat != "음식점":
        return f"{cat} 위주의 인기 메뉴를 즐길 수 있는 곳이에요."
    else:
        return f"{name}만의 인기 메뉴를 즐길 수 있는 곳이에요."


def build_summary_text(name, category, rating, distance_km):
    rating_text = "평균 이상 좋은 평점" if rating and rating >= 4.0 else "무난한 평점"
    distance_text = (
        "현재 위치와 매우 가까워"
        if distance_km is not None and distance_km <= 0.5
        else "주변에서"
    )

    cat = _normalize_category_kr(category) or "이 곳"

    return (
        f"{name}은(는) {cat} 메뉴를 즐길 수 있는 곳입니다. "
        f"{rating_text}을 받고 있으며, {distance_text} 가볍게 방문하기 좋습니다."
    )


def build_keywords(category, rating, distance_km, preferred=False, review_text=""):
    tags = []
    if distance_km is not None:
        if distance_km <= 0.3:
            tags.append("#도보5분이내")
        elif distance_km <= 1.0:
            tags.append("#도보10~15분이내")
        else:
            tags.append("#차로가기좋은")

    if rating is not None:
        if rating >= 4.5:
            tags.append("#평점매우좋은")
        elif rating >= 4.0:
            tags.append("#평점좋은")
        else:
            tags.append("#무난한평점")

    if category:
        tags.append(f"#{category}")

    if preferred:
        tags.append("#내취향저격")

    if "점심" in review_text or "런치" in review_text:
        tags.append("#점심메뉴")
    if "저녁" in review_text or "디너" in review_text:
        tags.append("#저녁메뉴")

    return tags


# =========================
# Flask 페이지 라우트
# =========================

@app.route("/")
def index():
    phone = request.args.get("phone", "").strip()
    time_of_day = request.args.get("time", "").strip()
    return redirect(f"/reco?phone={phone}&time={time_of_day}")


@app.route("/signup")
def signup():
    html = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>내 위치·취향 기반 맛집 알림 서비스, 냠냠이!</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">

  <link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">

  <style>
    * {
      box-sizing: border-box;
    }

    body {
      font-family: "Noto Sans KR", sans-serif;
      background: linear-gradient(180deg, #ffeaf5, #e3f0ff);
      margin: 0;
      padding: 12px;
      display: flex;
      justify-content: center;
      align-items: flex-start;
      min-height: 100vh;
    }

    .wrap {
      width: 100%;
      max-width: 480px;
      background: #ffffff;
      padding: 24px 18px 26px;
      border-radius: 20px;
      box-shadow: 0 16px 45px rgba(0, 0, 0, 0.08);
      text-align: center;
    }

    .logo {
      width: 70px;
      margin: 0 auto 8px;
      display: block;
    }

    h1 {
      font-size: 20px;
      margin-bottom: 6px;
      word-break: keep-all;
      line-height: 1.4;
    }

    .subtitle {
      font-size: 13px;
      color: #666;
      margin-bottom: 16px;
      line-height: 1.6;
      word-break: keep-all;
    }

    .phone-block {
      margin: 14px 0 12px;
      text-align: left;
    }

    .phone-label {
      font-size: 13px;
      color: #555;
      margin-left: 4px;
      margin-bottom: 4px;
    }

    .phone-input {
      width: 100%;
      display: block;
      padding: 11px 14px;
      border-radius: 999px;
      border: 1px solid #ddd;
      font-size: 15px;
      text-align: center;
      background: #fafafa;
    }

    .section-title {
      font-size: 14px;
      font-weight: 600;
      margin: 14px 0 6px;
      text-align: left;
    }

    .chips-row {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-start;
      gap: 8px;
      margin-bottom: 4px;
    }

    .chip {
      flex: 0 1 calc(50% - 8px);   /* 모바일에서 두 줄 정렬 */
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 7px 10px;
      border-radius: 999px;
      border: 1px solid #ddd;
      font-size: 13px;
      cursor: pointer;
      background: #fafafa;
      white-space: nowrap;
    }

    .chip input {
      margin: 0;
    }

    .chip span {
      padding-top: 1px;
    }

    .btn {
      width: 100%;
      margin: 8px auto 0;
      display: block;
      padding: 11px 0;
      border-radius: 999px;
      border: none;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
    }

    .btn-location {
      background: #f4f4f4;
      color: #333;
      margin-top: 10px;
    }

    .btn-submit {
      background: #ff6b81;
      color: white;
      margin-top: 12px;
    }

    #status {
      font-size: 12px;
      color: #333;
      margin-top: 6px;
      text-align: center;
      word-break: keep-all;
    }

    .location-help {
      font-size: 12px;
      color: #777;
      margin-top: 8px;
      line-height: 1.5;
      word-break: keep-all;
      text-align: left;
    }

    .agreements {
      width: 100%;
      margin: 12px auto 0;
      font-size: 11px;
      color: #777;
      text-align: left;
      line-height: 1.5;
    }

    .agreements label {
      display: inline-flex;
      align-items: flex-start;
      gap: 6px;
      margin-top: 4px;
    }

    .agreements input {
      margin-top: 2px;
    }

    .agreements a {
      color: #555;
      text-decoration: underline;
      cursor: pointer;
    }

    .footer {
      margin-top: 14px;
      font-size: 11px;
      color: #999;
      text-align: center;
      word-break: keep-all;
    }

    /* 약관 모달 */
    .modal-terms {
      position: fixed;
      inset: 0;
      display: none;
      justify-content: center;
      align-items: center;
      z-index: 999;
    }

    .modal-terms-backdrop {
      position: absolute;
      inset: 0;
      background: rgba(0, 0, 0, 0.45);
    }

    .modal-terms-content {
      position: relative;
      background: #fff;
      width: 90%;
      max-width: 420px;
      max-height: 80vh;
      border-radius: 18px;
      padding: 18px 16px 14px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
      overflow-y: auto;
      font-size: 12px;
      text-align: left;
    }

    .modal-terms-content h3 {
      font-size: 14px;
      margin-top: 0;
      margin-bottom: 8px;
      text-align: center;
    }

    .modal-terms-content h4 {
      font-size: 13px;
      margin-bottom: 4px;
    }

    .modal-terms-body {
      font-size: 12px;
      line-height: 1.6;
      text-align: left;
      word-break: keep-all;
    }

    .modal-terms-close {
      margin-top: 10px;
      width: 100%;
      padding: 8px 0;
      border-radius: 999px;
      border: none;
      background: #ff6b81;
      color: #fff;
      font-size: 13px;
      cursor: pointer;
    }

    /* 데스크탑에서 chip 폭 살짝 줄이기 */
    @media (min-width: 480px) {
      .chip {
        flex: 0 0 auto;
      }
      .section-title {
        text-align: center;
      }
      .location-help {
        text-align: center;
      }
    }
  </style>
</head>
<body>

<div class="wrap">
  <img src="/static/logo.png" class="logo" alt="냠냠이 로고">

  <h1>내 위치·취향 기반 맛집 알림 서비스, 냠냠이!</h1>
  <p class="subtitle">
    고객님이 선택하신 선호 음식과 현재 위치를 기반으로,<br>
    아침·점심·저녁·야식 시간에 맞춰 주변 맛집을 카카오톡으로 보내드립니다.
  </p>

  <div class="phone-block">
    <div class="phone-label">휴대폰 번호</div>
    <input type="text" id="phone" class="phone-input" placeholder="'-' 없이 숫자만 입력">
  </div>

  <div class="section-title">선호하는 음식 종류</div>
  <div class="chips-row">
    <label class="chip">
      <input type="checkbox" name="category" value="한식"><span>한식</span>
    </label>
    <label class="chip">
      <input type="checkbox" name="category" value="중식"><span>중식</span>
    </label>
    <label class="chip">
      <input type="checkbox" name="category" value="일식"><span>일식</span>
    </label>
    <label class="chip">
      <input type="checkbox" name="category" value="양식"><span>양식</span>
    </label>
    <label class="chip">
      <input type="checkbox" name="category" value="분식"><span>분식</span>
    </label>
  </div>

  <div class="section-title">알림 받고 싶은 시간대</div>
  <div class="chips-row">
    <label class="chip">
      <input type="checkbox" name="alert" value="아침"><span>아침(08시)</span>
    </label>
    <label class="chip">
      <input type="checkbox" name="alert" value="점심"><span>점심(11시)</span>
    </label>
    <label class="chip">
      <input type="checkbox" name="alert" value="저녁"><span>저녁(17시)</span>
    </label>
    <label class="chip">
      <input type="checkbox" name="alert" value="야식"><span>야식(21시)</span>
    </label>
  </div>

  <button class="btn btn-location" onclick="getLocation()">📍 현재 위치 설정</button>

  <p class="location-help">
    기본적으로 현재 위치를 기반으로 주변 맛집을 추천 드리며,<br>
    현재 위치를 받지 못할 경우, 신청 시 설정한 위치 기반 주변 맛집 안내를 발송 드립니다.
  </p>

  <div id="status">아직 위치가 설정되지 않았습니다.</div>

  <div class="agreements">
    <label>
      <input type="checkbox" id="agree-service">
      <span>서비스 이용 약관 및 개인정보 수집·이용에 동의합니다. (<a onclick="openTerms()">내용 보기</a>)</span>
    </label>
  </div>

  <button class="btn btn-submit" onclick="submitForm()">신청하기</button>

  <div class="footer">
    운영: 돕힝연구소 · 대표자: 김신혁 · 본 서비스는 테스트용 베타 서비스입니다.
  </div>
</div>

<div id="terms-modal" class="modal-terms">
  <div class="modal-terms-backdrop" onclick="closeTerms()"></div>
  <div class="modal-terms-content">
    <h3>서비스 이용 약관 및 개인정보 수집·이용 동의</h3>
    <div class="modal-terms-body">
      <h4>1. 서비스 개요</h4>
      <p>
        본 서비스는 이용자가 선택한 선호 음식과 설정한 위치를 바탕으로,
        지정한 시간대에 주변 음식점을 추천하여 카카오톡으로 안내하는 알림 서비스입니다.
      </p>

      <h4>2. 수집 항목</h4>
      <ul>
        <li>휴대폰 번호</li>
        <li>위치 정보(위도·경도)</li>
        <li>선호 음식 종류, 알림 희망 시간대</li>
        <li>서비스 이용 기록 및 선택적으로 제출한 이용 후기</li>
      </ul>

      <h4>3. 이용 목적</h4>
      <ul>
        <li>시간대별 맞춤형 맛집 추천 알림 발송</li>
        <li>추천 품질 개선을 위한 통계·분석</li>
        <li>서비스 이용 내역 확인 및 문의 대응</li>
      </ul>

      <h4>4. 보관 및 파기</h4>
      <p>
        수집된 정보는 서비스 제공 기간 동안 보관되며,
        이용자가 서비스 탈퇴 또는 삭제를 요청하는 경우 지체 없이 파기합니다.
        관련 법령에서 별도의 보관 기간을 정한 경우 해당 기간 동안만 보관합니다.
      </p>

      <h4>5. 제3자 제공 및 위탁</h4>
      <p>
        법령상 요구되거나 이용자의 별도 동의가 있는 경우를 제외하고,
        제3자에게 개인정보를 제공하지 않으며 필수적인 시스템 운영을 위해
        일부 업무를 외부 서비스(예: 카카오 알림 발송 대행사)에 위탁할 수 있습니다.
      </p>

      <h4>6. 동의 거부 권리</h4>
      <p>
        이용자는 개인정보 수집·이용에 대한 동의를 거부할 권리가 있으며,
        다만 이 경우 서비스 이용(맛집 알림 제공)이 제한될 수 있습니다.
      </p>

      <p style="margin-top:10px; font-size:11px; color:#999;">
        * 본 약관 및 안내문은 일반적인 예시이며, 실제 상용 서비스 운영 시에는
        별도의 법률 검토가 필요할 수 있습니다.
      </p>
    </div>
    <button type="button" class="modal-terms-close" onclick="closeTerms()">닫기</button>
  </div>
</div>

<script>
  let currentLat = null;
  let currentLon = null;

  function getLocation() {
    if (!navigator.geolocation) {
      document.getElementById("status").innerText = "⚠ 브라우저에서 위치 정보를 지원하지 않습니다.";
      return;
    }

    document.getElementById("status").innerText = "위치를 불러오는 중입니다...";

    navigator.geolocation.getCurrentPosition(
      function(pos) {
        currentLat = pos.coords.latitude;
        currentLon = pos.coords.longitude;
        document.getElementById("status").innerText = "✅ 현재 위치 설정 완료";
      },
      function(err) {
        document.getElementById("status").innerText = "위치 정보를 가져오지 못했습니다. 다시 시도해주세요.";
      }
    );
  }

  function openTerms() {
    const m = document.getElementById("terms-modal");
    if (m) m.style.display = "flex";
  }

  function closeTerms() {
    const m = document.getElementById("terms-modal");
    if (m) m.style.display = "none";
  }

  function submitForm() {
    const phone = document.getElementById("phone").value.trim();

    if (!phone) {
      alert("휴대폰 번호를 입력해주세요.");
      return;
    }

    if (!/^[0-9]+$/.test(phone)) {
      alert("휴대폰 번호는 '-' 없이 숫자만 입력해주세요.");
      return;
    }

    if (!currentLat || !currentLon) {
      alert("먼저 '현재 위치 설정' 버튼을 눌러 위치를 설정해주세요.");
      return;
    }
    if (!document.getElementById("agree-service").checked) {
      alert("서비스 이용 약관에 동의해주세요.");
      return;
    }

    const categoryEls = document.querySelectorAll("input[name='category']:checked");
    const alertEls = document.querySelectorAll("input[name='alert']:checked");

    const categories = Array.from(categoryEls).map(el => el.value);
    const alertTimes = Array.from(alertEls).map(el => el.value);

    fetch("/register", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        phone_number: phone,
        latitude: currentLat,
        longitude: currentLon,
        preferences_categories: categories,
        preferences_focus: "맛",
        alert_times: alertTimes
      })
    })
    .then(res => res.json())
    .then(data => {
      if (data.success) {
        alert("✅ 신청이 완료되었습니다! 선택하신 시간대에 맞춰 맛집을 보내드릴게요.");
      } else {
        alert("오류: " + (data.message || "알 수 없는 오류"));
      }
    })
    .catch(err => {
      alert("요청 중 오류가 발생했습니다.");
    });
  }
</script>

</body>
</html>
"""
    return render_template_string(html)


@app.route("/reco")
def reco_page():
    phone = (request.args.get("phone") or "").strip()
    time_of_day = (request.args.get("time") or "").strip()

    signup_lat = None
    signup_lon = None
    has_signup_location = False

    if phone:
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT latitude, longitude FROM users WHERE phone_number = %s",
                (phone,),
            )
            row = cur.fetchone()
            if row and row[0] is not None and row[1] is not None:
                signup_lat, signup_lon = float(row[0]), float(row[1])
                has_signup_location = True
            cur.close()
            conn.close()
        except Exception as e:
            print("[RECO_PAGE_USER_LOCATION_ERR]", e)

    return render_template(
        "reco.html",
        phone=phone,
        time=time_of_day,
        signup_lat=signup_lat,
        signup_lon=signup_lon,
        has_signup_location=has_signup_location,
    )


# =========================
# 관리자 페이지
# =========================

@app.route("/admin")
def admin_dashboard():
    key = request.args.get("key", "")
    if key != ADMIN_PASSWORD:
        return "UNAUTHORIZED", 403

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users;")
    total_users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE is_active = TRUE;")
    active_users = cur.fetchone()[0]

    try:
        cur.execute("""
            SELECT COUNT(*) 
            FROM recommendation_logs
            WHERE created_at::date = CURRENT_DATE;
        """)
        today_reco = cur.fetchone()[0]
    except Exception:
        conn.rollback()
        cur.execute("SELECT COUNT(*) FROM recommendation_logs;")
        today_reco = cur.fetchone()[0]

    try:
        cur.execute("SELECT COUNT(*) FROM user_feedback;")
        feedback_count = cur.fetchone()[0]
    except Exception:
        conn.rollback()
        feedback_count = 0

    try:
        cur.execute("""
            SELECT category, AVG(rating) AS avg_rating, COUNT(*) AS cnt
            FROM user_feedback
            GROUP BY category
            ORDER BY avg_rating DESC, cnt DESC
            LIMIT 5;
        """)
        category_stats = cur.fetchall()
    except Exception:
        conn.rollback()
        category_stats = []

    try:
        cur.execute("""
            SELECT phone_number, latitude, longitude, alert_times, created_at, is_active
            FROM users
            ORDER BY created_at DESC
            LIMIT 10;
        """)
        recent_users = cur.fetchall()
    except Exception:
        conn.rollback()
        cur.execute("""
            SELECT phone_number, latitude, longitude, alert_times, is_active
            FROM users
            ORDER BY phone_number DESC
            LIMIT 10;
        """)
        rows = cur.fetchall()
        recent_users = [(r[0], r[1], r[2], r[3], None, r[4]) for r in rows]

    try:
        cur.execute("""
            SELECT phone_number, restaurant_name, category, rating, comment, created_at
            FROM user_feedback
            ORDER BY created_at DESC
            LIMIT 10;
        """)
        recent_feedback = cur.fetchall()
    except Exception:
        conn.rollback()
        try:
            cur.execute("""
                SELECT phone_number, restaurant_name, category, rating
                FROM user_feedback
                ORDER BY id DESC
                LIMIT 10;
            """)
            rows = cur.fetchall()
            recent_feedback = [(r[0], r[1], r[2], r[3], None) for r in rows]
        except Exception:
            recent_feedback = []

    conn.close()

    html = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>냠냠이 관리자 대시보드</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f6f7fb;
  margin: 0;
  padding: 0;
}
h1 { margin-top:0; }
.cards {
  display:flex;
  flex-wrap:wrap;
  gap:12px;
  margin-bottom:20px;
}
page-title {
  width: 100%;
  max-width: 520px;
  padding: 0 24px;
  margin: 8px auto 18px;
  text-align: center;
  font-size: 35px;   /* 크게 */
  font-weight: 700;
  color: #222;
  display: none;     /* 로딩 끝나고 JS에서 보이게 */
}
.card {
  background:white;
  padding:10px 14px;
  border-radius:10px;
  box-shadow:0 2px 8px rgba(0,0,0,0.05);
  min-width:150px;
}
.card-title {
  font-size:12px;
  color:#777;
  margin-bottom:4px;
}
.card-value {
  font-size:18px;
  font-weight:700;
}
.section-title {
  margin-top:20px;
  margin-bottom:8px;
  font-size:15px;
  font-weight:600;
}
table {
  width:100%;
  border-collapse: collapse;
  background:white;
  border-radius:10px;
  overflow:hidden;
  box-shadow:0 2px 8px rgba(0,0,0,0.05);
  margin-bottom:16px;
}
th, td {
  padding:8px 10px;
  border-bottom:1px solid #eee;
  font-size:12px;
}
th {
  background:#fafafa;
}
tr:last-child td {
  border-bottom:none;
}
.small {
  font-size:11px;
  color:#999;
}
</style>
</head>
<body>

<h1>냠냠이 관리자 대시보드</h1>
<p class="small">내부용 통계 페이지입니다. URL과 key는 외부에 공유하지 마세요.</p>

<p class="small">
  👉 <a href="/admin/restaurants?key={{ admin_key }}">[식당 DB 탭으로 이동]</a>
</p>


<div class="cards">
  <div class="card">
    <div class="card-title">전체 가입자 수</div>
    <div class="card-value">{{ total_users }}</div>
  </div>
  <div class="card">
    <div class="card-title">활성 사용자 수</div>
    <div class="card-value">{{ active_users }}</div>
  </div>
  <div class="card">
    <div class="card-title">오늘 발송된 추천 수</div>
    <div class="card-value">{{ today_reco }}</div>
  </div>
  <div class="card">
    <div class="card-title">누적 피드백 수</div>
    <div class="card-value">{{ feedback_count }}</div>
  </div>
</div>

<div class="section-title">카테고리별 평균 평점 TOP5</div>
<table>
  <tr>
    <th>카테고리</th>
    <th>평균 평점</th>
    <th>피드백 수</th>
  </tr>
  {% for c, avg, cnt in category_stats %}
  <tr>
    <td>{{ c }}</td>
    <td>{{ "%.2f"|format(avg) }}</td>
    <td>{{ cnt }}</td>
  </tr>
  {% endfor %}
  {% if not category_stats %}
  <tr><td colspan="3">아직 피드백이 없습니다.</td></tr>
  {% endif %}
</table>

<div class="section-title">최근 가입자 10명</div>
<table>
  <tr>
    <th>전화번호</th>
    <th>위도</th>
    <th>경도</th>
    <th>알림시간</th>
    <th>가입일시</th>
    <th>활성</th>
    <th>관리</th>
  </tr>
  {% for row in recent_users %}
  <tr>
    <td>{{ row[0] }}</td>
    <td>{{ row[1] }}</td>
    <td>{{ row[2] }}</td>
    <td>{{ row[3] }}</td>
    <td>{{ row[4] if row[4] else "-" }}</td>
    <td>{{ 'ON' if row[5] else 'OFF' }}</td>
    <td>
      <form method="POST" action="/admin/users/update?key={{ admin_key }}" style="margin-bottom:4px; font-size:11px;">
        <input type="hidden" name="phone_number" value="{{ row[0] }}">
        <input type="text" name="latitude"  value="{{ row[1] }}" style="width:80px; font-size:11px;" placeholder="위도">
        <input type="text" name="longitude" value="{{ row[2] }}" style="width:80px; font-size:11px;" placeholder="경도">
        <input type="text" name="alert_times" value="{{ row[3] or '' }}" placeholder="예: 아침,점심" style="width:120px; font-size:11px;">
        <label style="font-size:11px;">
          <input type="checkbox" name="is_active" {% if row[5] %}checked{% endif %}> 활성
        </label>
        <button type="submit" style="font-size:11px;">수정</button>
      </form>

      <form method="POST"
            action="/admin/users/delete?key={{ admin_key }}"
            onsubmit="return confirm('정말 이 회원과 관련 데이터를 모두 삭제할까요?');"
            style="font-size:11px;">
        <input type="hidden" name="phone_number" value="{{ row[0] }}">
        <button type="submit" style="font-size:11px; color:#c00;">삭제</button>
      </form>
    </td>
  </tr>
  {% endfor %}
  {% if not recent_users %}
  <tr><td colspan="7">가입자가 없습니다.</td></tr>
  {% endif %}
</table>

<div class="section-title">최근 피드백 10개</div>
<table>
  <tr>
    <th>전화번호</th>
    <th>가게명</th>
    <th>카테고리</th>
    <th>평점</th>
    <th>댓글</th>
    <th>작성일시</th>
  </tr>
  {% for row in recent_feedback %}
  <tr>
    <td>{{ row[0] }}</td>
    <td>{{ row[1] }}</td>
    <td>{{ row[2] }}</td>
    <td>{{ row[3] }}</td>
    <td style="max-width:200px; white-space:normal;">
      {{ row[4] if row[4] else '-' }}
    </td>
    <td>{{ row[5] if row[5] else '-' }}</td>
  </tr>
  {% endfor %}
  {% if not recent_feedback %}
  <tr><td colspan="6">피드백이 없습니다.</td></tr>
  {% endif %}
</table>


</body>
</html>
"""
    return render_template_string(
        html,
        total_users=total_users,
        active_users=active_users,
        today_reco=today_reco,
        feedback_count=feedback_count,
        category_stats=category_stats,
        recent_users=recent_users,
        recent_feedback=recent_feedback,
        admin_key=key,)

@app.route("/admin/restaurants")
def admin_restaurants():
    """
    restaurants 테이블 조회용 관리자 페이지.
    URL 예시:
      /admin/restaurants?key=관리자비밀번호
    """
    key = request.args.get("key", "")
    if key != ADMIN_PASSWORD:
        return "UNAUTHORIZED", 403

    conn = get_conn()
    cur = conn.cursor()

    # 최근 저장된 식당 100개 (id 역순)
    cur.execute(
        """
        SELECT id, name, category, address, lat, lon, rating, num_reviews
        FROM restaurants
        ORDER BY id DESC
        LIMIT 100;
        """
    )
    rows = cur.fetchall()
    conn.close()

    html = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>냠냠이 – 식당 DB</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #f6f7fb;
  margin: 0;
  padding: 16px;
}
h1 {
  margin-top: 0;
}
.small {
  font-size: 12px;
  color: #777;
  margin-bottom: 10px;
}
a {
  color: #3366cc;
  text-decoration: none;
}
a:hover {
  text-decoration: underline;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: white;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 2px 8px rgba(0,0,0,0.05);
}
th, td {
  padding: 6px 8px;
  border-bottom: 1px solid #eee;
  font-size: 12px;
}
th {
  background: #fafafa;
  text-align: left;
}
tr:last-child td {
  border-bottom: none;
}
td.numeric {
  text-align: right;
}
</style>
</head>
<body>

<h1>식당 DB (restaurants)</h1>
<p class="small">
  최근 저장된 식당 100개만 보여줍니다.<br>
  🔙 <a href="/admin?key={{ admin_key }}">[요약 대시보드로 돌아가기]</a>
</p>

<table>
  <tr>
    <th>ID</th>
    <th>이름</th>
    <th>카테고리</th>
    <th>주소</th>
    <th>위도</th>
    <th>경도</th>
    <th>평점</th>
    <th>리뷰수</th>
  </tr>
  {% if not rows %}
  <tr>
    <td colspan="8">아직 저장된 식당 데이터가 없습니다.</td>
  </tr>
  {% else %}
    {% for r in rows %}
    <tr>
      <td class="numeric">{{ r[0] }}</td> <!-- id -->
      <td>{{ r[1] }}</td>                <!-- name -->
      <td>{{ r[2] or '-' }}</td>         <!-- category -->
      <td>{{ r[3] or '-' }}</td>         <!-- address -->
      <td class="numeric">{{ r[4] if r[4] is not none else '-' }}</td> <!-- lat -->
      <td class="numeric">{{ r[5] if r[5] is not none else '-' }}</td> <!-- lon -->
      <td class="numeric">{{ "%.1f"|format(r[6]) if r[6] is not none else '-' }}</td> <!-- rating -->
      <td class="numeric">{{ r[7] }}</td> <!-- num_reviews -->
    </tr>
    {% endfor %}
  {% endif %}
</table>

</body>
</html>
"""
    return render_template_string(html, rows=rows, admin_key=key)


@app.route("/admin/users/update", methods=["POST"])
def admin_update_user():
    key = request.args.get("key", "")
    if key != ADMIN_PASSWORD:
        return "UNAUTHORIZED", 403

    phone = request.form.get("phone_number")
    lat_str = request.form.get("latitude")
    lon_str = request.form.get("longitude")
    alert_times = request.form.get("alert_times", "").strip()
    is_active = request.form.get("is_active") == "on"

    if not phone:
        return "phone_number is required", 400

    try:
        lat = float(lat_str) if lat_str is not None else None
        lon = float(lon_str) if lon_str is not None else None
    except ValueError:
        return "위도/경도는 숫자만 입력해주세요.", 400

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE users
        SET latitude = %s,
            longitude = %s,
            alert_times = %s,
            is_active = %s
        WHERE phone_number = %s;
        """,
        (lat, lon, alert_times, is_active, phone),
    )

    conn.commit()
    conn.close()

    return redirect(f"/admin?key={key}")


@app.route("/admin/users/delete", methods=["POST"])
def admin_delete_user():
    key = request.args.get("key", "")
    if key != ADMIN_PASSWORD:
        return "UNAUTHORIZED", 403

    phone = request.form.get("phone_number")

    if not phone:
        return "phone_number is required", 400

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("DELETE FROM user_feedback WHERE phone_number = %s;", (phone,))
    cur.execute("DELETE FROM recommendation_logs WHERE phone_number = %s;", (phone,))
    cur.execute("DELETE FROM users WHERE phone_number = %s;", (phone,))

    conn.commit()
    conn.close()

    return redirect(f"/admin?key={key}")

# =========================
# API: 회원가입/설정 저장
# =========================

@app.route("/api/save-user", methods=["POST"])
def api_save_user():
    data = request.get_json() or {}

    phone = data.get("phone")
    preferred_distance_km = data.get("distance_km")
    preferred_price_range = data.get("price_range")
    preferences_categories = data.get("categories")

    if not phone:
        return jsonify({"error": "전화번호는 필수입니다."}), 400

    if isinstance(preferences_categories, list):
        preferences_categories_str = ",".join(preferences_categories)
    else:
        preferences_categories_str = preferences_categories or ""

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO users (phone_number, preferred_distance_km, preferred_price_range, preferences_categories)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (phone_number)
        DO UPDATE SET
            preferred_distance_km = EXCLUDED.preferred_distance_km,
            preferred_price_range = EXCLUDED.preferred_price_range,
            preferences_categories = EXCLUDED.preferences_categories;
        """,
        (phone, preferred_distance_km, preferred_price_range, preferences_categories_str),
    )

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"result": "ok"})

@app.route("/register", methods=["POST"])
def register():
    """
    /signup 페이지에서 '신청하기' 버튼 눌렀을 때 호출되는 엔드포인트.

    프론트에서 보내는 JSON 예시:
    {
      "phone_number": "01012341234",
      "latitude": 37.5,
      "longitude": 126.9,
      "preferences_categories": ["한식", "일식"],
      "preferences_focus": "맛",
      "alert_times": ["아침", "점심"]
    }
    """
    data = request.get_json() or {}

    # 1) 기본 파라미터 파싱
    phone = data.get("phone_number") or data.get("phone")
    if not phone:
        return jsonify({"success": False, "message": "전화번호는 필수입니다."}), 400

    # 위도/경도
    lat = data.get("latitude")
    lon = data.get("longitude")
    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        # 위치 값이 이상하면 그냥 NULL로 저장
        lat, lon = None, None

    # 선호 카테고리
    categories = data.get("preferences_categories") or data.get("categories")
    if isinstance(categories, list):
        categories_str = ",".join(categories)
    else:
        categories_str = categories or ""

    # 알림 시간대 (아침,점심,저녁,야식)
    alert_times = data.get("alert_times") or []
    if isinstance(alert_times, list):
        alert_times_str = ",".join(alert_times)
    else:
        alert_times_str = str(alert_times) if alert_times else ""

    conn = None
    cur = None

    try:
        conn = get_conn()
        cur = conn.cursor()

        # 2) INSERT 전에 기존 가입 여부 확인
        cur.execute(
            "SELECT 1 FROM users WHERE phone_number = %s",
            (phone,),
        )
        already_exists = cur.fetchone() is not None

        # 3) 실제 DB에 있는 컬럼 기준으로 upsert
        #    (phone_number, latitude, longitude, alert_times,
        #     preferences_categories, is_active)
        cur.execute(
            """
            INSERT INTO users (phone_number, latitude, longitude, alert_times, preferences_categories, is_active)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (phone_number)
            DO UPDATE SET
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                alert_times = EXCLUDED.alert_times,
                preferences_categories = EXCLUDED.preferences_categories,
                is_active = TRUE;
            """,
            (phone, lat, lon, alert_times_str, categories_str),
        )

        conn.commit()

    except Exception as e:
        if conn:
            conn.rollback()
        print("[REGISTER_ERROR]", e)
        return jsonify({"success": False, "message": "서버 오류가 발생했습니다."}), 500

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    # 4) 완전 신규일 때만 웰컴 알림톡 발송
    if not already_exists:
        try:
            ok, res = send_welcome_message(phone)
            if not ok:
                print("[WELCOME_ERROR]", res)
        except Exception as e:
            # 웰컴 실패해도 회원가입 자체는 성공으로 처리
            print("[WELCOME_EXCEPTION]", e)

    # 프론트에서 data.success를 보고 있으므로 이 형식 유지
    return jsonify({"success": True})


# =========================
# API: 유저 피드백 저장
# =========================

@app.route("/api/quick-feedback", methods=["POST"])
def api_quick_feedback():
    """
    카드에서 눌리는 '좋아요/별로예요' 빠른 피드백 저장용.

    프론트에서 오는 JSON 예시:
    {
      "phone": "01012341234",
      "name": "맛있는 칼국수 본점",        // 또는 "restaurant_name"
      "category": "분식",
      "like": true,                     // 또는 "is_good": true
      "time_of_day": "점심"             // (옵션) 아침/점심/저녁/야식
    }
    """
    data = request.get_json() or {}

    phone = data.get("phone")
    name = data.get("restaurant_name") or data.get("name")
    category = data.get("category") or ""
    time_of_day = data.get("time_of_day") or data.get("time")
    restaurant_id = data.get("restaurant_id")

    # like / is_good 둘 다 지원
    like_flag = data.get("like")
    if like_flag is None:
        like_flag = data.get("is_good")

    # 필수값 체크
    if not phone or not name:
        return jsonify({"error": "필수 데이터(phone, name)가 누락되었습니다."}), 400

    # 좋아요면 5점, 싫어요면 1점
    rating = 5 if like_flag else 1

    conn = get_conn()
    cur = conn.cursor()

    try:
        # user_feedback 테이블에 저장
        cur.execute(
            """
            INSERT INTO user_feedback (phone_number, restaurant_name, category, rating, source, time_of_day, restaurant_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                phone,
                name,
                category,
                rating,
                'quick',
                time_of_day,
                restaurant_id,
            ),
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("[QUICK_FEEDBACK_ERROR]", e)
        return jsonify({"error": "DB 저장 중 오류 발생"}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({"result": "ok"})

@app.route("/feedback-form")
def feedback_form():
    phone = request.args.get("phone", "").strip()
    time_of_day = request.args.get("time", "").strip()  # 옵션

    if not phone:
        return "잘못된 접근입니다. (phone 파라미터 없음)", 400

    conn = None
    rows = []
    try:
        conn = get_conn()
        cur = conn.cursor()

        if time_of_day:
            # 오늘 + 해당 시간대 + 좋아요 리스트만
            cur.execute(
                """
                SELECT restaurant_name, COALESCE(category, ''), MAX(created_at) AS last_time
                FROM user_feedback
                WHERE phone_number = %s
                  AND rating >= 5
                  AND created_at::date = CURRENT_DATE
                GROUP BY restaurant_name, category
                ORDER BY last_time DESC
                LIMIT 10;
                """,
                (phone,),
            )
        else:
            # fallback: 전체 좋아요 최근 10개
            cur.execute(
                """
                SELECT restaurant_name, COALESCE(category, ''), MAX(created_at) AS last_time
                FROM user_feedback
                WHERE phone_number = %s
                  AND rating >= 5
                GROUP BY restaurant_name, category
                ORDER BY last_time DESC
                LIMIT 10;
                """,
                (phone,),
            )

        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        print("[FEEDBACK_FORM_QUERY_ERROR]", e)
        if conn:
            conn.close()
        return "서버 오류로 데이터를 불러오지 못했습니다.", 500

    if conn:
        conn.close()

    # rows: [(name, category, last_time), ...]
    item_blocks = ""
    if not rows:
        item_blocks = """
        <div class="empty">
          아직 좋아요를 누른 맛집이 없습니다.<br>
          오늘의 추천에서 마음에 드는 가게에 👍를 눌러 주세요!
        </div>
        """
    else:
        for name, category, last_time in rows:
            cat_text = category or "기타"
            item_blocks += f"""
            <form method="POST" action="/submit-feedback" class="store-card">
              <input type="hidden" name="phone_number" value="{phone}">
              <input type="hidden" name="restaurant_name" value="{name}">
              <input type="hidden" name="category" value="{cat_text}">
              <input type="hidden" name="time_of_day" value="{time_of_day}">

              <div class="store-header">
                <div class="store-name">{name}</div>
                <div class="store-cat">{cat_text}</div>
              </div>

              <div class="field">
                <div class="label">만족도 (1 ~ 5점)</div>
                <div class="rating-stars">
                  <label>
                    <input type="radio" name="rating" value="5" checked>
                    <span>★★★★★ (5점)</span>
                  </label>
                  <label>
                    <input type="radio" name="rating" value="4">
                    <span>★★★★☆ (4점)</span>
                  </label>
                  <label>
                    <input type="radio" name="rating" value="3">
                    <span>★★★☆☆ (3점)</span>
                  </label>
                  <label>
                    <input type="radio" name="rating" value="2">
                    <span>★★☆☆☆ (2점)</span>
                  </label>
                  <label>
                    <input type="radio" name="rating" value="1">
                    <span>★☆☆☆☆ (1점)</span>
                  </label>
                </div>
              </div>

              <div class="field">
                <div class="label">한 줄 후기 (선택)</div>
                <textarea name="comment"
                  placeholder="예) 양 많고 분위기 좋아요. 데이트 코스로 추천!"></textarea>
              </div>

              <button type="submit" class="btn">이 가게 평가하기</button>
            </form>
            """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>오늘의 맛집 피드백</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {{
          font-family: "Noto Sans KR", sans-serif;
          background: #f5f5f5;
          margin: 0;
          padding: 0;
          display: flex;
          justify-content: center;
          align-items: flex-start;
          min-height: 100vh;
        }}
        .wrap {{
          background: white;
          width: 95%;
          max-width: 480px;
          padding: 20px 18px 24px;
          margin: 24px 0;
          border-radius: 18px;
          box-shadow: 0 12px 30px rgba(0,0,0,0.08);
        }}
        h2 {{
          font-size: 18px;
          margin: 0 0 6px;
        }}
        .desc {{
          font-size: 13px;
          color: #666;
          margin-bottom: 12px;
        }}
        .empty {{
          font-size: 14px;
          color: #777;
          text-align: center;
          padding: 24px 8px;
        }}
        .store-card {{
          border-radius: 14px;
          border: 1px solid #eee;
          padding: 14px 12px 16px;
          margin-top: 12px;
          background: #fafafa;
        }}
        .store-header {{
          display: flex;
          justify-content: space-between;
          align-items: baseline;
          margin-bottom: 8px;
        }}
        .store-name {{
          font-size: 15px;
          font-weight: 600;
        }}
        .store-cat {{
          font-size: 12px;
          color: #999;
        }}
        .field {{
          margin-top: 8px;
        }}
        .label {{
          font-size: 12px;
          margin-bottom: 4px;
          color: #444;
        }}
        textarea {{
          width: 100%;
          min-height: 60px;
          font-size: 13px;
          padding: 6px;
          border-radius: 8px;
          border: 1px solid #ddd;
          resize: vertical;
          box-sizing: border-box;
        }}
        .btn {{
          margin-top: 10px;
          width: 100%;
          padding: 9px 0;
          border-radius: 999px;
          border: none;
          background: #ff6b81;
          color: white;
          font-size: 14px;
          cursor: pointer;
        }}
        .rating-stars {{
          display: flex;
          flex-direction: column;
          gap: 4px;
          font-size: 13px;
          align-items: flex-start;
        }}
        .rating-stars label {{
          display: flex;
          align-items: center;
          gap: 4px;
          cursor: pointer;
        }}
        .rating-stars input[type="radio"] {{
          accent-color: #ffb400;
        }}
      </style>
    </head>
    <body>
      <div class="wrap">
        <h2>좋아요 누르신 맛집들, 어떠셨나요?</h2>
        <div class="desc">
          별점과 짧은 한 줄 후기를 남겨주시면<br>
          다음 추천에 더 정확하게 반영해드릴게요 :)
        </div>
        {item_blocks}
      </div>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route("/submit-feedback", methods=["POST"])
def submit_feedback():
    conn = None
    try:
        phone = request.form.get("phone_number")
        restaurant = request.form.get("restaurant_name")
        category = request.form.get("category") or None
        rating_raw = request.form.get("rating")
        comment = request.form.get("comment") or ""
        time_of_day = request.form.get("time_of_day") or None
        # 상세 리뷰 폼에서는 restaurant_id를 별도로 넘기지 않으므로 None
        restaurant_id = request.form.get("restaurant_id") or None

        if not phone or not restaurant or not rating_raw:
            return (
                "<script>alert('필수 값이 누락되었습니다. 다시 시도해주세요.');history.back();</script>",
                400,
            )

        try:
            rating = int(rating_raw)
        except ValueError:
            return (
                "<script>alert('별점 값이 잘못되었습니다. 다시 선택해주세요.');history.back();</script>",
                400,
            )

        conn = get_conn()
        cur = conn.cursor()

        try:
            # comment 컬럼이 있다고 가정하고 시도
            cur.execute(
                """
                INSERT INTO user_feedback
                (phone_number, restaurant_name, category, rating, comment, source, time_of_day, restaurant_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    phone,
                    restaurant,
                    category,
                    rating,
                    comment,
                    'form',
                    time_of_day,
                    restaurant_id,
                ),
            )
        except UndefinedColumn:
            # comment 컬럼이 없을 경우 컬럼 추가 후 다시 시도
            conn.rollback()
            cur.execute(
                """
                ALTER TABLE user_feedback
                ADD COLUMN IF NOT EXISTS comment TEXT;
                """
            )
            conn.commit()

            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO user_feedback
                (phone_number, restaurant_name, category, rating, comment, source, time_of_day, restaurant_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    phone,
                    restaurant,
                    category,
                    rating,
                    comment,
                    'form',
                    time_of_day,
                    restaurant_id,
                ),
            )

        conn.commit()
        cur.close()
        conn.close()
        conn = None

        return """
        <script>
          alert('피드백이 저장되었습니다. 소중한 의견 감사합니다!');
          if (window.history.length > 1) {
              history.back();
          } else {
              window.close();
          }
        </script>
        """

    except Exception as e:
        print("[SUBMIT_FEEDBACK_ERROR]", e)
        if conn:
            conn.rollback()
        return (
            "<script>alert('서버 오류로 피드백 저장에 실패했습니다. 나중에 다시 시도해주세요.');history.back();</script>",
            500,
        )
    finally:
        if conn:
            conn.close()

@app.route("/cron/send-reco", methods=["GET"])
def cron_send_reco():
    """
    외부 크론에서:
      GET /cron/send-reco?time=아침
      GET /cron/send-reco?time=점심
    이런 식으로 호출.

    - users.alert_times 에 해당 time 문자열(아침/점심/저녁/야식)이 포함된
      활성 사용자에게 맛집 추천 알림톡 발송
    """
    time_label = (request.args.get("time") or "").strip()
    if not time_label:
        return jsonify({
            "result": "error",
            "message": "time 쿼리 파라미터 필요 (예: 아침,점심,저녁,야식)"
        }), 400

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT phone_number
            FROM users
            WHERE is_active = TRUE
              AND (
                  alert_times ILIKE %s
                  OR alert_times = ''
                  OR alert_times IS NULL
              )
            """,
            (f"%{time_label}%",),
        )
        phones = [row[0] for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    sent = []
    failed = []

    for p in phones:
        ok, res = send_reco_message(p, time_label)
        if ok:
            sent.append(p)
        else:
            failed.append({"phone": p, "res": res})

    return jsonify({
        "result": "ok",
        "time": time_label,
        "sent_count": len(sent),
        "failed_count": len(failed),
        "failed": failed,
    })

@app.route("/cron/send-feedback", methods=["GET"])
def cron_send_feedback():
    """
    외부 크론에서:
      GET /cron/send-feedback?time=점심

    - 오늘 해당 time에 추천 나간 사람 중
    - 최소 1곳 이상 '좋아요(5점)' 남긴 사용자에게
      피드백 알림톡 발송
    """
    time_label = (request.args.get("time") or "").strip()
    if not time_label:
        return jsonify({
            "result": "error",
            "message": "time 쿼리 파라미터 필요 (예: 아침,점심,저녁,야식)"
        }), 400

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT DISTINCT rl.phone_number
            FROM recommendation_logs rl
            JOIN user_feedback uf
              ON uf.phone_number = rl.phone_number
             AND uf.restaurant_name = rl.restaurant_name
            WHERE rl.time_of_day = %s
              AND rl.created_at::date = CURRENT_DATE
              AND uf.rating >= 5
              AND uf.created_at::date = CURRENT_DATE
            """,
            (time_label,),
        )
        phones = [row[0] for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    sent = []
    failed = []

    for p in phones:
        ok, res = send_feedback_message(p, time_label)

        # 상세 응답은 서버 로그에만 남김
        print("[CRON_FEEDBACK]", "phone=", p, "ok=", ok, "res=", res)

        if ok:
            sent.append(p)
        else:
            failed.append(p)  # ← 번호만 저장 (가볍게)

    # 실패 번호도 너무 많으면 잘라서 리턴 (안전빵)
    failed_sample = failed[:50]

    return jsonify({
        "result": "ok",
        "time": time_label,
        "target_count": len(phones),
        "sent_count": len(sent),
        "failed_count": len(failed),
        "failed_sample": failed_sample,  # 최대 50개까지만
    })


@app.route("/test/alimtalk", methods=["GET"])
def test_alimtalk():
    """
    간단한 테스트용 알림톡 발송 엔드포인트.
    예:
      /test/alimtalk?type=welcome&phone=01095883044
      /test/alimtalk?type=reco&phone=01095883044&time=점심
      /test/alimtalk?type=feedback&phone=01095883044&time=점심
    """
    phone = (request.args.get("phone") or "").strip()
    msg_type = (request.args.get("type") or "welcome").strip()
    time_label = (request.args.get("time") or "점심").strip()

    if not phone:
        return jsonify({"result": "error", "message": "phone 쿼리 필요"}), 400

    if msg_type == "welcome":
        ok, res = send_welcome_message(phone)
    elif msg_type == "reco":
        ok, res = send_reco_message(phone, time_label)
    elif msg_type == "feedback":
        ok, res = send_feedback_message(phone, time_label)
    else:
        return jsonify({"result": "error", "message": "type=welcome|reco|feedback 중 하나"}), 400

    return jsonify({"ok": ok, "aligo_response": res})

# =========================
# 위치 기반 추천 API (Google Places + Kakao)
# =========================
def upsert_restaurant_and_get_id(cur, name, category, address, lat, lon, rating, num_reviews):
    """
    restaurants 테이블에 (name + address) 기준 upsert 하고,
    해당 row의 id를 리턴한다.
    """
    if not name:
        return None

    addr_val = address or ""

    cur.execute(
        """
        INSERT INTO restaurants (name, category, address, lat, lon, rating, num_reviews)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (name, address)
        DO UPDATE SET
            category    = EXCLUDED.category,
            address     = EXCLUDED.address,
            lat         = EXCLUDED.lat,
            lon         = EXCLUDED.lon,
            rating      = EXCLUDED.rating,
            num_reviews = EXCLUDED.num_reviews
        RETURNING id;
        """,
        (name, category, addr_val, lat, lon, rating, num_reviews),
    )
    row = cur.fetchone()
    return row[0] if row else None


@app.route("/api/reco", methods=["POST"])
def api_reco():
    """
    위치 기반 맛집 추천 (Google Places + 카카오맵 매칭 버전)

    주요 기능:
    1) Google Places로 주변 음식점 후보 수집
    2) 카카오맵에 실제로 등록된 곳만 필터링 (place_id 없는 곳 제외)
    3) 유저 피드백(좋아요/별로에요)을 반영한 선호 점수 계산
    4) 최근 2일 내에 이미 추천한 가게는 최대한 제외 (restaurant_id 기준 우선)
    5) 최종 상위 10개 중 3곳 랜덤 노출
    6) restaurants 테이블에 upsert + recommendation_logs에 restaurant_id 저장
    7) 현재 영업 중이 아니고, 1시간 뒤에도 영업 중이 아닌 가게는 제외
    8) 디버그용 로그로, 어떤 후보들이 수집/필터/최종 선정됐는지 확인
    """
    data = request.get_json() or {}
    phone = data.get("phone") or ""
    time_of_day = data.get("time") or ""

    lat = data.get("lat")
    lon = data.get("lon")

    # 1) 위치 값 체크
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return jsonify({"error": "위치 정보가 잘못되었습니다."}), 400

    # 기본 요청 정보 로그
    print(
        f"[API_RECO_REQUEST] phone={phone}, time={time_of_day}, "
        f"lat={lat}, lon={lon}"
    )

    # 2) DB에서 유저 선호/최근 추천 이력 가져오기
    user_categories = []
    category_prefs = {}
    restaurant_prefs = {}
    recent_ids_2d = set()
    recent_names_2d = set()

    conn = None
    cur = None
    if phone:
        try:
            conn = get_conn()
            cur = conn.cursor()

            # 선호 카테고리 (회원 가입 시 선택한 것)
            try:
                cur.execute(
                    "SELECT preferences_categories FROM users WHERE phone_number = %s;",
                    (phone,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    user_categories = [
                        c.strip() for c in str(row[0]).split(",") if c.strip()
                    ]
            except Exception as e:
                print("[API_RECO_USER_PREF_CATS_ERR]", e)
                conn.rollback()

            # 피드백 기반 (시간대별) 카테고리별 평균 점수
            try:
                category_prefs = get_user_prefs_by_time(phone, time_of_day, cur)
            except Exception as e:
                print("[API_RECO_CATEGORY_PREF_ERR]", e)
                conn.rollback()
                category_prefs = {}

            # 피드백 기반 개별 가게별 평균 점수
            try:
                restaurant_prefs = get_user_restaurant_prefs(phone, cur)
            except Exception as e:
                print("[API_RECO_RESTAURANT_PREF_ERR]", e)
                conn.rollback()
                restaurant_prefs = {}

            # 최근 2일간 이미 추천한 가게 목록 (restaurant_id 우선)
            try:
                cur.execute(
                    """
                    SELECT restaurant_name, restaurant_id
                    FROM recommendation_logs
                    WHERE phone_number = %s
                      AND created_at >= NOW() - INTERVAL '2 days';
                    """,
                    (phone,),
                )
                recent_rows = cur.fetchall()
                for name, rid in recent_rows:
                    if rid is not None:
                        recent_ids_2d.add(rid)
                    if name:
                        recent_names_2d.add(name)
            except Exception as e:
                print("[API_RECO_RECENT_ERR]", e)
                conn.rollback()
                recent_ids_2d = set()
                recent_names_2d = set()

        except Exception as e:
            print("[API_RECO_DB_ERR]", e)
            conn = None
            cur = None

    # 3) Google Places에서 주변 음식점 검색
    places = search_google_places(lat, lon, radius_m=1500, max_results=20)

    # 🔍 Google 원본 결과 로그
    print(f"[API_RECO_GOOGLE_RAW] count={len(places)}")
    for p in places:
        try:
            print(
                "  -",
                p.get("name"),
                "/",
                p.get("shortFormattedAddress") or p.get("address"),
                "/ rating=",
                p.get("rating"),
                "/ dist_km=",
                p.get("distance_km"),
            )
        except Exception:
            # 로그에서 에러 나도 추천 로직은 계속 진행
            pass

    if not places:
        if conn:
            conn.close()
        return jsonify([])

    # 3-1) 동일한 가게(이름 + 주소 기준) 1차 중복 제거
    unique_places = []
    seen_keys = set()
    for p in places:
        key = (p.get("name"), p.get("shortFormattedAddress") or p.get("address"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_places.append(p)

    # 4) 카카오맵에 실제로 등록된 곳만 매칭 + 동일 place_id 재중복 제거
    candidates = []
    seen_kakao_ids = set()

    for p in unique_places:
        plat = p.get("lat")
        plon = p.get("lon")
        if plat is None or plon is None:
            continue

        raw_name = p.get("name")
        name_ko, kakao_place_id, kakao_addr = match_kakao_place_by_location(
            raw_name, plat, plon
        )
        if not kakao_place_id:
            continue

        # 같은 카카오 place_id 는 한 번만 사용
        if kakao_place_id in seen_kakao_ids:
            continue
        seen_kakao_ids.add(kakao_place_id)

        # ── 영업 여부: Kakao 우선, 없으면 Google 정보 사용 ─────────────
        kakao_open_info = ""
        is_open_now = None

        try:
            kakao_basic = get_kakao_basic_info(kakao_place_id)
        except Exception as e:
            kakao_basic = None
            print("[KAKAO_BASIC_INFO_IN_RECO_ERR]", e)

        if kakao_basic:
            if kakao_basic.get("address"):
                kakao_addr = kakao_basic.get("address")

            if kakao_basic.get("open_info"):
                kakao_open_info = kakao_basic.get("open_info")

            raw_flag = kakao_basic.get("is_open")
            if raw_flag is not None:
                flag = str(raw_flag).lower()
                if flag in ("y", "1", "true", "open", "o"):
                    is_open_now = True
                elif flag in ("n", "0", "false", "closed", "c"):
                    is_open_now = False

        # Google open_now / open_in_1h 정보
        g_open_now = p.get("open_now")
        g_open_in_1h = p.get("open_in_1h")

        # 현재 영업 여부: Kakao 우선, 없으면 Google
        if is_open_now is None:
            is_open_now = g_open_now

        # ★ 1시간 뒤에도 영업 중이 아닐 경우 제외
        #  - g_open_in_1h 가 False 로 명시되어 있으면 제외
        if g_open_in_1h is False:
            continue

        # 화면에 보여줄 영업정보
        open_info = kakao_open_info or p.get("open_info") or ""
        # ────────────────────────────────────────────────────────

        rating = p.get("rating")
        user_rating_count = p.get("user_rating_count") or 0

        # 거리: 소수점 1자리
        raw_distance = p.get("distance_km")
        distance_km = None
        if raw_distance is not None:
            try:
                distance_km = round(float(raw_distance), 1)
            except (TypeError, ValueError):
                distance_km = None

        address = kakao_addr or p.get("address") or ""

        photo_urls = p.get("photo_urls") or []
        photo_url = photo_urls[0] if photo_urls else None

        category = p.get("category") or ""

        reviews = p.get("reviews") or []
        review_texts = [r for r in reviews if isinstance(r, str)]

        name = name_ko or raw_name or "이름 없음"

        # 한 줄 요약
        if review_texts:
            kr_reviews = [txt for txt in review_texts if re.search(r"[가-힣]", txt)]
            if kr_reviews:
                chosen = kr_reviews[0]
                chosen = chosen.replace("\\n", " ").strip()
                if len(chosen) > 80:
                    chosen = chosen[:80].rstrip() + "..."
                summary = chosen
            else:
                summary = build_summary_text(name, category, rating, distance_km)
        else:
            summary = build_summary_text(name, category, rating, distance_km)

        # 대표 메뉴
        menus = []
        for txt in review_texts:
            menus += extract_menu_from_review(txt)
        menus = list(dict.fromkeys(menus))

        if menus:
            menu = ", ".join(menus[:2])
        else:
            menu = build_menu_text(name, category)

        # 선호 여부/설명 태그
        is_preferred = False
        reasons = []

        base_rating = rating if rating is not None else 3.0
        base_dist = float(distance_km or 0.0)

        # 리뷰 수에 따른 신뢰도 가중치
        review_count = user_rating_count or 0
        if review_count >= 50:
            review_factor = 1.2
        elif review_count >= 20:
            review_factor = 1.1
        elif review_count >= 5:
            review_factor = 1.0
        else:
            review_factor = 0.9

        score = base_rating * 10 * review_factor - base_dist

        # 회원가입 시 선택한 선호 카테고리
        if user_categories and category:
            for uc in user_categories:
                if uc and uc in category:
                    score += 5
                    is_preferred = True
                    reasons.append("회원가입에서 선택한 선호 카테고리와 일치해요.")
                    break

        # 시간대별 카테고리 선호도
        if category_prefs and category in category_prefs:
            avg_cat = category_prefs[category]
            if avg_cat >= 4.5:
                score *= 1.3
                is_preferred = True
                reasons.append("이 시간대에 자주 높게 평가한 음식 종류예요.")
            elif avg_cat >= 4.0:
                score *= 1.15
                reasons.append("이 시간대에 만족도가 높은 카테고리예요.")
            elif avg_cat >= 3.0:
                pass
            elif avg_cat >= 2.0:
                score *= 0.7
                reasons.append("예전에 살짝 아쉬웠던 카테고리지만, 근처라 후보에 포함했어요.")
            else:
                score *= 0.4
                reasons.append("평균 만족도가 낮았던 카테고리라 점수를 낮췄어요.")

        # 개별 가게 선호도
        if restaurant_prefs and name in restaurant_prefs:
            avg_rest = restaurant_prefs[name]
            if avg_rest >= 4.0:
                score *= 1.3
                is_preferred = True
                reasons.append("이전에 이 가게에 높은 점수를 주신 적이 있어요.")
            elif avg_rest <= 2.5:
                score *= 0.2
                reasons.append("예전에 별로라고 평가하신 가게라 점수를 크게 낮췄어요.")

        reason_text = " ".join(dict.fromkeys(reasons)) if reasons else ""

        keywords = build_keywords(
            category,
            rating,
            distance_km,
            preferred=is_preferred,
            review_text=summary,
        )

        # restaurants 테이블 upsert + id 획득
        restaurant_id = None
        if conn and cur:
            try:
                restaurant_id = upsert_restaurant_and_get_id(
                    cur,
                    name=name,
                    category=category,
                    address=address,
                    lat=plat,
                    lon=plon,
                    rating=rating,
                    num_reviews=user_rating_count,
                )
            except Exception as e:
                print("[UPSERT_RESTAURANT_ERROR]", e)
                conn.rollback()

        candidates.append(
            {
                "name": name,
                "category": category,
                "rating": rating,
                "menu": menu,
                "summary": summary,
                "place_id": kakao_place_id,
                "image_url": photo_url,
                "distance_km": distance_km,
                "keywords": keywords,
                "images": photo_urls,
                "address": address,
                "open_info": open_info,
                "score": score,
                "restaurant_id": restaurant_id,
                "reason": reason_text,
                "is_preferred": is_preferred,
                "is_ad": False,
                "is_sponsored": False,
            }
        )

        # 후보가 너무 많아지는 것 방지 (최대 12개 정도까지만)
        if len(candidates) >= 12:
            break

    # 🔍 후보 리스트 요약 로그
    print(f"[API_RECO_CANDIDATES] count={len(candidates)}")
    for c in candidates:
        try:
            print(
                "  -",
                c["name"],
                "/",
                c.get("address"),
                "/ rating=",
                c.get("rating"),
                "/ dist_km=",
                c.get("distance_km"),
                "/ rid=",
                c.get("restaurant_id"),
            )
        except Exception:
            pass

    if not candidates:
        if conn:
            conn.close()
        return jsonify([])

    # 6) 최근 2일 내에 이미 추천한 가게 최대한 제외 (restaurant_id 우선)
    filtered_candidates = []
    if recent_ids_2d or recent_names_2d:
        for c in candidates:
            rid = c.get("restaurant_id")
            if rid is not None and rid in recent_ids_2d:
                continue
            if c["name"] in recent_names_2d:
                continue
            filtered_candidates.append(c)
    else:
        filtered_candidates = list(candidates)

    # 후보가 하나도 안 남으면, 다양한 추천을 위해 전체 후보를 사용
    pool = filtered_candidates if filtered_candidates else list(candidates)

    # 최소 3개 채우기
    if len(pool) < 3:
        existing_ids = {
            c.get("restaurant_id") for c in pool if c.get("restaurant_id") is not None
        }
        existing_names = {c["name"] for c in pool}
        for c in candidates:
            rid = c.get("restaurant_id")
            if rid is not None and rid in existing_ids:
                continue
            if c["name"] in existing_names:
                continue
            pool.append(c)
            if rid is not None:
                existing_ids.add(rid)
            existing_names.add(c["name"])
            if len(pool) >= 3:
                break

    # 7) 점수 기준 상위 10개 중 랜덤 3개 (다양성 확보)
    pool.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_pool = pool[:10]
    random.shuffle(top_pool)
    picked = top_pool[:3]

    for c in picked:
        c.pop("score", None)

    # 🔍 최종 풀/선정 결과 로그
    print(
        f"[API_RECO_POOL] total={len(pool)}, "
        f"top10={len(top_pool)}, picked3={len(picked)}"
    )
    for c in picked:
        try:
            print(
                "  [PICKED]",
                c["name"],
                "/",
                c.get("address"),
                "/ rating=",
                c.get("rating"),
                "/ dist_km=",
                c.get("distance_km"),
                "/ rid=",
                c.get("restaurant_id"),
            )
        except Exception:
            pass

    # 8) 추천 로그 기록 (restaurant_id 포함)
    if phone and conn and cur:
        try:
            for c in picked:
                try:
                    cur.execute(
                        """
                        INSERT INTO recommendation_logs
                            (phone_number, restaurant_name, time_of_day, restaurant_id)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (phone, c["name"], time_of_day, c.get("restaurant_id")),
                    )
                except Exception as e:
                    print("[API_RECO_LOG_ONE_ERR]", e)
                    conn.rollback()
            conn.commit()
        except Exception as e:
            print("[API_RECO_LOG_ERR]", e)
            conn.rollback()

    if conn:
        conn.close()

    return jsonify(picked)





# =========================
# 디버그용
# =========================

@app.route("/debug/restaurants")
def debug_restaurants():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM restaurants LIMIT 50;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(rows)


@app.route("/init-db")
def init_db_route():
    init_db()
    return "DB initialized!"


@app.route("/go")
def go_kakao_map():
    # 프론트에서 place_id 또는 pid 로 보낼 수 있으므로 둘 다 받기
    place_id = request.args.get("pid") or request.args.get("place_id")
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    name = request.args.get("name", "")
    phone = request.args.get("phone", "").strip()
    time_of_day = request.args.get("time", "").strip()
    restaurant_id = request.args.get("rid")

    # 클릭 로그 저장 (실패하더라도 이동 자체는 계속 진행)
    if phone and (restaurant_id or name):
        conn = None
        cur = None
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO click_logs (phone_number, restaurant_id, restaurant_name, time_of_day)
                VALUES (%s, %s, %s, %s);
                """,
                (
                    phone,
                    restaurant_id,
                    name,
                    time_of_day or None,
                ),
            )
            conn.commit()
        except Exception as e:
            print("[CLICK_LOG_ERROR]", e)
            if conn:
                conn.rollback()
        finally:
            if cur:
                cur.close()
            if conn:
                conn.close()

    # 1) place_id 있을 때 → 카카오맵 공식 장소 상세 URL
    if place_id:
        # 앱/웹 모두 정상적으로 장소 상세 페이지로 이동하는 확실한 방식
        return redirect(f"https://place.map.kakao.com/{place_id}")

    # 2) place_id 없고 좌표만 있을 때 → 지도에 핀 찍기
    if lat and lon:
        try:
            lat_f = float(lat)
            lon_f = float(lon)
            return redirect(f"https://map.kakao.com/link/map/{name},{lat_f},{lon_f}")
        except:
            pass

    # 3) 모두 없으면 카카오맵 홈
    return redirect("https://map.kakao.com/")

@app.route("/my-ip")
def my_ip():
    return requests.get("https://api.ipify.org").text

@app.route("/debug/aligo")
def debug_aligo():
    return {
        "API_KEY": ALIGO_API_KEY,
        "USER_ID": ALIGO_USER_ID,
        "SENDER_KEY": ALIGO_SENDER_KEY,
        "SENDER": ALIGO_SENDER,
        "TESTMODE": ALIGO_TESTMODE,
        "BASE_SERVER_URL": BASE_SERVER_URL,
    }


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
