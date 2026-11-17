# 临汀服务时窗交付簿

数据服务请求时间→业务时钟核算→站内定时交付；含时区与跨日，非工单客服功能

## 业务场景与工作流
模拟客户：临汀研究协作组，12位业务与分析人员。每月研究明细先从`service-calendar-202611.csv`录入底稿，经口径冻结、Python核算、负责人复核后向业务阅读者交付站内CSV。原有流程依靠人工表格抄算，痛点为口径版本难追溯、重复计算易错、复核与交付脱节。

模块：业务总览、业务底稿、新建口径版本、异步核算与复核、定时交付、站内收件箱、增量透视、审计。底稿可新增/修改/删除；口径仅新增版本；报告提交时冻结来源与版本并保存SHA256。定时交付引用已复核报告快照，worker到期写入数据库，业务阅读者下载。

## 算法口径
工作日/假日/午休交集分钟与SLA超时分层透视。周五到周一含假日和午休手算；负时段拒绝；边界分钟。

业务时钟 · 午休与假日显式扣除。输入：业务底稿与口径JSON；输出：summary、透视rows、method、warnings。算法实现在`backend/src/services/calculation.py`，输入校验在`validation.py`。固定seed=42，240条合成数据；未对接真实业务系统。修改源数据和新建因子/口径不会改变已提交报告。

## 一天的验收剧本
1. 09:00 admin查看240条seed底稿与具名业务期间。
2. 09:10 analyst添加`service-calendar-202611.csv`的一条具名业务记录。
3. 09:20 admin在口径版本页登记新版本；旧版本保留。
4. 09:30 analyst命名“十一月核算”并选择期间/版本排队。
5. 09:31 Redis worker真实执行Python计算，展示汇总/透视与口径边界。
6. 09:40 admin核对数值后填写至少4字复核说明。
7. 09:45 admin建立60秒周期计划，worker到期生成站内持久化交付。
8. 09:46 viewer在站内收件箱下载交付CSV；审计页查看操作者与时间。
9. 09:50 analyst修改一条已计入底稿的月份并删除另一条；增量透视页两次刷新即可看到对应聚合单元先撤旧再计新，页面水位与接收水位追平后显示“已追平”。
10. 09:55 admin登记含新假日的口径版本；透视页显示“新口径建账中”与基线进度，期间新增底稿照常成功；切换后生效口径与旧快照（保留原值）同列展示。

## 快速启动
```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```
前端 http://localhost:18252 ，后端 http://localhost:19252/docs ，数据库宿主端口17752。中文父目录可用。恰好5服务frontend/backend/worker/db/redis；均有真实healthcheck。停止并清空演示数据：`docker compose down -v --remove-orphans`。

|角色|邮箱|密码|权限|
|---|---|---|---|
|admin|admin@demo.local|Demo2026!|全部底稿、口径版本、核算、复核、调度|
|analyst|analyst@demo.local|Demo2026!|底稿增改删、核算、导出；无复核/调度/口径写权限|
|viewer|viewer@demo.local|Demo2026!|只读；仅可下载已复核报告与站内交付|

## 工程结构与开发
Python3.11/FastAPI/SQLAlchemy/Redis，PostgreSQL16；前端原生HTML/CSS/JS ES modules，Nginx代理`/api/`剥前缀至后端。`frontend/src`按api/stores/pages/components/hooks/utils/router/constants/types拆分；`backend/src`按models/schemas/services/controllers/routes/middlewares/config/utils/constants拆分；`backend/scripts/seed_demo.py`自然键幂等且不覆盖用户修改。SQLAlchemy启动创建初始结构，迁移说明在backend/migrations。

后端开发：在backend安装requirements，设置DATABASE_URL/REDIS_URL/JWT_SECRET，`uvicorn src.main:app --reload`；前端可用Nginx或带同等代理的静态开发服务器。前后端全部业务计算在Python。

环境变量：COMPOSE_PROJECT_NAME、FRONTEND_PORT、BACKEND_PORT、DB_PORT、JWT_SECRET（部署替换）、DATABASE_URL、REDIS_URL、SEED_DEMO、PIVOT_INLINE。`.env`不入Git。数据库/Redis/共享产物为命名卷。共享角色枚举admin/analyst/viewer与状态queued/running/completed/failed分别定义于backend/src/constants/enums.py和frontend/src/constants/enums.js。两个中间件：统一可恢复校验错误、每IP每分钟360请求限流。

## 固定口径增量透视
页面「增量透视」按当前生效口径展示一份**固定维度**（结算月份×分析小组×优先级）的透视。底稿的新增/修改/删除在同一事务写入 `source_changes` 变更日志，worker 逐条应用：先撤回该样本的旧贡献，再计入新贡献；期间或小组调动因此不会把旧月份的计数留在原处。单条更正只读写该样本的一条贡献与其对应聚合单元，不扫描全库（十万条底稿也只付出 O(1) 的单条代价）。

- **接收水位与页面水位**：控制行在锁内分配事件序号，序号等于提交顺序且无空洞，因此 `receive_seq` 就是“已可见的接收水位”。页面读取结果、水位、建账进度在同一个数据库事务快照内完成，始终是同一水位的完整结果。
- **首次建账**：新口径建立 `building` 快照，按 `source.id` 分页装载基线；期间的新增/修改/删除照常提交，不阻塞写入。基线完成后进入 `catching` 追赶变更，追平 `receive_seq` 后在同一事务里撤下旧 active 并原子切换。切换前页面继续读取旧口径的完整结果。
- **重建失败**：建账快照保留错误与失败次数；连续失败达到上限标记 `failed`，旧 active 结果继续服务，页面显示失败原因。
- **幂等与顺序**：每条贡献记录最后应用的事件序号，重复投递与晚到的旧事件被忽略（不重复累加、不倒退）；基线装载与事件应用都从持久化游标继续，进程中断后不重不漏。
- **多进程**：worker 与后端内联线程通过 `pivot_lease` 租约互斥，持有者中断后租约过期自动接管；任务队列的认领是原子状态更新，重复投递只有一个执行者。
- **可解释性**：页面显示接收水位、页面水位、滞后变更数、处理进程心跳与未计入问题清单，并直接写明滞后原因（处理进程无心跳 / 正在追建账 / 有底稿口径不完整）。
- **冻结语义不变**：`sla_runs` 的报告快照、已复核报告、`deliveries` 中的定时交付 payload 都不读取透视表，重建与切换不会改动它们的值。

接口：`GET /pivot`（可选 `policy_id`，返回 active 结果与该水位下的进度）与 `POST /pivot/rebuild`（负责人按同口径重新建账，返回快照状态）。相关实现：`backend/src/services/pivot.py`、`backend/src/models/pivot.py`、`backend/src/routes/pivot.py`、`frontend/src/pages/pivot.js`。

## 已知边界
定时分发为站内收件箱；worker轮询执行，增量透视由 worker 与后端内联线程经数据库租约互斥推进（`PIVOT_INLINE=false` 可关闭内联线程）。调度发送已复核固定报告快照，不自动重算新数据。时间显示Asia/Shanghai。报告导出适合小型数据；底稿全量加载240行演示数据。Google Fonts不可用时回退serif。后端API以专属字段对象收发，页面通过业务输入控件编辑，不要求用户写JSON。新增来源期间自动可选；专属字段和算法严格校验。JWT演示有效期2小时。

MIT License。

## 实际实体映射
来源记录（Source，专属业务字段校验）、口径版本（Policy）、核算报告（Run）、交付计划（Schedule）、交付（Delivery）均经数据库→模型→请求schema→service→route/controller→frontend API→store→page。Period为已有来源期间字典，在任务期间选择器使用；不单独作为复杂度计数。底稿JSON为小型多字段单记录存储，不虚构未落库的子实体。

实际表：期间字典 service_periods；专属来源 service_cases；冻结口径 business_calendars；异步报告 sla_runs；增量透视 pivot_control / source_changes / pivot_snapshots / pivot_cells / pivot_contributions / pivot_issues / pivot_lease / pivot_workers。
