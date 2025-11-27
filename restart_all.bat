@echo off

REM 🛑 1) 먼저 돌아가던 python 프로세스들 종료 (이미 없으면 에러 숨김)
taskkill /F /IM python.exe 2>nul

REM 📁 2) 프로젝트 폴더 경로 (신혁님 PC 기준)
set "PROJECT_PATH=C:\Users\김신혁\my_restaurant_project"

REM 🟢 3) Flask 서버 실행
start "" cmd /k "cd /d \"%PROJECT_PATH%\" && .\Scripts\activate && python app.py"

REM 🔵 4) Celery Worker 실행
start "" cmd /k "cd /d \"%PROJECT_PATH%\" && .\Scripts\activate && celery -A app.celery worker -l info -P solo"

REM 🟠 5) Celery Beat 실행
start "" cmd /k "cd /d \"%PROJECT_PATH%\" && .\Scripts\activate && celery -A app.celery beat -l info"


