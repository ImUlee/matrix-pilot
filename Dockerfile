# 使用 Alpine 极简版 Linux 镜像
FROM python:3.10-alpine

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装 (使用阿里云加速并清除缓存)
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

# 复制项目代码
COPY . .

# 暴露端口并启动
EXPOSE 5000
CMD ["python", "app.py"]