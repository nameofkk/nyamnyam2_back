from asyncio import open_connection
import os
import math
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request, render_template, jsonify, redirect
from datetime import datetime, timedelta
from psycopg2.errors import UndefinedColumn

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
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "AIzaSyAox_CWmpe4klOp48vfgRk9JX8vTAQ_guard")
ALIGO_API_KEY = os.getenv("ALIGO_API_KEY", "YOUR_ALIGO_API_KEY")

BASE_SERVER_URL = os.getenv("SERVER_BASE_URL", "http://127.0.0.1:5000")


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


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(CREATE_USERS_TABLE)
    cur.execute(CREATE_RESTAURANTS_TABLE)
    cur.execute(CREATE_REVIEWS_TABLE)
    cur.execute(CREATE_USER_FEEDBACK_TABLE)
    cur.execute(CREATE_RECOMMENDATION_LOGS_TABLE)

    # user_feedback에 source 컬럼 없을 수 있으니 안전하게 추가
    try:
        cur.execute("ALTER TABLE user_feedback ADD COLUMN source VARCHAR(50);")
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
    resp = requests.get(url, headers=headers, params=params, timeout=5)
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


def match_kakao_place_by_location(name, lat, lon, radius=100):
    """
    Google Places에서 받은 가게 이름 + 좌표를 가지고
    카카오맵 place_id를 찾는다.

    1순위: 키워드 검색(query=정제된 이름, sort=distance)
    2순위: 결과 없으면 FD6 카테고리 검색으로 근처 1개라도 잡기
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

    # 1) 이름 기반 키워드 검색
    if clean_name:
        url = "https://dapi.kakao.com/v2/local/search/keyword.json"
        headers = {
            "Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"
        }
        params = {
            "query": clean_name,
            "x": lon,
            "y": lat,
            "radius": radius,
            "sort": "distance",
            "category_group_code": "FD6"
        }
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=5)
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

    # 2) 이름 기반 검색 실패 시, 카테고리(FD6)로 근처 한 곳이라도
    try:
        cat_data = kakao_category_search("FD6", x=lon, y=lat, radius=radius, size=1)
        docs = cat_data.get("documents", [])
        if docs:
            doc = docs[0]
            place_name = doc.get("place_name")
            place_id = doc.get("id")
            address = doc.get("road_address_name") or doc.get("address_name")
            return place_name, place_id, address
    except Exception as e:
        print("[KAKAO_MATCH_CATEGORY_ERROR]", e)

    return None, None, None



def get_kakao_basic_info(place_id):
    url = "https://place.map.kakao.com/main/v/{place_id}".format(place_id=place_id)
    headers = {
        "Referer": "https://map.kakao.com/"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=5)
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
          photo_url, distance_km, reviews
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
        resp.raise_for_status()
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

        # 영업시간 텍스트: "휴무 요일: ~, 영업 시간: ~" 형식으로 정리
        open_info = ""
        opening = p.get("currentOpeningHours") or p.get("regularOpeningHours")
        if opening:
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

        raw_cat = p.get("primaryTypeDisplayName") or ""
        if isinstance(raw_cat, dict):
            en_cat = raw_cat.get("text", "")
        else:
            en_cat = str(raw_cat) if raw_cat is not None else ""
        category = translate_category_to_kr(en_cat)

        # ✅ 사진 여러 장 (최대 5장) URL 생성
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

        # 기존 호환용 대표 사진 1장 (첫 번째 것)
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
                "photo_url": photo_url,      # 대표 1장 (기존 호환용)
                "photo_urls": photo_urls,    # ✅ 슬라이더용 여러 장
                "photo_url": photo_url,
                "distance_km": dist_km,
                "reviews": reviews,
            }
        )

    return results


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


# =========================
# 리뷰 분석 / 대표메뉴 / 요약
# =========================

def extract_menu_from_review(review_text):
    # 한글 메뉴 키워드
    menu_keywords = [
        "김치찌개", "된장찌개", "불고기", "삼겹살", "갈비", "냉면", "비빔밥",
        "초밥", "스시", "라멘", "우동", "돈카츠", "텐동",
        "짜장면", "짬뽕", "탕수육", "마라탕",
        "파스타", "피자", "리조또", "스테이크",
        "커피", "라떼", "에스프레소", "케이크", "빵",
        "치킨", "버거", "뷔페"
    ]

    found = []
    for kw in menu_keywords:
        if kw in review_text:
            found.append(kw)

    # 영어 리뷰에서도 메뉴 뽑아서 한글로 매핑
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
        "curry": "카레",
        "coffee": "커피",
        "cake": "케이크",
        "dessert": "디저트",
        "buffet": "뷔페",
    }
    lower = review_text.lower()
    for eng, kor in eng_map.items():
        if eng in lower:
            found.append(kor)

    # 중복 제거
    return list(dict.fromkeys(found))


def build_menu_text(name, category):
    # '음식점' 같이 포괄적인 카테고리는 가게 이름 기준 문구 사용
    if category and category != "음식점":
        return f"{category} 위주의 인기 메뉴를 즐길 수 있는 곳이에요."
    else:
        return f"{name}만의 인기 메뉴를 즐길 수 있는 곳이에요."
    

def build_summary_text(name, category, rating, distance_km):
    """
    기본 한 줄 요약 텍스트 생성 함수.
    (구글 리뷰가 없을 때 fallback 용)
    """
    # 평점 문구
    if rating is None:
        rating_text = "무난한 평점"
    elif rating >= 4.2:
        rating_text = "평균 이상 좋은 평점"
    elif rating >= 3.5:
        rating_text = "무난한 평점"
    else:
        rating_text = "보통 수준의 평점"

    # 거리 문구
    if distance_km is not None:
        if distance_km <= 0.3:
            dist_text = "현재 위치와 매우 가까워"
        elif distance_km <= 1.0:
            dist_text = "현재 위치와 가까워"
        else:
            dist_text = "주변 위치에서"
    else:
        dist_text = "주변 위치에서"

    # 카테고리 문구
    cat_text = category if category else "다양한 메뉴"

    return (
        f"{name}은(는) {cat_text} 메뉴를 즐길 수 있는 곳입니다. "
        f"{rating_text}을 받고 있으며, {dist_text} 가볍게 방문하기 좋습니다."
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
def signup_page():
    return render_template("signup.html")


@app.route("/reco")
def reco_page():
    phone = request.args.get("phone", "")
    time_of_day = request.args.get("time", "")
    return render_template("reco.html", phone=phone, time=time_of_day)


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


# =========================
# API: 유저 피드백 저장
# =========================

@app.route("/api/quick-feedback", methods=["POST"])
def api_quick_feedback():
    data = request.get_json() or {}

    phone = data.get("phone")
    name = data.get("restaurant_name") or data.get("name")  # 혹시 name 으로 올 때 대비
    category = data.get("category") or ""
    is_good = data.get("is_good")

    # 필수값 검증
    if not phone or not name:
        return jsonify({"error": "필수 데이터(phone, restaurant_name)가 누락되었습니다."}), 400

    rating = 5 if is_good else 1

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            INSERT INTO user_feedback (phone_number, restaurant_name, category, rating)
            VALUES (%s, %s, %s, %s)
            """,
            (phone, name, category, rating)
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




# =========================
# 위치 기반 추천 API (Google Places + Kakao)
# =========================

@app.route("/api/reco", methods=["POST"])
def api_reco():
    """
    위치 기반 맛집 추천 (Google Places + 카카오맵 매칭 버전)

    주요 기능:
    1) Google Places로 주변 음식점 후보 수집
    2) 카카오맵에 실제로 등록된 곳만 필터링 (place_id 없는 곳 제외)
    3) 유저 피드백(좋아요/별로에요)을 반영한 선호 점수 계산
    4) 최근 2일 내에 이미 추천한 가게는 최대한 제외
       - 다만 추천할 가게가 더 이상 없으면 다시 포함
    5) 최종적으로 상위 50개 중에서 3곳을 랜덤 노출
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

    # 2) DB에서 유저 선호/최근 추천 이력 가져오기
    user_categories = []
    category_prefs = {}
    restaurant_prefs = {}
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

            # 피드백 기반 카테고리별 평균 점수
            try:
                category_prefs = get_user_prefs(phone, cur)
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

            # 최근 2일간 이미 추천한 가게 목록
            try:
                cur.execute(
                    """
                    SELECT restaurant_name
                    FROM recommendation_logs
                    WHERE phone_number = %s
                      AND created_at >= NOW() - INTERVAL '2 days';
                    """,
                    (phone,),
                )
                recent_rows = cur.fetchall()
                recent_names_2d = {r[0] for r in recent_rows if r[0]}
            except Exception as e:
                print("[API_RECO_RECENT_ERR]", e)
                conn.rollback()
                recent_names_2d = set()

        except Exception as e:
            print("[API_RECO_DB_ERR]", e)
            conn = None
            cur = None

    # 3) Google Places에서 주변 음식점 검색
    places = search_google_places(lat, lon, radius_m=1500, max_results=20)
    if not places:
        if conn:
            conn.close()
        return jsonify([])

    # 3-1) 동일한 가게(이름 + 주소 기준) 중복 제거
    unique_places = []
    seen_keys = set()
    for p in places:
        key = (p.get("name"), p.get("address"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_places.append(p)

    # 4) 카카오맵에 실제로 등록된 곳만 매칭 (place_id 없는 경우 추천 제외)
    # 4) 카카오맵에 실제로 등록된 곳만 매칭 (place_id 없는 경우 추천 제외)
    candidates = []
    for p in unique_places:
        plat = p.get("lat")
        plon = p.get("lon")
        if plat is None or plon is None:
            continue

        raw_name = p.get("name")
        name_ko, kakao_place_id, kakao_addr = match_kakao_place_by_location(raw_name, plat, plon)
        if not kakao_place_id:
            continue

        rating = p.get("rating")

        # ✅ 거리: 소수점 첫째 자리까지만
        raw_distance = p.get("distance_km")
        distance_km = None
        if raw_distance is not None:
            try:
                distance_km = round(float(raw_distance), 1)
            except (TypeError, ValueError):
                distance_km = None

        address = kakao_addr or p.get("address") or ""
        open_info = p.get("open_info") or ""

        # ✅ 여러 장 사진 (최대 5장) 사용
        photo_urls = p.get("photo_urls") or []
        photo_url = photo_urls[0] if photo_urls else None  # 기존 구조 호환용 대표 1장

        category = p.get("category") or ""


        # 구글 리뷰 문자열 리스트
        reviews = p.get("reviews") or []
        review_texts = [r for r in reviews if isinstance(r, str)]

        # 가게 이름 정리
        name = name_ko or raw_name or "이름 없음"

        # ✅ 한 줄 리뷰: 한국어 리뷰가 있으면 그걸 사용,
        #               없으면 영어 리뷰 대신 기본 요약 사용
        if review_texts:
            # 한글 포함된 리뷰만 우선
            kr_reviews = [txt for txt in review_texts if re.search(r"[가-힣]", txt)]
            if kr_reviews:
                chosen = kr_reviews[0]
                chosen = chosen.replace("\\n", " ").strip()
                if len(chosen) > 80:
                    chosen = chosen[:80].rstrip() + "..."
                summary = chosen
            else:
                # 한국어 리뷰가 하나도 없으면 기본 요약으로
                summary = build_summary_text(name, category, rating, distance_km)
        else:
            summary = build_summary_text(name, category, rating, distance_km)


        # ✅ 대표 메뉴: 강화된 extract_menu_from_review 사용
        menus = []
        for txt in review_texts:
            menus += extract_menu_from_review(txt)
        menus = list(dict.fromkeys(menus))

        if menus:
            menu = ", ".join(menus[:2])
        else:
            menu = build_menu_text(name, category)

        keywords = build_keywords(
            category,
            rating,
            distance_km,
            preferred=False,
            review_text=summary,
        )

        base_rating = rating if rating is not None else 3.0
        base_dist = float(distance_km or 0.0)
        score = base_rating * 10 - base_dist

        if user_categories and category:
            for uc in user_categories:
                if uc and uc in category:
                    score += 5
                    break

        if category_prefs and category in category_prefs:
            avg_cat = category_prefs[category]
            if avg_cat >= 4.5:
                score *= 1.3
            elif avg_cat >= 4.0:
                score *= 1.15
            elif avg_cat >= 3.0:
                score *= 1.0
            elif avg_cat >= 2.0:
                score *= 0.7
            else:
                score *= 0.4

        if restaurant_prefs and name in restaurant_prefs:
            avg_rest = restaurant_prefs[name]
            if avg_rest >= 4.0:
                score *= 1.3
            elif avg_rest <= 2.5:
                score *= 0.2

        candidates.append(
            {
                "name": name,
                "category": category,
                "rating": rating,
                "menu": menu,
                "summary": summary,
                "place_id": kakao_place_id,
                "image_url": photo_url,        # 대표 1장 (기존 카드용)
                "distance_km": distance_km,
                "keywords": keywords,
                "images": photo_urls,           # ✅ 슬라이더용 여러 장
                "address": address,
                "open_info": open_info,
                "score": score,
            }
        )



    if not candidates:
        if conn:
            conn.close()
        return jsonify([])

    # 6) 최근 2일 내에 이미 추천한 가게는 최대한 제외
    # 6) 최근 2일 내에 이미 추천한 가게는 최대한 제외
    filtered_candidates = []
    if recent_names_2d:
        for c in candidates:
            if c["name"] not in recent_names_2d:
                filtered_candidates.append(c)
    else:
        filtered_candidates = list(candidates)

    # 기본은 최근 2일 안 나온 집들만
    if filtered_candidates:
        pool = list(filtered_candidates)
    else:
        pool = list(candidates)

    # ✅ 최소 3개는 채우기 위해, 부족하면 예전에 추천한 집도 다시 섞어서 포함
    if len(pool) < 3:
        existing_names = {c["name"] for c in pool}
        for c in candidates:
            if c["name"] not in existing_names:
                pool.append(c)
                existing_names.add(c["name"])
            if len(pool) >= 3:
                break

    # 7) 점수 기준 상위 50개 중 랜덤 3개
    pool.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_pool = pool[:50]
    random.shuffle(top_pool)
    picked = top_pool[:3]

    for c in picked:
        c.pop("score", None)


    # 8) 추천 로그 기록
    if phone and conn and cur:
        try:
            for c in picked:
                try:
                    cur.execute(
                        """
                        INSERT INTO recommendation_logs (phone_number, restaurant_name, time_of_day)
                        VALUES (%s, %s, %s);
                        """,
                        (phone, c["name"], time_of_day),
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



if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
