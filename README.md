# invest-workbench

美股 / 港股 / 韩股资讯与研报工作台。数据来自公开行情接口与 Toss 买卖动向；账户/持仓不在本仓库。

## 模块

| 面板 | 说明 |
|------|------|
| 大盘估值 | VIX / 指数分位 / Fear&Greed 等合成买点分 |
| 期权雷达 | 美股 IV 与 Put / Call 年化排序 |
| 交易决策 | 六层漏斗 + 本地交易日志（localStorage） |
| 个股研报 | 九段 Markdown 研报 |
| KOR 买卖动向 · 滤网 | 服务端每 20s 拉 Toss → SQLite；`/api/kr-dashboard` 只读库 |

## 本地运行

```bash
pip3 install -r requirements.txt
python3 cli.py serve
# http://127.0.0.1:8787/  （端口可读环境变量 PORT）
```

常用命令：

```bash
python3 cli.py quote 00700
python3 cli.py research AAPL
python3 cli.py market --years 5
python3 cli.py options AAPL,MSFT,NVDA
python3 cli.py kr-dashboard           # 读 SQLite
python3 cli.py kr-dashboard --collect # 手动拉一次 Toss
```

需能访问外网。韩股休市日不采集（见 `src/kr_calendar.py`）。

## Docker

```bash
docker compose up -d --build
```

挂载 `./data`（SQLite）与 `./reports`。

## Railway

- Dockerfile + `railway.toml`；探活 `/api/health`；监听 `PORT`
- **Volume** 挂 `/app/data`；**单实例**；不要开 App Sleep
- 建议设置 `BASIC_AUTH_USER` / `BASIC_AUTH_PASSWORD`（未设则公开）
- 内存建议 0.5–1 GB

## 研报 Prompt

见 `prompts/equity-research.md`。
