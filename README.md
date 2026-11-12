# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务 → 选种子 `/mock/catalog/alpha`、`beta` 或 `gamma` → 详情页「运行采集」→ 查看落库（alpha 含重复标题，会演示去重跳过）→ 详情页「查看对账与补差」。

## 运行后对账与补差

每次采集运行结束后，服务端会**自动做一次对账**。对账只比较、只标记，**不修改正式采集结果**（`crawl_records`）；差异由服务端计算，页面只负责展示和标记。条目的身份是 mock 条目的编码（`sku`），目录侧取目录当前条目，库侧从已落库记录的 `payload_json` 解析编码。

### 三类差异（页面分区展示，可按类型筛选）

| 类型 | 含义 | 能否补差 |
|------|------|----------|
| 目录有、库没有（缺失 `dir_only`） | 该编码目录当前看得到，但库里没有正式记录 | 仅**未保留**的此类编码可补差 |
| 库有、目录没有（多出 `db_only`） | 库里有记录，但目录当前已看不到该编码 | 不补差 |
| 两边都有、标题不同（`title_mismatch`） | 编码两边都在，但标题不一致 | **不会**被当成缺失补抓 |

### 人工保留

- 在对账页每条差异右侧的「标记人工保留」框中**填写保留原因（必填）**后提交；空原因会被拒绝。
- 已保留条目在**之后每次对账仍会出现**（保留状态和原因会继承到新批次），但不能用于生成补差。
- 列表用「已保留 / 未保留」标签直观看出保留状态，并展示保留原因与时间。

### 补差任务（catchup）

- 只有**尚未保留、且属于「目录有、库没有」**的编码才允许生成一条补差任务；标题不同、库侧多出、已保留项都不会进入补差。
- 补差**只抓名单内的指定编码**，不会顺手把目录里的其它编码抓进来。
- 同一批差异**最多一条**补差：未处理完不能再生成第二条；已有补差运行中再次触发会被拒绝。补差结束后自动再对一次账。

### 浏览器操作步骤

1. 登录 `admin / 123456`，进入任务详情页，点击「查看对账与补差 →」。
2. 顶部用类型筛选条在「全部 / 缺失 / 多出 / 标题不同」之间切换；也可用「对账批次」下拉查看历史批次，点「按目录当前状态重新对账」可随时重算（不动正式结果）。
3. **标记保留**：在某条差异行的输入框填写原因 → 点「标记人工保留」，该行变为「已保留」。
4. **生成补差**：在「补差任务」卡片核对允许补差的编码清单 → 点「生成补差任务（仅含以上编码）」，确认后执行，完成自动跳到最新对账批次。

## 自动化测试

```powershell
pytest
```

`tests/test_reconciliation.py` 覆盖七组核心场景：三类差异都会出现且分开展示、保留之后不能生成补差、未保留的缺失可以生成补差、补差只含指定编码、同一批差异不能生成两次、标题不同不会进入补差、补差运行尚未结束时拒绝再次触发（另含自动对账、登录鉴权等附加用例）。

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
