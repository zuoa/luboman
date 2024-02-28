FROM python:3.9-slim
ENV TZ=Asia/Shanghai
EXPOSE 5001/tcp
WORKDIR /app
VOLUME /data
COPY requirements.txt .
RUN \
  set -eux; \
  mkdir /data -p && \
   apt-get update; \
  apt-get install -y --no-install-recommends ffmpeg git g++ curl unzip; \
    curl -L https://github.com/tickstep/aliyunpan/releases/download/v0.2.9/aliyunpan-v0.2.9-linux-amd64.zip -o bin/aliyunpan.zip && \
    unzip bin/aliyunpan.zip -d bin/ && \
    pip3 install --no-cache-dir -r requirements.txt && \
    ls -a
COPY . .
RUN pwd && ls -a


ENTRYPOINT ["python", "luboman/main.py"]