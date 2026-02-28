FROM python:3.10-alpine

# 设置时区为上海，防止时间错乱
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 在开发环境中，其实 COPY . . 这一步会被稍后的 volume 挂载覆盖
# 但保留它可以在你未来直接用于生产环境部署
COPY . .

EXPOSE 5000
CMD ["python", "app.py"]