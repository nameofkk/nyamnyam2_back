from asyncio import open_connection
import os
import math
import random
from urllib.parse import quote

from flask import Flask, request, jsonify, redirect, render_template, render_template_string
import psycopg2
from psycopg2.errors import UndefinedColumn
from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv
import requests
import json
from bs4 import BeautifulSoup


# =========================
# 환경 변수 로딩
# =========================
load_dotenv()

# =========================
# 설정값
# =========================

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5433")
DB_NAME = os.getenv("DB_NAME", "restaurant_db")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "1111")  # 네 환경에 맞게 이미 사용 중

CELERY_BROKER = os.getenv("CELERY_BROKER", "redis://127.0.0.1:6379/0")
CELERY_BACKEND = os.getenv("CELERY_BACKEND", "redis://127.0.0.1:6379/0")

KAKAO_KEY = os.getenv("KAKAO_REST_API_KEY")

# === 알리고 / 발송 설정 ===
PROVIDER_URL = os.getenv("PROVIDER_URL")

ALIGO_API_KEY = os.getenv("ALIGO_API_KEY")
ALIGO_USER_ID = os.getenv("ALIGO_USER_ID")
ALIGO_SENDER = os.getenv("ALIGO_SENDER")
ALIGO_TEMPLATE_CODE = os.getenv("ALIGO_TEMPLATE_CODE")
KAKAO_SENDER_KEY = os.getenv("KAKAO_SENDER_KEY")

SEND_MODE = os.getenv("SEND_MODE", "print")  # 기본은 print
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://127.0.0.1:5000")


# =========================
# DB 연결 함수
# =========================

def get_conn():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


# =========================
# 카카오 API: 특정 좌표 주변 맛집 가져오기
# =========================

def fetch_restaurants_from_kakao(lat, lon, radius=3000, query="맛집"):
    """
    카카오 로컬 API로 (lat, lon) 주변 radius(m) 내 맛집 리스트 가져오기.
    """
    if not KAKAO_KEY:
        print("⚠ KAKAO_REST_API_KEY가 설정되어 있지 않습니다.")
        return []

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}

    params = {
        "query": query,
        "y": lat,
        "x": lon,
        "radius": radius,
        "category_group_code": "FD6",  # 음식점
        "size": 15,
        "page": 1,
    }

    all_places = []

    while True:
        res = requests.get(url, headers=headers, params=params)
        if res.status_code != 200:
            print("카카오 API 오류:", res.text)
            break

        data = res.json()
        docs = data.get("documents", [])
        if not docs:
            break

        all_places.extend(docs)

        meta = data.get("meta", {})
        if meta.get("is_end", True):
            break
        params["page"] += 1

    print(f"[카카오] {len(all_places)}개 장소 수집 (lat={lat}, lon={lon})")
    return all_places


# =========================
# 카카오맵 상세페이지에서 실제 평점 가져오기 (스크래핑)
# =========================
def get_kakao_rating(place_id):
    """
    카카오맵 place_id로 실제 평점 가져오기
    """
    try:
        url = f"https://place.map.kakao.com/{place_id}"
        html = requests.get(url, timeout=3).text
        soup = BeautifulSoup(html, "html.parser")

        # 평점이 em.num_rate 안에 들어 있음
        score = soup.select_one("em.num_rate")
        if score:
            return float(score.get_text().strip())

    except Exception as e:
        print(f"[평점 스크래핑 오류] id={place_id} → {e}")

    return None


# =========================
# 카카오 API + 평점 스크래핑 결합 수집 함수
# =========================
def fetch_restaurants_detailed(lat, lon, radius=1500):
    """
    1) 카카오 키워드 API로 주변 맛집(place_id 포함) 수집
    2) 각 place_id로 상세페이지 스크래핑 → 실제 평점 가져오기
    3) 너무 낮은 평점(예: 2.5 미만)만 빼고 다 받아들임
    """
    if not KAKAO_KEY:
        print("⚠ KAKAO_REST_API_KEY 미설정")
        return []

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}

    params = {
        "query": "맛집",
        "y": lat,
        "x": lon,
        "radius": radius,
        "category_group_code": "FD6",
        "size": 15,
        "page": 1,
    }

    collected = []

    while True:
        res = requests.get(url, headers=headers, params=params)
        if res.status_code != 200:
            print("[카카오 API 오류]", res.text)
            break

        data = res.json()
        docs = data.get("documents", [])
        if not docs:
            break

        for d in docs:
            place_id = d.get("id")   # 상세페이지 ID
            if not place_id:
                continue

            # ⭐ 실제 평점 가져오기 시도
            rating = get_kakao_rating(place_id)

            # 1) 스크래핑 실패했으면 일단 4.0 기본값
            if rating is None:
                rating = 4.0

            # 2) 너무 낮은 곳(2.5 미만)만 버리고 나머지는 수용
            if rating < 2.5:
                continue

            # 카테고리 단순화
            category_name = d.get("category_name")
            if category_name and ">" in category_name:
                category_simple = category_name.split(">")[1].strip()
            else:
                category_simple = "기타"

            collected.append({
                "place_id": place_id,
                "name": d.get("place_name"),
                "lat": float(d.get("y")),
                "lon": float(d.get("x")),
                "category": category_simple,
                "rating": float(rating),
            })

        # 마지막 페이지면 중단
        if data.get("meta", {}).get("is_end", True):
            break

        params["page"] += 1

    print(f"[카카오★] 평점2.5+ 매장 {len(collected)}개 수집 완료 (lat={lat}, lon={lon})")
    return collected


# =========================
# 유저 피드백 기반 선호 계산
# =========================

from psycopg2.errors import UndefinedColumn  # 이미 있으면 중복 import 하지 말 것


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
        # user_feedback 테이블이 아직 없거나 컬럼 없을 때 대비
        return {}

    rows = cur.fetchall()
    prefs = {}
    for c, r in rows:
        if c:
            prefs[c] = float(r)
    return prefs


# =========================
# 거리 & 점수 계산
# =========================

def calc_distance(lat1, lon1, lat2, lon2):
    R = 6371
    lat1, lon1, lat2, lon2 = map(float, [lat1, lon1, lat2, lon2])
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def calculate_score(user, restaurant, last_recent_names):
    """
    user: {
      "phone": "...",
      "lat": ...,
      "lon": ...,
      "categories": [...],
      "category_prefs": {"한식": 4.5, ...}
    }
    restaurant: (name, lat, lon, category, rating)
    last_recent_names: 최근 N번 추천된 가게 이름 리스트
    """
    name, rlat, rlon, category, rating = restaurant

    distance_km = calc_distance(user["lat"], user["lon"], rlat, rlon)

    base = (rating or 3.0) * 10
    penalty = distance_km * 1.5
    score = base - penalty

    # 선호 카테고리 가점
    if category in user["categories"]:
        score += 8

    # 피드백 기반 가중치
    prefs = user.get("category_prefs", {})
    if category in prefs:
        avg = prefs[category]  # 1~5
        score *= 0.8 + (avg / 5.0) * 0.7  # 0.8 ~ 1.5배

    # 최근에 추천한 가게면 큰 패널티 (최근 N개)
    if name in last_recent_names:
        score -= 1000

    return score


# =========================
# 메뉴/키워드/요약 텍스트 생성 유틸
# =========================

# =========================
# 메시지 생성
# =========================

def build_message(user, restaurant, time_of_day):
    """
    알리고 템플릿과 100% 동일한 본문 생성용

    [#{time} 추천 맛집 안내]

    오늘 #{place_name} 어때요?  

    오늘 추천 맛집: #{place_name}
    평점: #{rating}
    거리: #{distance}km
    해당 메시지는 고객님께서 신청하신 주변 맛집 추천 알림으로, 고객님이 요청하신 시간대에 발송됩니다.
    """
    name, lat, lon, category, rating = restaurant

    # 거리 계산
    dist = round(calc_distance(user["lat"], user["lon"], lat, lon), 2)

    # ⚠ 알리고 템플릿과 줄/문장 구조를 그대로 맞춤
    msg = (
        f"[{time_of_day} 추천 맛집 안내]\n\n"
        f"오늘 {name} 어때요?\n\n"
        f"오늘 추천 맛집: {name}\n"
        f"평점: {rating}\n"
        f"거리: {dist}km\n"
    )

    # 메시지와 거리(km) 둘 다 리턴
    return msg, dist

    return msg


# =========================
# 카카오(알리고) 발송 함수
# =========================

def send_kakao(phone, msg, time_of_day=None, place_name=None, rating=None, distance_km=None, extra_buttons=None):
    """
    SEND_MODE = print  → 콘솔 출력
    SEND_MODE = aligo → 알리고 알림톡 실제 발송
    """
    if SEND_MODE == "print":
        print("\n[카카오톡(테스트 모드)]")
        print("수신자:", phone)
        print(msg)
        if extra_buttons:
            print("버튼:", extra_buttons)
        print("-" * 40)
        return True

    if SEND_MODE != "aligo":
        print("\n[경고] SEND_MODE 값이 잘못되었습니다. (현재:", SEND_MODE, ")")
        print("수신자:", phone)
        print(msg)
        if extra_buttons:
            print("버튼:", extra_buttons)
        print("-" * 40)
        return False

    payload = {
        "apikey": ALIGO_API_KEY,
        "userid": ALIGO_USER_ID,
        "senderkey": KAKAO_SENDER_KEY,
        "tpl_code": ALIGO_TEMPLATE_CODE,
        "sender": ALIGO_SENDER,
        "receiver_1": phone,
        "subject_1": "맛집 추천 안내",
        "message_1": msg,
    }

    if time_of_day is not None:
        payload["time_1"] = time_of_day
    if place_name is not None:
        payload["place_name_1"] = place_name
    if rating is not None:
        payload["rating_1"] = str(rating)
    if distance_km is not None:
        payload["distance_1"] = str(distance_km)

    # 버튼 추가 (알리고 템플릿에서 버튼 허용되어 있어야 함)
    if extra_buttons:
        btns = []
        for b in extra_buttons:
            btns.append({
                "name": b["name"],
                "linkType": "WL",
                "linkUrl": b["link"],
            })
        payload["button_1"] = json.dumps(btns, ensure_ascii=False)

    try:
        res = requests.post(PROVIDER_URL, data=payload, timeout=5)
        res.raise_for_status()
        data = res.json()
    except Exception as e:
        print("\n[알리고 요청 에러]")
        print(e)
        return False

    code = int(data.get("code", -1))

    if code == 0:
        print("\n[알리고 발송 성공]")
        print(data)
        return True
    else:
        print("\n[알리고 발송 실패]")
        print(data)
        return False


# =========================
# Flask / Celery 설정
# =========================

app = Flask(__name__)
celery = Celery(app.name, broker=CELERY_BROKER, backend=CELERY_BACKEND)
celery.conf.timezone = "Asia/Seoul"
celery.conf.enable_utc = False

# 시간대별 자동 발송 스케줄
celery.conf.beat_schedule = {
    # 아침 08:00
    "send_morning": {
        "task": "send_recommendations",
        "schedule": crontab(hour=8, minute=0),
        "args": ("아침",),
    },
    # 점심 11:00
    "send_lunch": {
        "task": "send_recommendations",
        "schedule": crontab(hour=11, minute=0),
        "args": ("점심",),
    },
    # 저녁 17:00
    "send_dinner": {
        "task": "send_recommendations",
        "schedule": crontab(hour=17, minute=0),
        "args": ("저녁",),
    },
    # 야식 21:00
    "send_night": {
        "task": "send_recommendations",
        "schedule": crontab(hour=21, minute=0),
        "args": ("야식",),
    },
}


# =========================
# Celery 추천 작업
# =========================

@celery.task(name="send_recommendations")
def send_recommendations(time_of_day):
    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            SELECT phone_number, latitude, longitude,
                   preferences_categories, alert_times, is_active
            FROM users;
            """
        )
        users = cur.fetchall()
        pref_mode = True
    except UndefinedColumn:
        cur.execute("SELECT phone_number, latitude, longitude FROM users;")
        users = [(u[0], u[1], u[2], None, None, True) for u in cur.fetchall()]
        pref_mode = False

    for u in users:
        phone, lat, lon, cats, times, is_active = u

        if not is_active:
            continue

        times = times.split(",") if (pref_mode and times) else []

        # 유저가 선택한 시간대가 있다면, 그 시간대만 발송
        if times and time_of_day not in times:
            continue

        msg = (
            f"[{time_of_day} 추천 맛집 알림]\n\n"
            f"오늘의 {time_of_day} 추천 맛집이 도착했어요!\n"
            f"지금 바로 확인해보세요 :)"
        )

        button_url = f"{SERVER_BASE_URL}/reco?phone={phone}&time={time_of_day}"

        send_kakao(
            phone,
            msg,
            time_of_day=time_of_day,
            extra_buttons=[{"name": "맛집 확인하기", "link": button_url}],
        )

    conn.close()
    return {"ok": True}


@celery.task(name="send_feedback_followup")
def send_feedback_followup(phone, time_of_day):
    """
    특정 전화번호·시간대(아침/점심/저녁/야식) 기준으로,
    '오늘 그 시간대에 좋아요 눌렀던 가게들' 리뷰 요청을 한 번만 발송.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        # 안전하게 테이블 보장
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_requests (
                id SERIAL PRIMARY KEY,
                phone_number VARCHAR(15),
                time_of_day TEXT,
                session_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()

        # 이미 오늘 같은 시간대에 보낸 적 있으면 스킵
        cur.execute(
            """
            SELECT 1
            FROM feedback_requests
            WHERE phone_number = %s
              AND time_of_day = %s
              AND session_date = CURRENT_DATE
            LIMIT 1;
            """,
            (phone, time_of_day),
        )
        if cur.fetchone():
            print("[FEEDBACK_FOLLOWUP] already sent today:", phone, time_of_day)
            return {"ok": True, "skipped": "already_sent"}

        # 오늘 + 해당 시간대 + 좋아요(5점 이상) 리스트 가져오기
        cur.execute(
            """
            SELECT restaurant_name, COALESCE(category, ''), MAX(created_at) AS last_time
            FROM user_feedback
            WHERE phone_number = %s
              AND rating >= 5
              AND time_of_day = %s
              AND created_at::date = CURRENT_DATE
            GROUP BY restaurant_name, category
            ORDER BY last_time DESC;
            """,
            (phone, time_of_day),
        )
        rows = cur.fetchall()

        # 해당 시간대에 좋아요가 하나도 없으면 알림도 보내지 않음
        if not rows:
            print("[FEEDBACK_FOLLOWUP] no likes for", phone, time_of_day)
            return {"ok": True, "skipped": "no_likes"}

        # 알림톡 본문 + 버튼 링크
        msg = (
            f"오늘 {time_of_day}에 추천드렸던 맛집은 어떠셨나요?\n"
            f"이용해보신 가게에 별점과 한 줄 후기를 남겨주시면,\n"
            f"다음 {time_of_day} 추천에 더 잘 반영할게요 :)"
        )

        feedback_url = (
            f"{SERVER_BASE_URL}/feedback-form"
            f"?phone={quote(phone)}"
            f"&time={quote(time_of_day)}"
        )

        send_kakao(
            phone,
            msg,
            extra_buttons=[{"name": "평가하기", "link": feedback_url}],
        )

        # 오늘 이 세션에 대해 리뷰요청 보냈다고 기록
        cur.execute(
            """
            INSERT INTO feedback_requests (phone_number, time_of_day, session_date)
            VALUES (%s, %s, CURRENT_DATE);
            """,
            (phone, time_of_day),
        )
        conn.commit()

        return {"ok": True, "sent": True, "count": len(rows)}

    except Exception as e:
        conn.rollback()
        print("[SEND_FEEDBACK_FOLLOWUP_ERROR]", e)
        return {"ok": False}
    finally:
        conn.close()



# =========================
# DB 초기화 라우트
# =========================

@app.route("/init-db")
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # restaurants 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS restaurants (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            category TEXT,
            rating DOUBLE PRECISION,
            place_id TEXT,
            is_open BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # users 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            phone_number VARCHAR(15) UNIQUE NOT NULL,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            preferences_categories TEXT, -- "한식,양식"
            preferences_focus TEXT,       -- "맛", "분위기" 등
            alert_times TEXT,             -- "아침,점심"
            last_alert_sent TIMESTAMP DEFAULT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # user_feedback 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_feedback (
            id SERIAL PRIMARY KEY,
            phone_number VARCHAR(15),
            restaurant_name TEXT,
            category TEXT,
            rating INTEGER CHECK (rating BETWEEN 1 AND 5),
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

        # ✅ time_of_day 컬럼 없으면 추가
    cur.execute(
        """
        ALTER TABLE user_feedback
        ADD COLUMN IF NOT EXISTS time_of_day TEXT;
        """
    )

    # recommendation_logs 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recommendation_logs (
            id SERIAL PRIMARY KEY,
            phone_number VARCHAR(15),
            restaurant_name TEXT,
            time_of_day TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # click_logs 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS click_logs (
            id SERIAL PRIMARY KEY,
            phone_number VARCHAR(15),
            place_id TEXT,
            place_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

        # ✅ 세션별 리뷰요청 발송 여부 기록 테이블
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS feedback_requests (
            id SERIAL PRIMARY KEY,
            phone_number VARCHAR(15),
            time_of_day TEXT,
            session_date DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # 기존 restaurants 테이블에 place_id 컬럼이 없다면 추가
    cur.execute(
        """
        ALTER TABLE restaurants
        ADD COLUMN IF NOT EXISTS place_id TEXT;
        """
    )

    conn.commit()
    cur.close()
    conn.close()
    return "✅ DB 테이블 생성/업데이트 완료"


# =========================
# 카카오 기반 맛집 수동 수집용
# =========================

@app.route("/sync-kakao", methods=["POST"])
def sync_kakao():
    data = request.get_json()
    lat = data.get("latitude")
    lon = data.get("longitude")
    radius = data.get("radius", 1000)

    if lat is None or lon is None:
        return jsonify({"error": "latitude, longitude 필요"}), 400

    places = fetch_restaurants_detailed(float(lat), float(lon), radius=radius)

    conn = get_conn()
    cur = conn.cursor()

    for p in places:
        name = p["name"]
        y = p["lat"]
        x = p["lon"]
        category_simple = p["category"]
        rating = p["rating"]
        place_id = p["place_id"]

        cur.execute(
            """
            INSERT INTO restaurants (name, latitude, longitude, category, rating, place_id, is_open)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (name)
            DO UPDATE SET
              latitude = EXCLUDED.latitude,
              longitude = EXCLUDED.longitude,
              category = EXCLUDED.category,
              rating = EXCLUDED.rating,
              place_id = EXCLUDED.place_id;
            """,
            (name, y, x, category_simple, rating, place_id),
        )

    conn.commit()
    conn.close()

    return jsonify({"status": "ok", "count": len(places)})


# =========================
# 샘플 시드용
# =========================

@app.route("/seed-restaurants")
def seed_restaurants():
    conn = get_conn()
    cur = conn.cursor()

    sample = [
        ("샘플 김치찌개집", 37.5665, 126.9780, "한식", 4.5, True),
        ("샘플 파스타집", 37.5660, 126.9790, "양식", 4.7, True),
    ]

    for name, lat, lon, cat, rating, is_open in sample:
        cur.execute(
            """
            INSERT INTO restaurants (name, latitude, longitude, category, rating, is_open)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
              latitude = EXCLUDED.latitude,
              longitude = EXCLUDED.longitude,
              category = EXCLUDED.category,
              rating = EXCLUDED.rating,
              is_open = EXCLUDED.is_open;
            """,
            (name, lat, lon, cat, rating, is_open),
        )

    conn.commit()
    cur.close()
    conn.close()
    return "✅ 샘플 맛집 시드 완료"


# =========================
# 기본 라우트
# =========================

@app.route("/")
def home():
    return redirect("/signup")


@app.route("/send-now")
def send_now():
    t = request.args.get("t", "점심")
    send_recommendations.delay(t)
    return t + " 추천 발송 작업 시작됨"


# =========================
# 회원가입 페이지
# =========================

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


# =========================
# 회원 등록 API
# =========================

@app.route("/register", methods=["POST"])
def register_user():
    data = request.get_json()
    phone = data.get("phone_number")
    lat = data.get("latitude")
    lon = data.get("longitude")
    categories = data.get("preferences_categories", [])
    focus = data.get("preferences_focus", "")
    alert_times = data.get("alert_times", [])

    if not phone or lat is None or lon is None:
        return jsonify({"success": False, "message": "phone_number, latitude, longitude는 필수입니다."}), 400

    if not str(phone).isdigit():
        return jsonify({"success": False, "message": "휴대폰 번호는 숫자만 입력 가능합니다."}), 400

    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "위도/경도가 잘못 전달되었습니다."}), 400

    cats_str = ",".join(categories) if categories else ""
    times_str = ",".join(alert_times) if alert_times else ""

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO users (
            phone_number, latitude, longitude,
            preferences_categories, preferences_focus,
            alert_times, is_active
        )
        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT (phone_number)
        DO UPDATE SET
          latitude = EXCLUDED.latitude,
          longitude = EXCLUDED.longitude,
          preferences_categories = EXCLUDED.preferences_categories,
          preferences_focus = EXCLUDED.preferences_focus,
          alert_times = EXCLUDED.alert_times,
          is_active = TRUE;
        """,
        (phone, lat, lon, cats_str, focus, times_str),
    )

    print(f"[REGISTER] {phone} -> lat={lat}, lon={lon}")

    places = fetch_restaurants_detailed(lat, lon, radius=1000)

    for p in places:
        name = p["name"]
        y = p["lat"]
        x = p["lon"]
        category_simple = p["category"]
        rating = p["rating"]
        place_id = p["place_id"]

        cur.execute(
            """
            INSERT INTO restaurants (name, latitude, longitude, category, rating, place_id, is_open)
            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT (name)
            DO UPDATE SET
              latitude = EXCLUDED.latitude,
              longitude = EXCLUDED.longitude,
              category = EXCLUDED.category,
              rating = EXCLUDED.rating,
              place_id = EXCLUDED.place_id;
            """,
            (name, y, x, category_simple, rating, place_id),
        )

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({
        "success": True,
        "message": "✅ 신청/정보 업데이트 및 주변 맛집 데이터 수집이 완료되었습니다.",
        "count": len(places)
    })


# =========================
# 피드백 API
# =========================

@app.route("/api/quick-feedback", methods=["POST"])
def api_quick_feedback():
    conn = None
    try:
        data = request.get_json()
        phone = data.get("phone")
        name = data.get("name")
        category = data.get("category")
        like = data.get("like")
        time_of_day = data.get("time_of_day")  # ✅ 추가


        if not phone or not name:
            return jsonify({"ok": False, "message": "phone/name 필요"}), 400

        # 좋아요면 5점, 싫어요면 1점
        rating = 5 if like else 1

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO user_feedback
            (phone_number, restaurant_name, category, rating, time_of_day)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (phone, name, category, rating, time_of_day),
        )

        conn.commit()

        # ✅ 좋아요인 경우에만 1시간 뒤 카카오톡 리뷰 요청 예약
        if like and time_of_day:
            try:
                # 60 * 60 = 3600초 = 1시간
                send_feedback_followup.apply_async(
                    args=[phone, time_of_day],
                    countdown=60 * 60,
                )
            except Exception as e:
                print("[FEEDBACK_FOLLOWUP_DISPATCH_ERROR]", e)

        return jsonify({"ok": True})

    except Exception as e:
        print("[QUICK_FEEDBACK_ERROR]", e)
        return jsonify({"ok": False, "message": "서버 오류로 피드백 저장 실패"}), 500

    finally:
        if conn:
            conn.close()


@app.route("/feedback-form")
def feedback_form():
    phone = request.args.get("phone", "").strip()
    time_of_day = request.args.get("time", "").strip()  # ✅ 추가

    if not phone:
        return "잘못된 접근입니다. (phone 파라미터 없음)", 400

    conn = None
    rows = []
    try:
        conn = get_conn()
        cur = conn.cursor()

        if time_of_day:
            # ✅ 오늘 + 해당 시간대 + 좋아요 리스트만
            cur.execute(
                """
                SELECT restaurant_name, COALESCE(category, ''), MAX(created_at) AS last_time
                FROM user_feedback
                WHERE phone_number = %s
                  AND rating >= 5
                  AND time_of_day = %s
                  AND created_at::date = CURRENT_DATE
                GROUP BY restaurant_name, category
                ORDER BY last_time DESC
                LIMIT 10;
                """,
                (phone, time_of_day),
            )
        else:
            # (fallback) 전체 좋아요 최근 10개 (기존 동작 유지)
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

    # 이하 HTML 렌더링 부분은 그대로 사용

    # rows: [(name, category, last_time), ...]
    # HTML 생성
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
        # 폼에서 넘어온 값들
        phone = request.form.get("phone_number")
        restaurant = request.form.get("restaurant_name")
        category = request.form.get("category") or None
        rating_raw = request.form.get("rating")
        comment = request.form.get("comment") or ""

        # 필수값 체크
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
            # 1차 시도: comment 컬럼 포함 INSERT
            cur.execute(
                """
                INSERT INTO user_feedback
                (phone_number, restaurant_name, category, rating, comment)
                VALUES (%s, %s, %s, %s, %s);
                """,
                (phone, restaurant, category, rating, comment),
            )
        except UndefinedColumn:
            # comment 컬럼이 없어서 난 오류 → 컬럼 추가 후 다시 시도
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
    return """
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>피드백 제출 완료</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body {
          font-family: "Noto Sans KR", sans-serif;
          background: #f5f5f5;
          display: flex;
          justify-content: center;
          align-items: center;
          min-height: 100vh;
          margin: 0;
        }
        .wrap {
          background: white;
          width: 90%;
          max-width: 420px;
          padding: 24px 20px;
          border-radius: 18px;
          box-shadow: 0 12px 30px rgba(0,0,0,0.08);
          text-align: center;
        }
        h2 { font-size: 18px; margin-bottom: 8px; }
        p  { font-size: 14px; color: #666; }
      </style>
    </head>
    <body>
      <div class="wrap">
        <h2>피드백 감사합니다!</h2>
        <p>남겨주신 별점과 후기가 다음 추천에 반영됩니다 :)</p>
      </div>
    </body>
    </html>
    """


# =========================
# /reco 랜딩 페이지
# =========================
@app.route("/reco")
def reco_page():
    phone = request.args.get("phone", "")
    time = request.args.get("time", "")

    # 여기서부터는 templates/reco.html 파일을 사용함
    return render_template("reco.html", phone=phone, time=time)

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


@app.route("/go")
def go_redirect():
    phone = request.args.get("phone", "")
    pid = request.args.get("place_id", "")
    name = request.args.get("name", "")

    if not pid or pid.lower() in ("null", "none"):
        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT latitude, longitude
            FROM restaurants
            WHERE name = %s
            ORDER BY id DESC
            LIMIT 1;
            """,
            (name,),
        )
        row = cur.fetchone()
        conn.close()

        if row:
            lat, lon = row
            kakao_url = f"https://map.kakao.com/link/map/{quote(name)},{lat},{lon}"
            return redirect(kakao_url)
        else:
            search_url = f"https://map.kakao.com/?q={quote(name)}"
            return redirect(search_url)

    kakao_url = f"https://place.map.kakao.com/{pid}"
    return redirect(kakao_url)


from psycopg2.extras import RealDictCursor


# =========================
# 메뉴 / 키워드 / 요약 텍스트 유틸
# =========================

def build_menu_text(name, category):
    if category:
        return f"{category} 위주의 인기 메뉴를 즐길 수 있는 곳이에요."
    return "다양한 메뉴를 즐길 수 있는 곳이에요."


def build_keywords(category, rating, distance_km, preferred, review_text=None):
    tags = []

    if preferred:
        tags.append("취향저격")

    if distance_km is not None:
        if distance_km <= 0.5:
            tags.append("도보5분이내")
        elif distance_km <= 1.5:
            tags.append("가까운거리")
        else:
            tags.append("근처맛집")

    if rating is not None:
        if rating >= 4.5:
            tags.append("평점4.5이상")
        elif rating >= 4.0:
            tags.append("평점좋은")

    if category:
        tags.append(category)

    if review_text:
        t = review_text
        if "양" in t or "푸짐" in t:
            tags.append("푸짐한양")
        if "친절" in t:
            tags.append("친절한서비스")
        if "가성비" in t or "가격" in t:
            tags.append("가성비좋음")
        if "분위기" in t or "인테리어" in t:
            tags.append("분위기좋음")

    tags = list(dict.fromkeys(tags))
    return tags


def build_summary_text(name, category, rating, distance_km):
    parts = []

    if category:
        parts.append(f"{name}은(는) {category} 메뉴를 즐길 수 있는 곳입니다.")
    else:
        parts.append(f"{name}은(는) 다양한 메뉴를 즐길 수 있는 곳입니다.")

    if rating is not None:
        if rating >= 4.5:
            parts.append("손님들 평점이 특히 높은 편이고,")
        elif rating >= 4.0:
            parts.append("평균 이상 좋은 평점을 받고 있으며,")

    if distance_km is not None:
        if distance_km <= 0.5:
            parts.append("현재 위치와 매우 가까워 가볍게 방문하기 좋습니다.")
        elif distance_km <= 1.5:
            parts.append("무리 없이 걸어가거나 짧게 이동해서 방문하기 좋습니다.")
        else:
            parts.append("조금 이동은 필요하지만 방문해볼 만한 거리입니다.")
    else:
        parts.append("방문 거리는 보통 수준입니다.")

    return " ".join(parts)


# =========================
# 카카오맵 스크래핑 유틸
# =========================

def get_kakao_images(place_id, limit=5):
    url = f"https://place.map.kakao.com/{place_id}"
    urls = []
    try:
        html = requests.get(url, timeout=3).text
        soup = BeautifulSoup(html, "html.parser")

        photo_imgs = soup.select(
            ".photo_area img, .photo_viewer img, .gallery_photo img, .link_photo img"
        )
        for img in photo_imgs:
            src = img.get("src") or img.get("data-src")
            if not src:
                continue

            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = "https://place.map.kakao.com" + src

            if src not in urls:
                urls.append(src)
            if len(urls) >= limit:
                break

        if not urls:
            for img in soup.select("img"):
                src = img.get("src") or img.get("data-src")
                if not src:
                    continue

                if src.startswith("//"):
                    src = "https:" + src
                elif src.startswith("/"):
                    src = "https://place.map.kakao.com" + src

                if src not in urls:
                    urls.append(src)
                if len(urls) >= limit:
                    break

        if not urls:
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                urls.append(og["content"])

        print(f"[이미지 URL] place_id={place_id} → {urls[:limit]}")

    except Exception as e:
        print(f"[이미지 스크래핑 오류] id={place_id} → {e}")

    return urls[:limit]


def get_kakao_image(place_id):
    images = get_kakao_images(place_id, limit=1)
    return images[0] if images else None


def summarize_review(place_id, max_len=100, empty_text="리뷰 정보 없음"):
    url = f"https://place.map.kakao.com/{place_id}"
    try:
        html = requests.get(url, timeout=3).text
        soup = BeautifulSoup(html, "html.parser")

        candidates = soup.select(".txt_comment, .comment_info .txt_comment")
        texts = [c.get_text().strip() for c in candidates[:3] if c.get_text().strip()]

        if not texts:
            return empty_text

        joined = " ".join(texts)
        if len(joined) > max_len:
            joined = joined[:max_len].rstrip() + "..."

        return joined
    except Exception as e:
        print(f"[리뷰 요약 오류] id={place_id} → {e}")
        return empty_text


def get_kakao_basic_info(place_id):
    url = f"https://place.map.kakao.com/{place_id}"
    address = None
    open_info = None
    is_open = None

    try:
        html = requests.get(url, timeout=3).text
        soup = BeautifulSoup(html, "html.parser")

        addr_el = soup.select_one(
            ".location_detail .txt_address, .tit_address, .addr, .txt_address"
        )
        if addr_el:
            address = addr_el.get_text().strip()

        time_el = soup.select_one(
            ".openhour_wrap .time_operation, .info_oper, .txt_operation"
        )
        if time_el:
            open_info = time_el.get_text().strip()

        text_block = ""
        if time_el:
            text_block += time_el.get_text(" ", strip=True) + " "
        all_text = soup.get_text(" ", strip=True)
        text_block += all_text

        if "영업중" in text_block:
            is_open = True
        elif "영업시간 종료" in text_block or "영업 종료" in text_block or "휴무" in text_block:
            is_open = False

    except Exception as e:
        print(f"[기본정보 스크래핑 오류] id={place_id} → {e}")

    return address, open_info, is_open


# =========================
# IP 기반 위치 API
# =========================

@app.route("/api/ip-location", methods=["GET"])
def api_ip_location():
    """
    클라이언트 IP 기반 대략 위치 추정
    - geolocation 실패 시 fallback 용도
    """
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip:
        ip = ip.split(",")[0].strip()

    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}?lang=ko", timeout=3)
        data = resp.json()
        if data.get("status") != "success":
            return jsonify({
                "lat": 33.462046,
                "lon": 126.329235,
                "source": "fallback"
            })

        return jsonify({
            "lat": data.get("lat"),
            "lon": data.get("lon"),
            "city": data.get("city"),
            "region": data.get("regionName"),
            "country": data.get("country"),
            "source": "ip-api"
        })
    except Exception as e:
        print("[IP-LOCATION ERROR]", e)
        return jsonify({
            "lat": 33.462046,
            "lon": 126.329235,
            "source": "error-fallback"
        })


# =========================
# /api/reco 메인 추천 API
# =========================

@app.route("/api/reco", methods=["POST"])
def api_reco():
    """
    위치 + 유저 취향 기반 맛집 추천
    - DB 기준 상위 N개에서 랜덤 3개
    - DB에 없으면 카카오 실시간 검색
    - user_reco_history 테이블에 노출 이력 저장
    - 같은 전화번호/시간대(time)에 최근 2일간 보여준 place_id는 가급적 제외
    - 카카오 스크래핑 기준 '영업시간 종료/휴무'로 추정되는 가게는 제외
    """
    data = request.get_json() or {}
    phone = data.get("phone") or ""
    time_of_day = data.get("time") or ""

    lat = data.get("lat")
    lon = data.get("lon")

    print("[DEBUG] /api/reco lat,lon =", lat, lon)

    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return jsonify({"error": "위치 정보가 잘못되었습니다."}), 400

    conn = get_conn()
    cur = conn.cursor()

    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_reco_history (
                id SERIAL PRIMARY KEY,
                phone_number VARCHAR(50),
                time_of_day VARCHAR(20),
                place_id VARCHAR(50),
                created_at TIMESTAMP DEFAULT NOW()
            );
            """
        )
        conn.commit()

        if phone:
            cur.execute(
                """
                SELECT place_id
                FROM user_reco_history
                WHERE phone_number = %s
                  AND time_of_day = %s
                  AND created_at >= NOW() - INTERVAL '2 days';
                """,
                (phone, time_of_day),
            )
            history_rows = cur.fetchall()
            shown_place_ids = {r[0] for r in history_rows if r[0]}
        else:
            shown_place_ids = set()

        feedback_prefs = get_user_prefs(phone, cur) if phone else {}

        cur.execute(
            """
            SELECT preferences_categories
            FROM users
            WHERE phone_number = %s
            """,
            (phone,),
        )
        row = cur.fetchone()
        base_prefs = row[0].split(",") if row and row[0] else []

        def pref_boost(category: str) -> float:
            if not category:
                return 0.0

            fb_score = feedback_prefs.get(category)
            if fb_score is not None:
                return (fb_score - 3.0) * 2.0

            if category in base_prefs:
                return 2.0

            return 0.0

        cur.execute(
            """
            SELECT name, latitude, longitude, category, rating, place_id
            FROM restaurants
            WHERE rating >= 3.5;
            """
        )
        rows = cur.fetchall()
        print("[DEBUG] restaurants rows (rating>=3.5):", len(rows))

        scored_all = []
        for name, rlat, rlon, category, rating, pid in rows:
            if not pid:
                continue

            dist = calc_distance(lat, lon, rlat, rlon)
            if dist > 3.0:
                continue

            base_score = (rating or 0) * 10 - dist
            score = base_score + pref_boost(category)
            scored_all.append((score, name, rlat, rlon, category, rating, pid))
        print("[DEBUG] candidates within 3km:", len(scored_all))

        result = []
        TOP_N = 30

        if scored_all:
            scored_all.sort(reverse=True, key=lambda x: x[0])

            def filter_by_history(candidates):
                filtered = []
                for item in candidates:
                    _, _, _, _, _, _, pid = item
                    if pid in shown_place_ids:
                        continue
                    filtered.append(item)
                return filtered

            candidates = scored_all[:TOP_N]
            filtered = filter_by_history(candidates)
            use_list = filtered if filtered else candidates

            if len(use_list) > 3:
                random.shuffle(use_list)
                picked = use_list[:3]
            else:
                picked = use_list

            for _, name, rlat, rlon, category, rating, pid in picked:
                distance_km = round(calc_distance(lat, lon, rlat, rlon), 1)
                preferred = category in base_prefs

                address, open_info, is_open = get_kakao_basic_info(pid)
                if is_open is False:
                    print(f"[RECO] {name} (pid={pid}) 영업 종료/휴무 추정 → 제외")
                    continue

                images = get_kakao_images(pid)
                image_url = images[0] if images else get_kakao_image(pid)

                base_menu = build_menu_text(name, category)
                base_summary = build_summary_text(name, category, rating, distance_km)

                menu = summarize_review(
                    pid,
                    max_len=60,
                    empty_text=base_menu,
                )
                summary = summarize_review(
                    pid,
                    max_len=140,
                    empty_text=base_summary,
                )

                keywords = build_keywords(
                    category,
                    rating,
                    distance_km,
                    preferred,
                    review_text=summary,
                )

                result.append(
                    {
                        "name": name,
                        "category": category,
                        "rating": rating,
                        "menu": menu,
                        "summary": summary,
                        "place_id": pid,
                        "image_url": image_url,
                        "distance_km": distance_km,
                        "keywords": keywords,
                        "images": images,
                        "address": address,
                        "open_info": open_info,
                    }
                )

                if phone and pid:
                    cur.execute(
                        """
                        INSERT INTO user_reco_history (phone_number, time_of_day, place_id)
                        VALUES (%s, %s, %s);
                        """,
                        (phone, time_of_day, pid),
                    )

            conn.commit()
            return jsonify(result)

        print("[API/RECO] DB 기준 3km 안 식당 없음 → 카카오 API 실시간 조회 사용")
        kakao_places = fetch_restaurants_detailed(lat, lon, radius=3000)

        scored_all_kakao = []
        for p in kakao_places:
            pid = p.get("place_id")
            if not pid:
                continue

            distance_km = round(
                calc_distance(lat, lon, p["lat"], p["lon"]), 1
            )
            base_score = (p["rating"] or 0) * 10 - distance_km
            score = base_score + pref_boost(p.get("category"))
            scored_all_kakao.append((score, p, distance_km))

        if not scored_all_kakao:
            return jsonify([])

        scored_all_kakao.sort(reverse=True, key=lambda x: x[0])

        candidates = scored_all_kakao[:TOP_N]
        filtered = []
        for score, p, distance_km in candidates:
            pid = p.get("place_id")
            if pid in shown_place_ids:
                continue
            filtered.append((score, p, distance_km))

        use_list = filtered if filtered else candidates

        if len(use_list) > 3:
            random.shuffle(use_list)
            picked = use_list[:3]
        else:
            picked = use_list

        for _, p, distance_km in picked:
            pid = p["place_id"]

            address, open_info, is_open = get_kakao_basic_info(pid)
            if is_open is False:
                print(f"[RECO] {p['name']} (pid={pid}) 영업 종료/휴무 추정 → 제외")
                continue

            images = get_kakao_images(pid)
            image_url = images[0] if images else get_kakao_image(pid)

            base_menu = build_menu_text(p["name"], p["category"])
            base_summary = build_summary_text(
                p["name"], p["category"], p["rating"], distance_km
            )

            menu = summarize_review(
                pid,
                max_len=60,
                empty_text=base_menu,
            )
            summary = summarize_review(
                pid,
                max_len=140,
                empty_text=base_summary,
            )

            keywords = build_keywords(
                p["category"],
                p["rating"],
                distance_km,
                False,
                review_text=summary,
            )

            result.append(
                {
                    "name": p["name"],
                    "category": p["category"],
                    "rating": p["rating"],
                    "menu": menu,
                    "summary": summary,
                    "place_id": pid,
                    "image_url": image_url,
                    "distance_km": distance_km,
                    "keywords": keywords,
                    "images": images,
                    "address": address,
                    "open_info": open_info,
                }
            )

            if phone and pid:
                cur.execute(
                    """
                    INSERT INTO user_reco_history (phone_number, time_of_day, place_id)
                    VALUES (%s, %s, %s);
                    """,
                    (phone, time_of_day, pid),
                )

        conn.commit()
        return jsonify(result)

    except Exception as e:
        print("[API/RECO ERROR]", e)
        conn.rollback()
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500
    finally:
        conn.close()


@app.route("/debug/restaurants")
def debug_restaurants():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT name, latitude, longitude FROM restaurants LIMIT 10;")
        rows = cur.fetchall()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)})


# =========================
# 메인
# =========================

if __name__ == "__main__":
    print("🚀 Flask 서버 시작 (SEND_MODE:", SEND_MODE, ")")
    app.run(host="0.0.0.0", port=5000, debug=True)
