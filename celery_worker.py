import os
from celery import Celery
from datetime import datetime

# Celery 앱 인스턴스를 생성합니다.
# 이 설정은 app.py에서 다시 업데이트되지만, 인스턴스 자체는 여기서 정의됩니다.
celery = Celery(
    'restaurant_alerts', 
    broker=os.getenv("CELERY_BROKER_URL"),
    backend=os.getenv("CELERY_RESULT_BACKEND")
)

celery.conf.update(
    # CeleryBeat가 태스크를 찾을 수 있도록 설정합니다.
    timezone='Asia/Seoul',
    enable_utc=True,
    task_routes = {
        'restaurant.alert.*': {'queue': 'default_queue'}
    }
)

# Celery 작업 (Task) 정의
@celery.task(bind=True, name='restaurant.alert.send')
def send_recommendation_alert(self, time_of_day):
    """
    사용자들에게 맞춤형 맛집 추천 알림톡을 발송하는 Celery 작업입니다.
    """
    # 순환 참조(Circular Import)를 해결하기 위해, 필요한 함수를 Task 내부에서 가져옵니다.
    # 이렇게 하면 모듈이 로드되는 시점이 아닌, Task가 실제로 실행되는 시점에 임포트됩니다.
    try:
        from app import get_db_connection, recommend_restaurant 
    except ImportError as e:
        print(f"ERROR: app.py 함수 임포트 실패 (순환 참조 오류): {e}")
        # 임포트 실패 시 Task를 실패로 표시하고 재시도
        raise self.retry(exc=e, countdown=5, max_retries=3)


    print(f"⏰ Task 시작: {time_of_day} 추천 알림 발송")
    
    # 1. DB 연결 (Task 실행마다 연결)
    conn = None
    try:
        conn = get_db_connection()
    except Exception as e:
        print(f"DB 연결 실패: {e}")
        raise self.retry(exc=e, countdown=10, max_retries=5)
    
    # 2. 알림을 받을 사용자 목록 조회
    users = []
    try:
        cur = conn.cursor()
        # is_active가 TRUE인 사용자만 조회
        cur.execute("SELECT phone_number, latitude, longitude FROM users WHERE is_active = TRUE;")
        users = cur.fetchall()
        cur.close()
    except Exception as e:
        print(f"사용자 조회 중 오류 발생: {e}")
        conn.close()
        return f"사용자 조회 실패: {e}"

    if not users:
        print("알림을 받을 활성 사용자가 없습니다. Task 종료.")
        conn.close()
        return "No active users to alert."

    # 3. 각 사용자에게 추천 및 알림 발송 (시뮬레이션)
    alert_count = 0
    now = datetime.now()
    
    for phone_number, lat, lon in users:
        try:
            # 3-1. 맛집 추천 로직 실행
            # app.py의 recommend_restaurant 함수는 DB 연결 및 쿼리를 내부에서 처리합니다.
            recommendation_message = recommend_restaurant(lat, lon, time_of_day)

            # 3-2. 알림톡 발송 시뮬레이션
            print(f"🔔 알림톡 발송 시뮬레이션 (수신자: {phone_number}, 위치: ({lat}, {lon})): {recommendation_message.splitlines()[0]}")

            # 3-3. 마지막 알림 시간 업데이트 (중요: 중복 알림 방지)
            cur = conn.cursor()
            cur.execute(
                "UPDATE users SET last_alert_sent = %s WHERE phone_number = %s",
                (now, phone_number)
            )
            conn.commit()
            cur.close()

            alert_count += 1
            
        except Exception as user_e:
            print(f"❌ 사용자 {phone_number} 알림 처리 중 오류 발생: {user_e}")
            conn.rollback() 
            continue
            
    conn.close()
    print(f"✅ Task 완료: 총 {alert_count} 명의 사용자에게 {time_of_day} 추천 알림이 발송되었습니다.")
    return f"Alerts sent to {alert_count} users successfully."

