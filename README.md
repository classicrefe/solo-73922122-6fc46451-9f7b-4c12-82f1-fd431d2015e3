# 街道更名佐证核查 API

一条街道更名记载即使列了几个出处，这些出处也可能都转引自同一份原件。本服务按
**（旧名、新名、生效日）** 归档更名事项，登记**原件档号**与**转引条目**及其引用关系，
并可从任一条目发起**佐证核查**：

> 若存在两条引用链，**除起点外互不重合**，且**分别通向不同档号的在册原件**，
> 则结论为「佐证成立」并保存两条链；否则记为「佐证不足」。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs 试用（Swagger UI）
```

本地开发（无 Docker）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # http://127.0.0.1:8000/docs
pytest -q                            # 运行自测
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./street_archive.db` | 数据库连接（容器内为 `sqlite:////data/street_archive.db`） |
| `VERIFY_DELAY_SECONDS` | `0.2` | 模拟核查耗时；核查期间资料仍可改动 |

## 数据模型

- **事项 Matter**：`old_name` + `new_name` + `effective_date` 唯一归档；`version` 随资料变动递增。
- **原件档号 Document**：属于某事项，状态 `在册 / 已撤回`，可登记、撤回、重新登记。
- **转引条目 Entry**：属于某事项，状态 `在录 / 已撤回`，可登记、撤回。
- **引用 Citation**：条目 → 另一条目（转引，可互引成环）或条目 → 原件档号；
  **限于同一事项**，可整体改录（PUT 替换）。
- **核查 Verification**：`在途 → 佐证成立 / 佐证不足`；本事项资料变动后旧结论置 `待复核`、
  在途核查置 `退回`；其他事项的变动互不影响。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/matters` | 新建事项（旧名、新名、生效日） |
| GET | `/matters` / `/matters/{id}` | 列表 / 详情（含档号、条目、引用、最近结论） |
| POST | `/matters/{id}/documents` | 登记档号（重复在册 409；已撤回则重新登记） |
| DELETE | `/matters/{id}/documents/{archive_no}` | 撤回档号 |
| POST | `/matters/{id}/entries` | 登记条目（可同时给出引用） |
| DELETE | `/matters/{id}/entries/{entry_id}` | 撤回条目 |
| PUT | `/matters/{id}/entries/{entry_id}/citations` | 改录引用（整体替换） |
| POST | `/matters/{id}/verifications?wait=true` | 按起点条目发起核查（202 异步；`wait=true` 等待结果） |
| GET | `/matters/{id}/verifications` | 查询本事项结论（可按条目、状态过滤） |
| GET | `/verifications/{vid}` | 查询单次核查 |

引用目标二选一：`{"entry_id": 3}`（转引同事事项条目）或 `{"archive_no": "A-001"}`（注明档号）。

## 典型流程（/docs 中依次执行）

1. `POST /matters`：`{"old_name":"中山路","new_name":"解放路","effective_date":"2026-01-01"}`
2. `POST /matters/1/documents`：登记 `A-001`、`A-002`
3. `POST /matters/1/entries`：
   - `县志卷三`，引用 `[{"archive_no":"A-001"}]`
   - `地名志`，引用 `[{"archive_no":"A-002"}]`
   - `市志`，引用 `[{"entry_id":<县志id>},{"entry_id":<地名志id>}]`
4. `POST /matters/1/verifications?wait=true`，`{"entry_id":<市志id>}`
   → `佐证成立`，返回两条引用链。
5. 把 `地名志` 的引用改录为 `A-001` 再核查 → `佐证不足`；原结论变为 `待复核`。

## 事务与并发

- 每次资料写入（登记/撤回/改录）在一个事务内完成并联动更新结论状态；
  任一步失败整体回滚，**沿用原资料和结论**（返回 500）。
- 核查在后台进行：先取图快照计算，再条件写入结论（仅当仍为「在途」）。
  核查期间资料可正常改动；若本事项资料变动，该核查被置为「退回」，结论不会被覆盖。
