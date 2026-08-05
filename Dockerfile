FROM python:3.11-slim

# 时区设为北京时间，保证定时任务按本地时间触发
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据与产物目录挂载出去，容器重建不丢历史
VOLUME ["/app/data", "/app/logs", "/app/reports"]

# 默认常驻定时模式；想跑单次改成 ["python", "main.py", "run"]
CMD ["python", "main.py", "schedule"]
