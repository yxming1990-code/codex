# Polymarket 自动交易机器人（可视化模板）

此仓库提供一个带有可视化界面的自动交易机器人模板，包含：

- FastAPI 后端：控制连接、启动/停止交易、推送事件。
- 前端 Dashboard：订单簿展示、交易日志、策略选择。
- Polymarket 客户端占位实现：可替换为官方/社区 websocket 封装。

## 快速开始

```bash
pip install -r requirements.txt
uvicorn backend.app.main:app --reload
```

打开浏览器访问 `http://localhost:8000`。

## 集成外部数据订阅

在 `backend/app/services/polymarket_client.py` 中替换 `_mock_stream`，
并调用以下社区项目来订阅订单簿/成交流：

- https://github.com/discountry/polymarket-orderbook-watcher
- https://github.com/discountry/polymarket-websocket-client

## 说明

当前版本为演示模板（交易接口为模拟下单），请在接入真实交易前
增加认证、风控、持仓管理等逻辑。
