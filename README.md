
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

#### 获取阿里云盘token
https://alist.nn.ci/zh/guide/drivers/aliyundrive.html#


const getCookie = (key) => {
  const { cookie } = document;
  return cookie.match(new RegExp(`${key}=(?<key>\\w+)`))?.groups?.key;
};


const getCookieText = (key) => {
  return key + '=' + getCookie(key) + ';'
};

console.log(getCookieText('__ac_nonce') + getCookieText('__ac_signature')+ getCookieText('sessionid'));


#### 获取发电
https://member.bilibili.com/x/vupre/web/archive/pre?lang=cn&t=1757769600025

---

## 项目结构

- `luboman/` —— 后端（Python aiohttp，端口 5005）
- `webui/` —— 前端（UmiJS Max + React + Ant Design，构建产物由 nginx 托管于端口 5001）

两个镜像（`ghcr.io/zuoa/luboman` 与 `ghcr.io/zuoa/luboman-webui`）均由本仓库的 `.github/workflows/docker-image-push.yml` 统一构建发布（push 到 `main` 触发）。

## 前端开发（本地联调）

> 前端基于 `@umijs/max` 4.x，其打包/dev 工具链要求 **Node 18**（见 `webui/.nvmrc`、`webui/Dockerfile` 的 `node:18`）。Node 20+ 会因 `http-deceiver` 调用已移除的 `process.binding('http_parser')` 而在 `npm install`/`npm run build` 时报错。建议用 nvm：`nvm use 18`。

前端与后端不同端口，开发时前端 dev server 通过代理把 `/api` 转发到本地后端（见 `webui/.umirc.ts` 的 `proxy`）：

```shell
# 1) 启动后端（端口 5005）
cd luboman
python async_main.py

# 2) 另开终端启动前端（默认端口 8000，代理到 http://127.0.0.1:5005）
cd webui
npm install
npm run dev
```

浏览器打开 http://localhost:8000 ，前端发起的 `/api/v1/...` 请求会被剥掉 `/api` 前缀后代理到后端。

- 指向远程后端：`LUBOMAN_BACKEND=http://<host>:5005 npm run dev`
- 构建生产产物：`cd webui && npm run build`（输出 `webui/dist/`，前端镜像据此构建）