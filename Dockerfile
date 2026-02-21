# 使用轻量级 Python 镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 防止 Python 产生 .pyc 文件，并让日志直接输出
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 安装运行环境
RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# 拷贝项目文件
COPY . .

# 暴露 5000 端口
EXPOSE 5000

# 使用 Gunicorn 运行生产环境（比 Flask 自带服务器稳得多）
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]