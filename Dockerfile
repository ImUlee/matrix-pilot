# 1. 基础镜像
FROM python:3.11-slim

# 2. 设置工作目录
WORKDIR /app

# 3. 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV FLASK_ENV=production

# 4. 安装基础依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# 5. 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# 6. 复制项目代码
COPY . .

# 7. 暴露端口
EXPOSE 5000

# 8. 启动命令（使用 Gunicorn 提高并发稳定性）
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app", "--workers", "2", "--threads", "4"]