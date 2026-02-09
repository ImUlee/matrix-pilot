# 使用超轻量级 Nginx 镜像
FROM nginx:alpine

# 设置工作目录
WORKDIR /usr/share/nginx/html

# 清除默认静态文件
RUN rm -rf ./*

# 复制我们的项目文件到镜像中
COPY index.html .
COPY manifest.json .
COPY sw.js .
# 如果你有图标文件夹，取消下面这行的注释
# COPY icons/ ./icons/

# 暴露 80 端口
EXPOSE 80

# 启动 Nginx
CMD ["nginx", "-g", "daemon off;"]