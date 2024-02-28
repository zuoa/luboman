FROM python:3.9-slim
ENV TZ=Asia/Shanghai
EXPOSE 5001/tcp
WORKDIR /app
VOLUME /app
COPY requirements.txt .
RUN ls -a && pip3 install --no-cache-dir -r requirements.txt
COPY . .


ENTRYPOINT ["python", "luboman/luboman.py"]