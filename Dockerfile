FROM python:3.9-slim as tools
RUN \
  set -eux; \
  apt-get update; \
  apt-get install -y --no-install-recommends git g++ curl unzip xz-utils; \
  curl -L https://github.com/tickstep/aliyunpan/releases/download/v0.2.9/aliyunpan-v0.2.9-linux-amd64.zip -o /tmp/aliyunpan.zip && \
  unzip /tmp/aliyunpan.zip -d /opt &&  mv /opt/aliyunpan-v0.2.9-linux-amd64 /opt/aliyunpan && \
  curl -L https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o /tmp/ffmpeg.tar.xz && \
  tar -xvf /tmp/ffmpeg.tar.xz -C /opt && mv /opt/ffmpeg-6.1-amd64-static /opt/ffmpeg


FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN  set -eux; \
     apt-get update; \
     apt-get install -y --no-install-recommends ffmpeg;  \
     pip3 install --no-cache-dir -r requirements.txt
COPY luboman ./luboman
RUN mkdir bin && ls -a

COPY --from=tools --chmod=777 /opt/aliyunpan bin/aliyunpan
COPY --from=tools --chmod=777 /opt/ffmpeg bin/ffmpeg

RUN pwd && ls -a

WORKDIR /app/luboman
ENV TZ=Asia/Shanghai
ENV PYTHONPATH="/app:$PYTHONPATH"
EXPOSE 5001/tcp
VOLUME /data
ENTRYPOINT ["python", "main.py"]