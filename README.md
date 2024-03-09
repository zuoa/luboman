
### docker-compose.yml
```yaml
version: "3"

services:
  app:
    image: ghcr.io/zuoa/luboman:main
    container_name: luboman
    restart: "always"
    ports:
      - "5005:5005"
    volumes:
      - /data/luboman:/data
      - ~/.bypy:/root/.bypy

  webui:
    image: ghcr.io/zuoa/luboman-webui:main
    container_name: luboman-webui
    restart: "always"
    depends_on:
      - app
    links:
      - "app:luboman-service"
    ports:
      - "5001:5001"
```




#### 运行服务
```shell
docker run -P --name luboman -v /data/luboman:/data -v ~/.bypy:/root/.bypy -p 5001:5001 -d --restart always ghcr.io/zuoa/luboman:main`
```


