# 安宁礼仪与公墓运营服务

这是一个供殡仪馆、公墓和合作医疗机构使用的 Python 后端服务，统一管理逝者业务档案、遗体保管交接、送别厅与火化设备预约、服务订单、墓位权属、账单收款和审计时间线。系统把容易产生争议的交接、排程与收费动作保存在本地 SQLite 中，支持在单个 Linux 应用容器内离线运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

依次执行 python -m venv .venv、source .venv/bin/activate、python -m pip install -e ".[dev]"。可通过 PEACEFUL_CARE_DATABASE_PATH 指定数据库文件，默认写入项目的 data 目录。

## 初始化与启动

先执行 python -m app.cli init-db 和 python -m app.cli check-db，再用 uvicorn app.main:app --host 0.0.0.0 --port 8432 启动。健康检查为 GET /api/system/health。殡葬业务接口位于 /api/mortuary，涵盖档案、交接、资源、预约、服务订单、墓位权属、账单和时间线。

## 跨资源整组编排

大型告别仪式同时占用送别厅、接运车辆、礼仪人员和火化时段，单项逐个预约会留下部分成功。`/api/mortuary/ceremony-groups` 提供整组编排：

- `POST /ceremony-groups`：在单个即时事务内检查全部资源，全部可用才一次性写入带期限的持有占用（`held`），任一资源冲突整体回滚并返回 `blocking_resources`，不保留任何部分占用；幂等键重复且内容一致返回原记录，内容变化返回冲突。
- `POST /ceremony-groups/{id}/confirm`：由 approver（或系统管理员）在确认期限内把整组置为 `confirmed`；`POST /ceremony-groups/{id}/release` 整体释放并在同一事务内幂等推进候补。
- `POST /ceremony-groups/waitlist`：直接登记候补；`GET /waitlist` 返回综合排序名次。排序依据为人工越序号（越靠前）、经审核紧急等级（高者优先）、遗体保存期限（早者优先）、申请时间（先者优先）。
- `POST /waitlist/promote`：容量释放后按名次幂等推进，重复幂等键回放同一结果；`POST /ceremony-groups/expire-holds` 清扫超过确认期限的持有并级联推进候补。
- `POST /ceremony-groups/{id}/waitlist/override`：仅 approver 可越序，必须填写理由，动作与前后队列快照写入 `ceremony_interventions` 审计；`POST /ceremony-groups/{id}/urgency-review` 用于审核紧急等级。
- 授权人员通过 `POST /orchestration/actors/grant` 维护（角色 family_service/planner/approver）。`GET /ceremony-groups/{id}` 返回整场仪式的各资源明细、阻塞资源、确认状态、候补名次、历次审计干预与时间线。

持有占用与候补均落库，进程重启后未过期数据原样恢复；清扫只处理真正到期的持有，不影响其他记录。

## 测试与编译检查

测试命令：python -m pytest

编译命令：python -m compileall -q app tests

API 与 CLI 冒烟命令：python -m app.cli smoke、python -m app.cli mortuary-demo

## 目录结构

- app/mortuary：档案、保管交接、资源排程、权属和账单领域
- app/api：登录、角色、审计及系统管理接口
- app/core：时钟、安全、异常、隐私与分页能力
- app/repositories：通用身份和审计数据访问
- app/services：会话、权限、后台任务及维护服务
- tests：领域、接口、异常路径和身份回归测试

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时事务。业务档案采用外部编号去重，保管交接与预约保留幂等键，服务订单开票后不可再次开票，支付流水不能重复分配。整组仪式占用在单个事务内判定全部资源（含 `held` 持有态）后统一提交或回滚，候补推进与过期清扫均幂等且持久化。关键状态变化同时写入领域时间线；会话令牌仅保存摘要，审计记录不会保存明文密码或令牌。
