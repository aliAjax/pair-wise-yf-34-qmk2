# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

发现相邻交通冲突时，审核员不必直接拒绝：可以创建**冲突协商单**，把主计划和相邻相关计划及其当前版本一并收入。各家运营方修改自己的计划后在单内确认接受；只有当所有确认版本仍与计划当前版本一致、且重新检查没有相邻交通冲突时，审核员才能放行。任一计划之后改动，其确认立即失效，页面会标出需重做的运营方。拒绝或撤回必须填写原因；计划取消、过期或被拒绝会自动退出协商，主计划出现这些情况时整单结束。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 模块划分

- `common.py`：错误类型、时间与空域几何等共享工具。
- `app.py`：HTTP 接口入口与计划审批主流程（存储 `Repository`、服务 `DroneAirspaceService`、路由 `Handler`）。
- `negotiation_store.py`：协商单存储层，表结构与 SQL，不做业务判定。
- `negotiation_rules.py`：协商单判定逻辑（参与方状态、放行条件、需重做名单），纯函数。
- `negotiation_service.py`：协商单业务编排（权限、事务、通知、自动退出钩子）。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `POST /api/plans/{id}/negotiations`：对检查出相邻冲突的已提交计划创建协商单（同一计划同时只存在一张未结束单，重复创建幂等返回原单）。
- `GET /api/negotiations`、`GET /api/negotiations/{id}`：按角色查看协商单，含每方确认版本与当前版本、待确认与需重做运营方、放行条件。
- `POST /api/negotiations/{id}/confirm`：运营方确认自己计划的当前版本（`plan_id` 必填，重复确认幂等）。
- `POST /api/negotiations/{id}/decline`：运营方拒绝协商（`plan_id` + `reason`），整单结束。
- `POST /api/negotiations/{id}/withdraw`：发起方、主计划运营方或审核角色撤回（`reason` 必填）。
- `POST /api/negotiations/{id}/release`：审核员放行（`reason` + `offline_id`）；前置条件为全部确认版本与计划当前版本一致且重新检查无相邻交通冲突，放行即按常规规则批准主计划。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理；到期处理同时让过期计划退出协商。
- `GET /api/state`：按角色返回计划、限制、协商单（待确认 / 可批准 / 已结束三栏）和公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。协商单不做多方实时协同编辑与消息推送，确认失效以版本比对在读取时即时判定。
