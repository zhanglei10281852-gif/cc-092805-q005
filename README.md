# 安宁礼仪与公墓运营服务

这是一个供殡仪馆、公墓和合作医疗机构使用的 Python 后端服务，统一管理逝者业务档案、遗体保管交接、送别厅与火化设备预约、服务订单、墓位权属、账单收款和审计时间线。系统把容易产生争议的交接、排程与收费动作保存在本地 SQLite 中，支持在单个 Linux 应用容器内离线运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

依次执行 python -m venv .venv、source .venv/bin/activate、python -m pip install -e ".[dev]"。可通过 PEACEFUL_CARE_DATABASE_PATH 指定数据库文件，默认写入项目的 data 目录。

## 初始化与启动

先执行 python -m app.cli init-db 和 python -m app.cli check-db，再用 uvicorn app.main:app --host 0.0.0.0 --port 8432 启动。健康检查为 GET /api/system/health。殡葬业务接口位于 /api/mortuary，涵盖档案、交接、资源、预约、服务订单、墓位权属、账单和时间线；大型告别仪式的跨资源整组占用与候补编排在 /api/mortuary/ceremonies 与 /api/mortuary/waitlist。

## 大型告别仪式跨资源编排

礼厅、接运车辆、礼仪人员和火化时段通过整组占用一并预约：

- POST /api/mortuary/ceremonies/preflight：预检各资源容量，返回阻塞资源明细。
- POST /api/mortuary/ceremonies/holds：在单个 IMMEDIATE 事务内为整组资源创建有期限（hold_minutes）的 held 占用。任一资源冲突整笔回滚，绝不留下部分成功；请求按 (case_id, idempotency_key) 幂等。
- POST /api/mortuary/ceremonies/{id}/confirm：由具备 mortuary.ceremony.confirm 权限的人员整体确认；POST /api/mortuary/ceremonies/{id}/release 整体释放并触发候补推进。
- 持有超时由系统自动整体释放（system:hold-expiry），释放后在同一事务内幂等推进候补。
- POST /api/mortuary/waitlist：登记候补，综合排序为“已审核紧急等级降序 → 遗体保存期限升序 → 申请时间升序”，自行申报的紧急等级须经 mortuary.ceremony.review 审核后才生效；超过遗体保存期限的候补自动失效。
- POST /api/mortuary/waitlist/{id}/urgency-review 审核紧急等级；POST /api/mortuary/waitlist/{id}/override 人工越序，必须填写理由，动作写入 ceremony_waitlist_overrides 与审计事件。
- POST /api/mortuary/waitlist/advance 手动触发清理与推进；重复推进不会重复递补。
- GET /api/mortuary/cases/{case_id}/ceremony-overview 供家属服务人员查询整场仪式的阻塞资源、确认状态、候补名次与历次调整。

编排状态全部落 SQLite（WAL），没有内存队列，重启后未过期的整组占用与候补位置不丢失。


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

SQLite 连接启用外键、WAL、忙等待和即时事务。业务档案采用外部编号去重，保管交接与预约保留幂等键，服务订单开票后不可再次开票，支付流水不能重复分配。关键状态变化同时写入领域时间线；会话令牌仅保存摘要，审计记录不会保存明文密码或令牌。
