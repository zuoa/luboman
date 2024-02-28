FROM python:3.9-slim as luboman
ENV TZ=Asia/Shanghai
EXPOSE 5001/tcp
VOLUME /data

RUN \
  set -eux; \
  mkdir /data/bin -p && \
  apt-get update; \
  apt-get install -y --no-install-recommends ffmpeg git g++ curl unzip; \
  curl -L https://github.com/tickstep/aliyunpan/releases/download/v0.2.9/aliyunpan-v0.2.9-linux-amd64.zip -o bin/aliyunpan.zip && \
  unzip bin/aliyunpan.zip -d bin/ && \
  git clone --depth 1 https://github.com/zuoa/luboman.git && \
  cd luboman && \
  pip3 install -r requirements.txt && \
  # Clean up \
  apt-mark auto '.*' > /dev/null; \
  apt-mark manual ffmpeg; \
  find /usr/local -type f -executable -exec ldd '{}' ';' \
     | awk '/=>/ { print $(NF-1) }' \
     | sort -u \
     | xargs -r dpkg-query --search \
     | cut -d: -f1 \
     | sort -u \
     | xargs -r apt-mark manual \
     ; \
  apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false; \
  rm -rf \
    /tmp/* \
    /usr/share/doc/* \
    /var/cache/* \
    /var/lib/apt/lists/* \
    /var/tmp/* && \
  #  apk del --purge .build-deps && \
#  rm -rf /var/cache/apk/* && \
  rm -rf /var/log/*

#COPY --from=webui /biliup/biliup/web/public/ /biliup/biliup/web/public/
WORKDIR /data

ENTRYPOINT ["luboman"]