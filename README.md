# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转；置为 `resolved_return` 前必须有已签收的返还交接单，否则返回 409 及阻塞原因。
- `POST /api/claims/{id}/handover`、`GET /api/claims/{id}/handover`：审查员登记返还交接单（交付方式、经办人、接收人），查看单据列表与阻塞原因。
- `POST /api/handover/{id}/checkout|sign|reject`、`GET /api/handover/{id}`：工作人员出库、接收人签收、拒收（必填原因）与单据留痕查看。
- `GET /handover`：交接单页面入口（建单、出库、签收、查看阻塞原因）。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。

## 返还交接单规则

- 主张在 `negotiating` 才能建单，同一主张同时只能有一单进行中；建单时冻结藏品版本号与证据摘要（数量 + SHA-256 汇总）。
- 单据状态机：`pending → checked_out → received`，另可转为 `rejected`/`invalidated`；工作人员出库、接收人签收后，主张才能置为 `resolved_return`。
- 待交付期间藏品信息、流转事件或证据发生变化，进行中的单据自动失效并记录原因，需重核后重新建单。
- 拒收必须填写原因，主张随之回到 `negotiating` 并写入审查记录。
- 规则常量在 `app.py` 顶部"规则"区，流程留痕（`handover_events` + `audit_log`）在 `ProvenanceStore` 的"流程与留痕"区，页面入口独立为 `web/handover.html`，三者分开维护。
