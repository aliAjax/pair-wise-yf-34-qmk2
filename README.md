# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

多个运营方计划挤入同一片空域时使用**冲突协商单**：检查到相邻计划后，由审核员或主计划运营方开单，把主计划、相关计划及其当前版本快照收进单子。各家运营方修改自己的计划后按当前版本确认；任一计划改动，该方确认立即失效，首页标出需重做的运营方。只有所有确认版本与计划当前版本一致、且重新检查不再有相邻交通冲突时，审核员才能整单放行（放行即批准单内全部计划）。拒绝、撤回必须填写原因；成员计划取消或单子过期自动退出。首页按角色把协商单分到待确认、可批准、已结束三栏。

判定（`NegotiationPolicy`）、存储（`Repository` 的协商单方法）和接口入口（`Handler`）分开维护，服务层 `DroneAirspaceService` 负责事务编排。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理（到期也会自动结束过期协商单）。
- `GET /api/state`：按角色返回计划、限制、协商单看板和公开信息。

## 冲突协商单接口

- `POST /api/plans/{id}/negotiation`：对已提交且检查到相邻计划的主计划开单，收进主计划与相关计划的当前版本；请求体可带 `reason`。
- `GET /api/negotiations`：按角色返回三栏看板：`to_confirm`（运营方有己方计划待确认 / 审核员看到未齐确认的单）、`approvable`（运营方己方已确认待审核 / 审核员看到全部确认且无相邻冲突）、`closed`（rejected/withdrawn/canceled/expired/released）。
- `GET /api/negotiations/{id}`：协商单详情，含各计划的基准版本、当前版本、确认版本、条目状态（`confirmed`/`stale`/`need_resubmit`/`pending`）、`pending_operators`、剩余相邻冲突和事件记录。
- `POST /api/negotiations/{id}/confirm`：运营方确认，请求体 `{plan_id, confirmed_revision}`；确认版本必须等于计划当前版本，且计划处于已提交/已批准。计划经 `change` 改版后，原确认自动变为 `stale`，需重新修改并确认。
- `POST /api/negotiations/{id}/reject`：审核员/指挥官拒绝，`reason` 必填。
- `POST /api/negotiations/{id}/withdraw`：主计划运营方撤回，`reason` 必填。
- `POST /api/negotiations/{id}/release`：审核员/指挥官整单放行，`offline_id`、`reason` 必填；放行时重新检查全部计划，存在相邻交通冲突一律拒绝（`adjacent_traffic_remains`，紧急授权也不能覆盖），硬约束冲突拒绝（`hard_constraint_violation`），其他空域阻断冲突仅允许指挥官凭 `override_reason` 覆盖。放行会为单内未批准计划各写一条幂等批准记录（离线编号派生自 `offline_id`）。
- 成员计划取消、过期，或主计划起飞时间已过，协商单在下次读写时惰性自动退出（`canceled`/`expired`），`POST /api/expire` 会立即清扫。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
