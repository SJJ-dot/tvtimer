# 小米电视管控脚本 镜像
# 零依赖（仅 Python 3 标准库），基于官方 python:3.13-slim
FROM python:3.13-slim

WORKDIR /app

# tzdata：让容器使用本地时区（禁用时段 / 跨天重置依赖本地时间，默认 UTC 会错位）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY tv_control.py tv_control.html ./

# 配置落盘目录（容器外挂卷持久化）；默认时区设为中国；日志不缓冲（首次启动随机密码立即可见）
ENV DATA_DIR=/app/data \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1

RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8080

# 网页/接口默认 0.0.0.0:8080，手机浏览器打开 http://<宿主机IP>:8080/
CMD ["python", "tv_control.py", "--host", "0.0.0.0", "--port", "8080"]
