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
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256（会推进藏品版本）。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating` 流转，可驳回为 `rejected`；`resolved_return` 不能手工设置，只能由交接单签收触发。
- `POST /api/claims/{id}/handovers`、`GET /api/claims/{id}/handovers`：为协商阶段的主张创建/查看返还交接单。
- `POST /api/handovers/{id}/outbound`：登记的经办人（工作人员）出库。
- `POST /api/handovers/{id}/sign`：接收人签收，主张随即置为 `resolved_return`。
- `POST /api/handovers/{id}/reject`：接收人填写原因拒收，主张回到 `negotiating`。
- `GET /api/handovers/{id}`：交接单详情、留痕时间线与当前阻塞原因。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

## 返还交接单

审查员不能再把主张直接记成已返还。协商阶段创建交接单时登记交付方式
（现场交接/馆方运送/第三方物流）、经办人、接收人，并冻结当时的藏品版本
和证据摘要（文件名 + SHA-256 清单的整体摘要值）。随后：

1. 工作人员出库（必须是单据登记的经办人）；
2. 接收人签收——接收人关联了系统主张人账号时只能本人签收/拒收，
   馆外接收人由工作人员代为登记；签收后主张才置为 `resolved_return`；
3. 待交付期间（待出库/已出库）藏品信息、流转事件或证据发生变化，
   单据自动失效，主张退回 `under_review` 重核，重核通过后才能重新建单；
4. 拒收必须填写原因，主张回到 `negotiating`。

每次建单、出库、签收、拒收、失效都写入 `handover_events` 留痕，并同步
推进藏品版本快照与审计记录。

## 代码分层

- `rules.py`：业务规则——主张/交接单状态机、角色权限、交付方式、
  冻结依据的阻塞判定（纯函数，可独立单测）。
- `app.py`：持久化、留痕（`handover_events`、`claim_reviews`、
  `object_versions`、`audit_log`）与 HTTP 入口。
- `web/index.html`：页面入口——建单、出库、签收、拒收、查看阻塞原因，
  不含业务规则。

公众看不到持有人和内部事件；主张人只能查看自己的主张及其交接单；阶段不能
跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
