# invest-workbench

美股 / 港股 / 韩股资讯与研报工作台。数据来自公开行情接口与 Toss 买卖动向；账户/持仓不在本仓库。

## 模块

| 面板 | 说明 |
|------|------|
| 大盘估值 | VIX / 指数分位 / Fear&Greed 等合成买点分 |
| 期权雷达 | 美股 IV 与 Put / Call 年化排序 |
| 交易决策 | 六层漏斗 + 本地交易日志（localStorage） |
| 个股研报 | 九段 Markdown 研报 |
| KOR 买卖动向 · 滤网 | **collector** 每 20s 拉 Toss → SQLite；**web** 经内网读库 |

## 进程角色

| ROLE | 职责 |
|------|------|
| `all` | 单进程：采集 + 全量 API（本地默认） |
| `collector` | 仅定时采集 + `/api/kr-*` 读库（挂 Volume，少部署） |
| `web` | 面板与其它 API；`/api/kr-*` 转发到 `KR_UPSTREAM` |

## 本地运行（单进程）

```bash
pip3 install -r requirements.txt
python3 cli.py serve
# http://127.0.0.1:8787/
```

## Docker（推荐双服务）

```bash
docker compose up -d --build
# web: http://127.0.0.1:8787/
# collector 内网 :8788，Volume 挂 ./data
```

## Railway（双服务）

同一镜像建两个 Service：

### 1) collector（稳定、少更新）
- Variables：`ROLE=collector`，`DATA_DIR=/app/data`
- **Volume Mount Path = `/app/data`**（只挂在 collector）
- 单实例；关闭 App Sleep
- 可不生成公网域名（仅 Private Networking）
- 探活：`/api/health` → `role: collector`，`persistent: true`

### 2) web（改 UI / 其它 API 时只 redeploy 这个）
- Variables：
  - `ROLE=web`
  - `KR_UPSTREAM=http://<collector服务名>.railway.internal:${{collector.PORT}}`  
    （在 Railway Variables 里用服务引用；或写死 collector 的 PORT）
- **不挂** Volume
- Generate Domain 给浏览器访问
- 探活：`/api/health` → `role: web`，带 `kr_upstream`

改面板代码时只部署 **web**，collector 继续采数，SQLite 不丢。

### 注意（易踩坑）
- **Variables 按服务分别设置**，不要两个服务共用同一个 `ROLE`
- Volume **只挂 collector**；web 不要挂 `/app/data`
- collector **不要** Generate Domain（仅内网）；公网只给 web
- `KR_UPSTREAM` 指错时，web `/api/health` 里 `collector_ok` 为 `false`（web 自身仍 `ok: true`，避免误杀进程）
- 鉴权：建议只给 **web** 配 Basic Auth；collector 内网可不配（若两边都配，密码须一致，因 web 会转发 Authorization）

## 研报 Prompt

见 `prompts/equity-research.md`。
