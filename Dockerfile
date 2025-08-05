# Python 3.9.6 베이스 이미지 사용 (slim 버전)
FROM python:3.9.6-slim

# 작업 디렉토리 설정
WORKDIR /app

# requirements.txt 복사 및 설치
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# 전체 프로젝트 파일 복사
COPY . .

# 앱 실행 명령어
CMD ["python", "distributor_bot.py"]