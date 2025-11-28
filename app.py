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
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "AIzaSyAox_CWmpe4klOp48vfgRk9JX8vTAQ_guard")
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


def send_alimtalk(template_code, receiver, subject, message,
                  btn_mobile_url=None, btn_pc_url=None, btn_name="자세히 보기"):
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
        "subject_1": subject,
        "message_1": message,
        "failover": "N",
    }

    # 테스트 모드
    if ALIGO_TESTMODE.upper() == "Y":
        payload["testMode"] = "Y"

    # 버튼(웹 링크) 세팅
    if btn_mobile_url or btn_pc_url:
        mobile = btn_mobile_url or btn_pc_url
        pc = btn_pc_url or btn_mobile_url or btn_mobile_url
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
    """1) 웰컴 알림톡 (템플릿코드 UD_8456)"""
    if not phone:
        return False, {"msg": "NO_PHONE"}

    # 템플릿에 등록한 제목과 최대한 동일하게
    # (subject_1는 템플릿 검사 대상이 아니지만, 보기 좋게 맞춰줌)
    subject = "#{emtitle_1}냠냠이 맛집 알림 서비스 안내"

    # ⚠ message_1 은 템플릿 본문과 동일해야 함
    #   버튼 JSON은 여기 넣지 않고 send_alimtalk에서 button_1로 전송
    message = (
        "냠냠이 서비스를 신청 해주셔서 감사 드립니다(축하).\n"
        "앞으로 고객님께서 신청하신 취향/시간대 별로 주변 맛집을 골라서 추천 드릴 예정입니다!\n"
        "우리 같이 맛있는 생활 해봐요. 냠냠(밥)"
    )

    # 웰컴 템플릿은 버튼이 '채널 추가(AC)' 고정이라면
    # 여기서 btn_* 를 안 보내도 되고, 보내도 링크만 무시될 수 있음.
    # (템플릿 버튼이 고정 AC라면 그냥 버튼 없이 보내도 템플릿 버튼이 노출됨)
    return send_alimtalk("UD_8456", phone, subject, message)


def send_reco_message(phone: str, time_label: str):
    """2) 맛집 추천 알림톡 (템플릿코드 UD_8444)"""
    if not phone or not time_label:
        return False, {"msg": "PARAM_ERROR"}

    base_url = BASE_SERVER_URL.rstrip("/") if BASE_SERVER_URL else ""
    link = f"{base_url}/reco?phone={phone}&time={time_label}"

    # 템플릿 제목과 동일하게
    subject = "오늘의 추천 맛집이 도착했어요"

    # 템플릿 본문과 동일 (#{time}, #{phone_number} 는 템플릿 변수로 그대로 둠)
    message = (
        "냠냠, 오늘의 추천 #{time} 맛집이 도착했어요!\n"
        "오늘은 어떤 음식을 먹어 볼까요?\n"
        "알림 받는 연락처: #{phone_number}"
    )

    # 버튼 정보는 send_alimtalk에서 button_1 JSON으로 전달
    # 템플릿에 등록된 버튼:
    # name: "과연 오늘의 밥은?"
    # linkType: "WL"
    # linkPc / linkMo: https://nyamnyam2-back.onrender.com/reco?phone=#{phone}&time=#{time}
    # → 여기서는 #{phone}, #{time} 자리에 실제 값이 들어간 동일 형식의 URL을 보냄
    return send_alimtalk(
        "UD_8444",
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

    # 템플릿에 등록한 제목과 일치
    subject = "#{emtitle_1}오늘 방문하신 맛집은 어떠셨나요?"

    # 템플릿 본문과 동일 (문구 주의)
    message = (
        "피드백과 리뷰를 남겨 주시면 다음 추천때 냠냠이가 고객님의 취향에 알맞는 음식점을 잘 찾아드려요!"
    )

    # 템플릿 버튼:
    # name: "피드백 남기러가기!"
    # linkType: "WL"
    # linkPc / linkMo: https://nyamnyam2-back.onrender.com/feedback-form?phone=#{phone}&time=#{time}
    # → 여기서는 #{phone}, #{time} 자리에 실제 값이 들어간 동일 형식의 URL을 보냄
    return send_alimtalk(
        "UD_8446",
        phone,
        subject,
        message,
        btn_mobile_url=link,
        btn_pc_url=link,
        btn_name="피드백 남기러가기!",
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


import re

# =========================
# 리뷰 분석 / 대표메뉴 / 요약
# =========================

def extract_menu_from_review(review_text):
    # 한글 메뉴 키워드 (분식 쪽 강화)
    menu_keywords = [
        # 한식/분식
        "김치찌개", "된장찌개", "불고기", "삼겹살", "갈비",
        "냉면", "비빔밥", "떡볶이", "라볶이", "튀김", "순대", "김밥",
        "칼국수", "국수",
        # 일식
        "초밥", "스시", "라멘", "우동", "돈카츠", "텐동",
        # 중식
        "짜장면", "짬뽕", "탕수육", "마라탕",
        # 양식
        "파스타", "피자", "리조또", "스테이크",
        # 기타
        "치킨", "버거", "뷔페"
        # ❌ 케이크/디저트는 분식집에 섞이는 걸 막기 위해 뺌
    ]

    found = []
    for kw in menu_keywords:
        if kw in review_text:
            found.append(kw)

    # 영어 메뉴 단어 → 한글 매핑 (케이크/디저트 제외)
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
        # "cake": "케이크",   # ← 제거
        # "dessert": "디저트",# ← 제거
        "buffet": "뷔페",
    }
    lower = review_text.lower()
    for eng, kor in eng_map.items():
        if eng in lower:
            found.append(kor)

    # 중복 제거
    return list(dict.fromkeys(found))



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
    body {
      font-family: "Noto Sans KR", sans-serif;
      background: linear-gradient(180deg, #ffeaf5, #e3f0ff);
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
    }

    .wrap {
      width: 95%;
      max-width: 480px;
      background: #ffffff;
      padding: 32px 24px 34px;
      border-radius: 26px;
      box-shadow: 0 16px 45px rgba(0, 0, 0, 0.08);
      text-align: center;
    }

    .logo {
      width: 80px;
      margin: 0 auto 10px;
      display: block;
    }

    h1 {
      font-size: 21px;
      margin-bottom: 6px;
    }

    .subtitle {
      font-size: 13px;
      color: #666;
      margin-bottom: 18px;
      line-height: 1.6;
    }

    .phone-block {
      margin: 16px 0 14px;
      text-align: left;
    }

    .phone-label {
      font-size: 13px;
      color: #555;
      margin-left: 8%;
    }

    .phone-input {
      width: 84%;
      margin: 6px auto 0;
      display: block;
      padding: 13px 14px;
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
      text-align: center;
    }

    .chips-row {
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 8px;
      margin-bottom: 4px;
    }

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 6px 14px;
      border-radius: 999px;
      border: 1px solid #ddd;
      font-size: 13px;
      cursor: pointer;
      background: #fafafa;
    }

    .chip input {
      margin: 0;
    }

    .chip span {
      padding-top: 1px;
    }

    .btn {
      width: 84%;
      margin: 10px auto 0;
      display: block;
      padding: 12px 0;
      border-radius: 999px;
      border: none;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
    }

    .btn-location {
      background: #f4f4f4;
      color: #333;
      margin-top: 8px;
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
    }

    .location-help {
      font-size: 12px;
      color: #777;
      margin-top: 8px;
      line-height: 1.5;
    }

    .agreements {
      width: 84%;
      margin: 10px auto 0;
      font-size: 11px;
      color: #777;
      text-align: center;
      line-height: 1.5;
    }

    .agreements label {
      display: inline-flex;
      align-items: flex-start;
      justify-content: center;
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
    }

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

  </br>
  <button class="btn btn-location" onclick="getLocation()">📍 현재 위치 설정</button>

  <p class="location-help">
    기본적으로 현재 위치를 기반으로 주변 맛집을 추천 드리며,<br>
    현재 위치를 받지 못할 경우, 신청 시 설정한 위치 기반 주변 맛집 안내를 발송 드립니다.<br>
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

    // 개선본 쪽에서는 /api/save-user 대신 기존 /register를 그대로 쓰는 경우,
    // 아래 fetch URL만 /register로 맞춰주면 됩니다.
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
    phone = request.args.get("phone", "")
    time_of_day = request.args.get("time", "")
    return render_template("reco.html", phone=phone, time=time_of_day)

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
            INSERT INTO user_feedback (phone_number, restaurant_name, category, rating)
            VALUES (%s, %s, %s, %s)
            """,
            (phone, name, category, rating),
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
                (phone_number, restaurant_name, category, rating, comment)
                VALUES (%s, %s, %s, %s, %s);
                """,
                (phone, restaurant, category, rating, comment),
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
                (phone_number, restaurant_name, category, rating, comment)
                VALUES (%s, %s, %s, %s, %s);
                """,
                (phone, restaurant, category, rating, comment),
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
    이런 식으로 호출.

    - recommendation_logs 에 오늘 날짜 + 해당 time(아침/점심...)으로
      추천이 나갔던 사용자들만 골라서 피드백 알림톡 발송
    - 실제 "언제" 보낼지는 크론 스케줄(예: 추천 후 2시간 뒤)에 맞추면 됨
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
            SELECT DISTINCT phone_number
            FROM recommendation_logs
            WHERE time_of_day = %s
              AND created_at::date = CURRENT_DATE
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
