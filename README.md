# OCI Image Manifest Resolver

本地运行的 **OCI 镜像清单解析服务**，基于 Python + FastAPI + SQLite。

服务支持按摘要（digest）录入 OCI Image Index / Image Manifest（同时兼容 Docker v2 schema 2 的
manifest list / manifest），然后根据客户端提交的平台参数（`os`、`architecture`、`variant`、
`os.version`、`os.features`），从指定的根摘要出发递归选择子描述符，最终解析出**唯一可部署的
Image Manifest**，并返回整条摘要链。

此外服务提供**发布通道（release channel）晋级**：一次晋级为 1～32 个目标平台逐一解析唯一
Manifest，全部成功后在单个 SQLite 事务中以 compare-and-swap 原子移动通道指针，并向不可变
晋级流水追加一条记录。

服务还提供**清单存储回收（垃圾回收）**维护操作：以所有通道当前摘要与晋级历史 `rootDigest`
为根，递归标记 Index 的 `manifests` 引用，把不再被任何发布记录引用的清单文档在单个 SQLite
写事务中删除；支持 `dryRun` 预演与 `limit` 分批限量，回收后已有通道仍可正常解析。

## 一条命令启动

需要本机已安装 Docker（含 Compose 插件）：

```bash
docker compose up -d --build
```

服务启动后监听 **8000** 端口：健康检查 <http://localhost:8000/healthz>，交互文档
<http://localhost:8000/docs>。数据保存在命名卷 `oci-resolver-data` 内的 SQLite 文件
`/data/resolver.db`（容器内）。

停止服务：`docker compose down`（加 `-v` 可同时删除数据卷）。

### 不使用 Docker（本地开发）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

此时数据库默认位于 `./data/resolver.db`，可用环境变量 `OCI_RESOLVER_DB` 覆盖；
递归索引层数上限可用 `OCI_MAX_INDEX_HOPS`（默认 32）调整。

---

## 平台匹配规则

对每个 Image Index 的子描述符按如下规则筛选、排序：

1. **`os` 与 `architecture` 必须精确匹配**（区分大小写），否则直接淘汰；
2. **`variant`**：客户端请求值与候选精确相同 → `exact`（最优）；候选为空 →
   `fallback-empty`（回退）；候选为另一个非空值 → 不兼容，淘汰；
3. **`os.version`**：规则与 `variant` 相同，精确项优先于空值回退；
4. **`os.features`**：客户端请求的**每一个** feature 都必须出现在候选中（候选可以有额外
   feature），否则淘汰；
5. 当客户端**未约束** `variant` / `os.version` 时，值为空的候选优先于带具体值的候选；
6. 排序时 `variant` 优先于 `os.version`（与 containerd 的平台排序一致）；
7. 最优优先级上若存在多个候选，则返回 `409 AMBIGUOUS_MATCH`，并给出并列候选详情。

解析过程中还会：

- 校验每个被跟随的描述符 `mediaType` 与其指向文档的实际类型一致；
- 识别**缺失摘要**（`DIGEST_NOT_FOUND`）、**循环引用**（`CIRCULAR_REFERENCE`）、
  **深度超限**（`DEPTH_EXCEEDED`，默认最多 32 层 Index 跳转）；
- 所有成功响应与错误响应都返回经过的摘要链 `chain`。

## API

### 1. 录入清单文档

```
PUT /manifests/{digest}
Content-Type: application/json
```

请求体为清单原始 JSON。`{digest}` 支持 `sha256:<64位十六进制>` 或裸 64 位十六进制。
服务会重新计算 SHA-256 并与 URL 中的摘要比对，不一致返回 `INVALID_DIGEST`；文档类型由
`mediaType` 字段与 JSON 形状（`manifests` / `config`）共同判定。重复录入同一摘要为幂等
更新。

```bash
curl -sS -X PUT --data-binary @index.json \
  -H 'Content-Type: application/json' \
  http://localhost:8000/manifests/sha256:$(sha256sum index.json | cut -d' ' -f1)
```

### 2. 查询已录入文档

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/manifests` | 列出所有已录入文档（摘要、mediaType、类型、大小、录入时间） |
| GET | `/manifests/{digest}` | 返回文档原始 JSON，响应头带 `Docker-Content-Digest` 与 `Content-Type` |
| HEAD | `/manifests/{digest}` | 仅检查文档是否存在 |

### 3. 递归解析可部署清单

```
POST /resolve
Content-Type: application/json

{
  "rootDigest": "sha256:5f...",
  "platform": {
    "os": "linux",
    "architecture": "arm64",
    "variant": "v7",
    "os.version": "1.0",
    "os.features": ["foo", "bar"]
  }
}
```

`variant`、`os.version`、`os.features` 可省略；平台字段也允许直接放在请求顶层。

成功响应（200）：

```json
{
  "digest": "sha256:ab...",
  "mediaType": "application/vnd.oci.image.manifest.v1+json",
  "kind": "manifest",
  "manifest": { "...": "解析出的 Image Manifest 全文" },
  "descriptor": { "mediaType": "...", "digest": "sha256:ab...", "size": 123,
                  "platform": { "os": "linux", "architecture": "arm64" } },
  "platform": { "os": "linux", "architecture": "arm64", "variant": "v7" },
  "chain": ["sha256:5f...", "sha256:9c...", "sha256:ab..."],
  "indexHops": 2,
  "request": { "os": "linux", "architecture": "arm64", "variant": "v7" }
}
```

### 4. 发布通道晋级

通道（channel）是一个具名的、指向某个根摘要的发布指针（例如 `stable`、`beta`）。
指针只能通过**晋级（promote）**移动，每次成功晋级都会追加一条不可变历史记录。

```
POST /channels/{name}/promote
Content-Type: application/json

{
  "rootDigest": "sha256:5f...",
  "platforms": [
    { "os": "linux", "architecture": "amd64" },
    { "os": "linux", "architecture": "arm64", "variant": "v7" }
  ],
  "expectedDigest": "sha256:9c..."
}
```

- `{name}`：1～64 位 `[A-Za-z0-9._-]`，且以字母或数字开头；
- `platforms`：**1～32** 个平台选择器，字段与 [`POST /resolve`](#3-递归解析可部署清单)
  完全一致（`os`、`architecture` 必填，`variant`、`os.version`、`os.features` 可选）；
- `expectedDigest`：可选的乐观锁。给出时必须等于通道当前摘要（通道不存在时当前值为
  `null`，因此新通道晋级不能携带非空期望摘要）。

**两阶段语义**：

1. **先解析，后写入**。服务先用现有 `/resolve` 规则为每个平台独立解析唯一 Manifest。
   任一平台失败则整体拒绝（HTTP `409 PROMOTION_FAILED`），响应 details 中同时给出每个
   平台的结果 `results` 与失败诊断 `failures`（含原始 `code`、`message`、`chain` 等），
   **通道指针与历史流水均不变化**；
2. **单事务 compare-and-swap**。全部平台解析成功后，服务在同一个 SQLite 事务中
   （`BEGIN IMMEDIATE`）校验 `expectedDigest`、移动指针、追加历史。期望摘要过期时返回
   `409 PROMOTION_CONFLICT`（details 含 `expectedDigest` 与 `currentDigest`），事务回滚，
   不写入任何内容。

成功响应（200）：

```json
{
  "channel": "stable",
  "sequence": 2,
  "rootDigest": "sha256:5f...",
  "oldDigest": "sha256:9c...",
  "newDigest": "sha256:5f...",
  "results": [
    { "request": {"os": "linux", "architecture": "amd64"},
      "platform": {"os": "linux", "architecture": "amd64"},
      "digest": "sha256:ab...", "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "descriptor": { "...": "命中的子描述符" },
      "chain": ["sha256:5f...", "sha256:ab..."], "indexHops": 1 }
  ],
  "createdAt": "2026-09-23T10:00:00.000Z"
}
```

`newDigest` 即晋级的根摘要（通道指针指向它）；`oldDigest` 在首次晋级时为 `null`；
`sequence` 是通道内从 1 开始、严格递增、无空洞的不可变序号。

### 5. 查询通道当前值与游标历史

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| GET | `/channels` | 列出全部通道（名称、当前摘要、序号、更新时间） |
| GET | `/channels/{name}` | 通道当前值；通道不存在返回 `404 CHANNEL_NOT_FOUND` |
| GET | `/channels/{name}/events` | 晋级历史（不可变流水），按 `sequence` **倒序**返回 |

历史查询参数：`limit`（1～100，默认 50）、`cursor`（上一页响应中的 `nextCursor`，不透明、
仅本通道可用）。响应：

```json
{
  "channel": "stable",
  "items": [
    { "sequence": 2, "rootDigest": "sha256:5f...",
      "oldDigest": "sha256:9c...", "newDigest": "sha256:5f...",
      "platforms": [ { "...": "与晋级响应中 results 相同的逐平台结果" } ],
      "createdAt": "2026-09-23T10:00:00.000Z" }
  ],
  "nextCursor": "eyJjaGFubmVsIjoic3RhYmxlIiwiYmVmb3JlIjoyfQ"
}
```

最后一页之后 `nextCursor` 为 `null`（不会导向空页）。历史记录只追加、永不修改或删除。

### 6. 清单存储回收（维护操作）

```
POST /maintenance/gc
Content-Type: application/json

{ "dryRun": true, "limit": 100 }
```

请求体可整体省略（等价于 `{}`）。字段：

- `dryRun`：可选，默认 `false`。为 `true` 时只计算并返回**将被删除**的摘要列表与总
  字节数，不修改数据库；
- `limit`：可选正整数。给出时本次最多删除 `limit` 个候选，优先删除**摘要字典序最小**
  的文档；响应中的 `remaining` 给出本次之后仍剩余的候选数量，便于分批回收。

**可达性规则**：以所有通道的当前摘要和晋级流水中全部 `rootDigest` 为根，递归跟随 Index
文档 `manifests` 数组中的子摘要；可达集合之外的已存储文档即为垃圾。整个标记-清除在**同一
个 SQLite 写事务**（`BEGIN IMMEDIATE`）中完成：事务期间晋级操作被阻塞，任何解析或删除
失败都会整体回滚，已有通道在回收后仍可正常解析。

成功响应（200）：

```json
{
  "dryRun": false,
  "roots": 3,
  "reachable": 5,
  "candidates": 4,
  "limit": 2,
  "digests": ["sha256:11...", "sha256:22..."],
  "bytes": 2048,
  "remaining": 2
}
```

`roots` / `reachable` / `candidates` 分别为根摘要数、可达文档数与垃圾候选总数；
`digests` 与 `bytes` 是本次实际删除（`dryRun` 时为**将要**删除）的摘要列表与总字节数。

```bash
# 预演：只看会删什么，不改库
curl -sS -X POST http://localhost:8000/maintenance/gc \
  -H 'Content-Type: application/json' -d '{"dryRun": true}'

# 实际回收，本次最多删除 100 个（字典序最小优先）
curl -sS -X POST http://localhost:8000/maintenance/gc \
  -H 'Content-Type: application/json' -d '{"limit": 100}'

# 不带请求体：回收全部垃圾
curl -sS -X POST http://localhost:8000/maintenance/gc
```

### 错误响应格式

所有错误均为：

```json
{ "error": { "code": "ERROR_CODE", "message": "人类可读说明", "details": { "chain": ["..."] } } }
```

| code | HTTP | 含义 |
| ---- | ---- | ---- |
| `INVALID_DIGEST` | 400 | 摘要格式错误或与请求体实际 SHA-256 不一致 |
| `INVALID_DOCUMENT` | 400 | 不是合法 JSON，或既不是 Index 也不是 Manifest |
| `INVALID_REQUEST` | 400/422 | `/resolve`、`/promote` 请求字段缺失或类型错误 |
| `INVALID_CHANNEL` | 400 | 通道名非法（1～64 位 `[A-Za-z0-9._-]`，以字母或数字开头） |
| `INVALID_CURSOR` | 400 | 历史游标无法解析或属于其他通道 |
| `UNSUPPORTED_MEDIA_TYPE` | 409 | 出现未知 / 不支持的 mediaType |
| `MEDIA_TYPE_MISMATCH` | 409 | 描述符声明的 mediaType 与目标文档实际类型不一致 |
| `DIGEST_NOT_FOUND` | 404 | 根或链路中引用的摘要尚未录入 |
| `NO_MATCHING_PLATFORM` | 404 | Index 中没有满足平台条件的候选（details 含 `available_platforms`） |
| `AMBIGUOUS_MATCH` | 409 | 同一最优优先级存在多个候选（details 含 `tied_candidates` 与全部候选） |
| `CIRCULAR_REFERENCE` | 409 | 索引引用成环（details 的 chain 给出完整环路径） |
| `DEPTH_EXCEEDED` | 409 | Index 递归跳转超过 `OCI_MAX_INDEX_HOPS`（默认 32） |
| `CHANNEL_NOT_FOUND` | 404 | 通道尚不存在（从未成功晋级） |
| `PROMOTION_FAILED` | 409 | 晋级被拒：至少一个目标平台无法唯一解析（details 含逐平台 `results`/`failures`），通道不变 |
| `PROMOTION_CONFLICT` | 409 | `expectedDigest` 与通道当前值不一致（compare-and-swap 失败），未写入 |
| `GC_FAILED` | 500 | 回收事务执行失败，已整体回滚，数据库未发生变化 |

## 端到端示例

```bash
# 1) 录入一个 Image Manifest
MANIFEST='{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",
"config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"sha256:'$(printf 0%.0s {1..64})'","size":2},
"layers":[]}'
M_DIGEST=sha256:$(printf '%s' "$MANIFEST" | sha256sum | cut -d' ' -f1)
curl -sS -X PUT --data "$MANIFEST" -H 'Content-Type: application/json' \
  http://localhost:8000/manifests/$M_DIGEST

# 2) 录入引用该 manifest 的 Image Index
INDEX='{"schemaVersion":2,"mediaType":"application/vnd.oci.image.index.v1+json",
"manifests":[{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"'$M_DIGEST'","size":0,
"platform":{"os":"linux","architecture":"amd64"}}]}'
I_DIGEST=sha256:$(printf '%s' "$INDEX" | sha256sum | cut -d' ' -f1)
curl -sS -X PUT --data "$INDEX" -H 'Content-Type: application/json' \
  http://localhost:8000/manifests/$I_DIGEST

# 3) 按平台解析
curl -sS -X POST http://localhost:8000/resolve \
  -H 'Content-Type: application/json' \
  -d '{"rootDigest":"'$I_DIGEST'","platform":{"os":"linux","architecture":"amd64"}}'

# 4) 晋级到 stable 通道（多平台 + 乐观锁可选）
curl -sS -X POST http://localhost:8000/channels/stable/promote \
  -H 'Content-Type: application/json' \
  -d '{"rootDigest":"'$I_DIGEST'",
       "platforms":[{"os":"linux","architecture":"amd64"}],
       "expectedDigest":"sha256:<当前摘要，首次晋级省略>"}'

# 5) 查询当前值与晋级历史
curl -sS http://localhost:8000/channels/stable
curl -sS 'http://localhost:8000/channels/stable/events?limit=20'

# 6) 清单存储回收：先 dryRun 预演（只看将删除的摘要与字节数），再实际执行
curl -sS -X POST http://localhost:8000/maintenance/gc \
  -H 'Content-Type: application/json' -d '{"dryRun": true}'
curl -sS -X POST http://localhost:8000/maintenance/gc \
  -H 'Content-Type: application/json' -d '{"limit": 100}'
```

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

测试覆盖：精确匹配、variant / os.version 精确优先与空值回退、os.features 全包含、
同优先级歧义、缺失摘要、mediaType 不一致、循环引用、深度超限、嵌套递归、摘要链、
Docker 媒体类型与幂等录入；以及通道晋级的多平台解析、平台级诊断与失败原子性、
compare-and-swap 冲突（含并发竞争）、不可变序号流水与游标分页；以及清单回收的
dryRun 预演、`limit` 限量与字典序、历史 `rootDigest` 保活、嵌套树保留、回收后通道
仍可解析、回收事务阻塞并发晋级与失败整体回滚。

## 项目结构

```
app/
  main.py       FastAPI 路由与请求/错误模型
  resolver.py   递归解析、链 / 循环 / 深度 / mediaType 校验
  platform.py   平台筛选与候选排序
  channels.py   晋级编排（逐平台解析、诊断收集）与历史游标
  gc.py         垃圾回收可达性标记（通道/历史根集合 → 递归 Index 引用）
  oci.py        mediaType、digest 与文档类型工具
  storage.py    SQLite 存储（清单、通道指针、不可变晋级流水、回收事务）
  errors.py     错误码与 HTTP 状态映射
Dockerfile
compose.yaml
requirements.txt
tests/
```
