FROM python:3.11-slim as tools
RUN \
  set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends vim git g++ curl unzip xz-utils; \
  curl -L https://github.com/tickstep/aliyunpan/releases/download/v0.4.0/aliyunpan-v0.4.0-linux-amd64.zip -o /tmp/aliyunpan.zip && \
  unzip /tmp/aliyunpan.zip -d /opt &&  mv /opt/aliyunpan-v0.4.0-linux-amd64 /opt/aliyunpan


FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
# WITH_DOUYIN=false 构建精简镜像（不含 Chrome 及 X 依赖，小约 400MB）：
# 抖音投稿功能不可用，其余功能不受影响；事后可容器内 patchright install --with-deps chromium 补齐。
ARG WITH_DOUYIN=true
RUN  set -eux; \
     apt-get update; \
     apt-get install -y --no-install-recommends ffmpeg; \
     pip3 install --no-cache-dir -r requirements.txt; \
     if [ "$WITH_DOUYIN" = "true" ]; then \
       apt-get install -y --no-install-recommends xvfb xauth; \
       patchright install --with-deps chromium; \
     fi
COPY luboman ./luboman
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
RUN mkdir bin && ls -a

COPY --from=tools --chmod=777 /opt/aliyunpan/aliyunpan /usr/bin/aliyunpan
#COPY --from=tools --chmod=777 /usr/bin/ffmpeg /usr/bin/ffmpeg

RUN pwd && ls -a

WORKDIR /app/luboman
ENV TZ=Asia/Shanghai
ENV PYTHONPATH="/app:$PYTHONPATH"
EXPOSE 5005/tcp
VOLUME /data
VOLUME /root/.bypy
# 经 entrypoint.sh 包 xvfb-run（完整镜像）或直跑（精简镜像），见 entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
