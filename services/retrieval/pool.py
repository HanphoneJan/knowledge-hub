"""模型池与生命周期管理（D29）。

**为什么要池而不是每次现加载**：
- bge-small-zh 加载约 2.5s，colSmol-500M 加载约 8s
- D29 定的策略：**核心预热 + 重模型懒加载 + LRU 缓存**

    核心（bge-small 100MB）→ 进程启动时预热。API 常驻要用。
    重模型（colSmol 2.5GB）→ 首次用到才加载，worker 独占内存。
    LRU 缓存 → embedding 结果按文本哈希缓存，重复查询不重算。

**实测依据**（本机 12 核、无 GPU，2026-10-04 重测）：
- bge-small 编码 88ms/chunk（重建 4602 chunk ≈ 7 分钟）
- colSmol 编码 **≈15s/图**（float32；bf16 在本机 CPU 上慢 4.6 倍 → 68s），
  内存峰值 2.8GB
- ⚠️ 这个成本**与图片分辨率无关**（实测 400×300 与 3000×4000 同为
  ~862 token / 13 个 512×512 图块）—— Idefics3 会把长边**重采样到 2048px**
  再切块，降 DPI 一分钱都不省（详见 `get_visual_model` 的说明）
- colSmol batch=2 反而慢 7%（CPU 无并行收益），所以**不要**为省时间而批处理

进程分离（D50）：API 只装 bge，Worker 才装 colSmol。
这个模块两边共用，但通过 `enable_visual` 开关决定是否允许加载重模型。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from ..common.config import get_settings

# ---------------- 状态 ----------------

_settings = get_settings()

_lock = threading.RLock()
_text_model: Any = None
_visual_model: Any = None

_enabled = {"visual": False}  # API 进程设 False；worker 设 True

#: embedding 结果缓存。key = 模型名 + 文本的 sha256。
#: 实测重复查询占比不低（同一文档反复问同一个问题），缓存命中率约 30~40%。
_EMBED_CACHE_MAX = 4096
_embed_cache: OrderedDict[str, list[float]] = OrderedDict()

_load_stats: dict[str, Any] = {
    "text_loaded_at": None,
    "text_load_ms": None,
    "visual_loaded_at": None,
    "visual_load_ms": None,
    "embed_cache_hits": 0,
    "embed_cache_misses": 0,
}


# ---------------- 文本嵌入模型 ----------------


def get_text_model() -> Any:
    """拿 bge-small-zh-v1.5（全局单例）。

    懒加载 + 双重检查：并发首次调用只加载一次。
    """
    global _text_model
    if _text_model is not None:
        return _text_model
    with _lock:
        if _text_model is None:
            t0 = time.perf_counter()
            from sentence_transformers import SentenceTransformer

            _text_model = SentenceTransformer(
                _settings.text_embed_model, device="cpu"
            )
            _load_stats["text_load_ms"] = round((time.perf_counter() - t0) * 1000)
            _load_stats["text_loaded_at"] = time.time()
    return _text_model


def encode_texts(texts: list[str], *, batch_size: int = 16) -> list[list[float]]:
    """编码文本为稠密向量。

    @param batch_size 实测 CPU 上 16 是甜点（更大反而因内存带宽饱和变慢）
    """
    if not texts:
        return []

    # 缓存命中判定
    import hashlib

    keys = [
        hashlib.sha256(f"{_settings.text_embed_model}\x00{t}".encode()).hexdigest()
        for t in texts
    ]
    with _lock:
        hits = {k: _embed_cache[k] for k in keys if k in _embed_cache}
        missing_idx = [i for i, k in enumerate(keys) if k not in hits]
        for k in hits:
            _embed_cache.move_to_end(k)

    if missing_idx:
        model = get_text_model()
        vectors = model.encode(
            [texts[i] for i in missing_idx],
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        with _lock:
            for i, vec in zip(missing_idx, vectors, strict=True):
                key = keys[i]
                _embed_cache[key] = vec.tolist()
                _embed_cache.move_to_end(key)
            _load_stats["embed_cache_misses"] += len(missing_idx)
            _load_stats["embed_cache_hits"] += len(hits)
            # LRU 淘汰
            while len(_embed_cache) > _EMBED_CACHE_MAX:
                _embed_cache.popitem(last=False)

    with _lock:
        return [_embed_cache[k] for k in keys]


def encode_query(text: str) -> list[float]:
    """单条查询编码。走同一缓存。"""
    return encode_texts([text])[0]


def encode_late_spans(
    full_text: str,
    spans: list[tuple[int, int]],
    *,
    max_length: int = 512,
) -> list[list[float]]:
    """Late Chunking 编码（D164）：整篇过一次 Transformer，按 span 池化。

    @param full_text 整篇文本
    @param spans 每个子块的 `(char_start, char_end)`（`strategies.spans_for` 产出）
    @return 与 `spans` **等长**的向量列表（512 维、已 L2 归一化，与 `encode_texts` 同构）

    ## 做法
    1. tokenizer 带 `offset_mapping` 把字符区间映射到 token 下标；
    2. 对**整篇**做一次前向（`output_hidden_states=True`），拿最后一层 hidden；
    3. 每个 span 取落在其区间内的 token，用**第一个 token（span 的 CLS）**做池化，
       再 L2 归一化。

    ## ⚠️ 诚实边界（写进 `DESIGN-HISTORY.md §2`）
    bge-small 最大长度 **512 token**，超长文档会被截断 ⇒ "整篇注意力"的收益
    只在 512 token 以内成立，**不假装它对长文档一定更好**。落在截断后的 span
    取不到 token 时，退化为"取整篇第一个 token"，至少保证**长度一致**。

    ## 失败即降级
    任何异常（模型结构变化、torch 缺等）都往上抛，由 `_index_text_legs`
    捕获后退回标准 `encode_texts` —— Late 是精度增强，**不配让摄取失败**。
    """
    if not spans:
        return []
    import torch

    model = get_text_model()
    tokenizer = model.tokenizer
    # SentenceTransformer 的第 0 个模块是 Transformer，其 auto_model 是裸 BERT。
    auto = model[0].auto_model

    enc = tokenizer(
        full_text,
        return_tensors="pt",
        max_length=max_length,
        truncation=True,
        return_offsets_mapping=True,
    )
    offsets = enc.pop("offset_mapping")[0].tolist()
    enc.pop("token_type_ids", None)

    with torch.no_grad():
        out = auto(**enc, output_hidden_states=True)
    hidden = out.hidden_states[-1][0]          # (seq, dim)

    vectors: list[list[float]] = []
    for start, end in spans:
        idxs = [i for i, (s, e) in enumerate(offsets) if s >= start and e <= end and e > s]
        if not idxs:
            # 截断后取不到：退回第一个 token，保证长度一致（见 docstring）
            idxs = [0]
        vec = torch.nn.functional.normalize(hidden[idxs[0]], dim=-1)
        vectors.append(vec.tolist())
    return vectors


# ---------------- 视觉模型（懒加载） ----------------


def enable_visual(enabled: bool = True) -> None:
    """允许本进程加载 colSmol。

    🔑 **只有两个进程该调它**（D158 之后是两个 worker 侧 + 视觉查询服务）：
    | 进程 | 为什么 |
    |---|---|
    | `services/worker` | 写入侧编码图片（15s/图） |
    | `services/visual_service` | 读侧编码查询（119~128ms/条） |
    | `services/api` | **绝不** —— +2.65GB 常驻、13s 首请求，§4.1b/c 有实测 |

    ⚠️ 它**不判断进程身份**：API 进程硬要调也能开。这是刻意的 ——
    判"我是不是 API 进程"要么读 argv（脆），要么读环境变量（更脆：
    uvicorn 有 `--factory`，`uv run` 会插参数）。`get_visual_model()`
    在**没开开关**时主动抛错 + `tests/test_visual_service.py` 钉住
    "API 进程里它仍抛错"，比在这里猜进程身份可靠。
    """
    _enabled["visual"] = enabled


def visual_enabled() -> bool:
    return _enabled["visual"]


def get_visual_model() -> Any:
    """拿 colSmol-500M（懒加载，2.5GB）。**必须用 `MultiVectorEncoder`**。

    只在 `enable_visual(True)` 后可用 —— 否则说明调用方搞错了进程
    （D50：API 进程不该加载重模型）。

    ## 🔑 为什么不能是 `AutoModel`（D124，这是 `kb_visual` 恒为 0 点的根因）

    `vidore/colSmol-500M` **不是**一个裸 transformers 模型，而是一个
    SentenceTransformers 的 **MultiVectorEncoder**：

    ```
    modules.json = Transformer → 1_Dense(960 → 128) → Normalize → 3_MultiVectorMask
    ```

    裸 `AutoModel` 只拿到 Transformer，**投影层被丢掉**（加载日志里那句
    `linear.weight | UNEXPECTED` 就是它），输出最后一维是 **960**。
    而 `kb_visual` 是 `VDR_DIM = 128` → upsert 必然 400。
    第一版还叠加了第二个错：只给 processor 传 `images=` 不传 `text=`，
    于是 Idefics3 的 forward 先抛
    `ValueError: You must specify exactly one of input_ids or inputs_embeds`
    —— 8 个 `embed_visual` 任务全 failed，`kb_visual` 一直是 0 点，
    而症状看起来像"库里没有照片"。

    `MultiVectorEncoder` 把 processor 的调用也一起管对了：
    文档侧自己补 `<image>` 占位、查询侧不传 `images`
    （照 colSmol README 写 `images=[[]]` 在 transformers 5.18 会抛
    `AttributeError: 'list' object has no attribute 'device'`）。

    ## 耗时（2026-10-04 实测，D124）

    `Idefics3ImageProcessor(do_image_splitting=True, size.longest_edge=2048,
    max_image_size.longest_edge=512, image_seq_len=64)`：
    先把长边**重采样到 2048px**，再切成 512×512 的图块，每块 64 token。

    ⚠️ **成本是常数**（13 图块 / ~862 token），与图片分辨率、DPI 无关：
    400×300 与 3000×4000 都是 13 块。所以 D37 的"扫描件 50 页上限"
    （≈12 分钟）成立，**不要再拿"降分辨率能省时间"做优化**。

    ⚠️ **dtype 必须钉成 float32**：ST 默认按 checkpoint 的 dtype 加载
    （本模型是 **bfloat16**），而本机 CPU（i5-13420H，**无 avx512_bf16 / AMX**）
    的 bf16 矩阵乘是软件模拟 —— 实测 **68.4s/图 vs fp32 14.9s/图（慢 4.6 倍）**。
    模型本身是 bf16 训的，但推理精度更高不会更差，所以这里选快的那个。
    （⚠️ 有 AMX/AVX512-BF16 的服务器结论可能反过来 —— 那种机器上要重测，
    换 dtype 等于换模型，按 D22 的纪律记进 PROGRESS。）
    """
    global _visual_model
    if not _enabled["visual"]:
        raise RuntimeError(
            "视觉模型未启用。本进程是 API（应只用文本模型），"
            "视觉编码请交给 worker 进程。"
        )
    if _visual_model is not None:
        return _visual_model
    with _lock:
        if _visual_model is None:
            t0 = time.perf_counter()
            import torch
            from sentence_transformers import MultiVectorEncoder

            # ⚠️ 不要在这里做"省时间"的重采样设置：实测降分辨率/降 DPI
            #   **完全无效**（输出恒为 ~862 token），因为重采样发生在
            #   Idefics3 的 processor 内部，不是我们这一侧。
            _visual_model = MultiVectorEncoder(
                _settings.vdr_model,
                device="cpu",
                # 见 docstring：bf16 在本机 CPU 上是软件模拟，慢 4.6 倍
                model_kwargs={"dtype": torch.float32},
            ).eval()
            _load_stats["visual_load_ms"] = round(
                (time.perf_counter() - t0) * 1000
            )
            _load_stats["visual_loaded_at"] = time.time()
    return _visual_model



def _multi_vector(tensor: Any) -> list[list[float]]:
    """multi-vector 张量 → `list[list[float]]`（**float32**）。

    ⚠️ 两个实测坑都在这里（PROGRESS §5.8）：

    1. 输出是 **bfloat16**（模型训练格式）。`np.array(out)` 会炸
       `Got unsupported ScalarType BFloat16` —— 所以这里统一 `.float()`，
       下游（Qdrant / JSON）只拿 float32 列表。
    2. **只切掉 batch 维**：`out[0]` 才是 `(n_tokens, 128)`。
       少切一层会双重嵌套 → Qdrant 400。
    """
    return tensor.float().tolist()


def encode_images(
    pil_images: list[Any], *, max_batch: int = 1
) -> list[list[list[float]]]:
    """编码图片为 multi-vector（colSmol）→ 每个 `(n_tokens, 128)`。

    @param max_batch **实测必须为 1**。batch=2 反而慢 7%
    （CPU 无并行收益，反而多占一份内存）。参数留着是为了把这个坑显式化。
    """
    if not pil_images:
        return []
    if max_batch != 1:
        raise ValueError(
            "colSmol 在 CPU 上 batch 化无收益（实测 batch=2 慢 7%），"
            "请用 max_batch=1"
        )

    model = get_visual_model()
    out: list[list[list[float]]] = []
    for img in pil_images:
        # ⚠️ `encode_document` 走 modules.json 里的 document prompt，
        #   自己会补 `<image>` 占位 —— **不要**在这里手工调 processor
        #   （D124：第一版就是这么写的，于是 8 个任务全 failed）。
        vecs = model.encode_document(
            [img], batch_size=1, show_progress_bar=False
        )
        out.append(_multi_vector(vecs[0]))
    return out


def encode_vdr_query(text: str) -> list[list[float]]:
    """colSmol 查询编码 → `[[128], [128], ...]`，形状 `(n_tokens, 128)`。

    给 Qdrant 的 multi-vector MaxSim 用（`kb_visual`）。

    ⚠️ **不要传 `images`**（哪怕是 `images=[[]]`）：那是 colSmol README 里的
    写法，transformers 5.18 会抛
    `AttributeError: 'list' object has no attribute 'device'`（D124 实测）。

    ⚠️ **只在 `enable_visual(True)` 的进程里可用**（`get_visual_model` 会主动抛错）。
    API 进程调用 `get_visual_model()` 会得到一句明确的 RuntimeError，
    这比返回空结果好 —— 后者会让人以为"库里就是没有照片"。
    """
    model = get_visual_model()
    return _multi_vector(
        model.encode_query([text], batch_size=1, show_progress_bar=False)[0]
    )



# ---------------- 远端编码（视觉查询服务，D158） ----------------
#
# 🔑 **本地 `encode_vdr_query` 与远端 `encode_vdr_query_remote` 是两个函数，
#   不是一个带参数的** —— 因为它们的可用性判据不同：
#   本地要"本进程开了 `enable_visual`"，远端要"另一个进程开着且模型就绪"。
#   合成一个 `encode_vdr_query(text, *, remote=False)` 的话，
#   "什么时候该走远端"这个判断就会散落到调用点 —— 而那正是
#   §4.1 想守住的那条边界（API 进程绝不能碰 colSmol）。
#   ⚠️ worker 的**写**路径（编码图片）默认仍用本地那个；当
#   `visual_write_via_service=true` 时改走远端（`encode_image_remote_sync`，
#   D178）—— 那是为了 SMB 多 worker 时**不要让每个 worker 各占 2.65GB**。
#   代价是摄取依赖视觉服务，见 config 里那条开关的说明。


class VisualServiceError(RuntimeError):
    """远端编码失败。**每一种都会降级成 `skipped`，绝不 500。**

    🔑 单独一个异常类型（而不是复用 `httpx` 的那些）是因为调用方要的
    不是"哪一类 HTTP 错误"，而是**一句话能照着修的话**：
    连不上 → "服务没起"；503 → "模型没就绪"；超时 → "太慢"。
    这三种的处置完全不同，写成一个通用异常就得去猜 `type(exc)`。
    """

    def __init__(self, reason: str, *, hint: str) -> None:
        super().__init__(f"{reason}；{hint}")
        self.reason = reason
        #: **可操作**的修复指引。`/search/debug` 的 `skipped` 会原样带出去 ——
        #: 一个只说"失败"的 skipped 会让人花几小时查错方向（§5.93/§5.94 那族）。
        self.hint = hint


#: 全局单例。⚠️ **不要改成"每次请求新建"**：那会每次重握手 + 重解析 DNS，
#:   而视觉腿是每次检索都可能被调到的热路径。
_async_client: Any = None
#: 一个事件循环只能绑一个 client（`test_unawaited_coroutines.py` 那一族），
#: 所以换个事件循环（pytest 每个用例一个）必须重建。
_async_client_loop: Any = None

_remote_sem: Any = None
_remote_sem_size: int | None = None

#: `/health` 探测结果的缓存（`(截止时刻, 结论)`）。理由见 `visual_available`。
_probe_cache: dict[str, Any] = {"at": 0.0, "value": None}

#: 降级文案里的**修复指引**（§5.7：不可达也是一种状态，不是故障）。
#: 🔑 它住在 `pool.py` 而不散在 `visual.py` 的各条 `if` 分支里 ——
#:   "怎么修"只有一份，运维看到的那句话与这里逐字一致。
_REMOTE_HINT = (
    "起它：`bash deploy/dev.sh start`（它常驻 colSmol，约 +2.65GB）；"
    "不装视觉服务时检索照常返回 dense+sparse+trgm 三条文本腿"
)


def remote_hint() -> str:
    """降级文案里的修复指引（**唯一**来源）。"""
    return _REMOTE_HINT


def _get_async_client() -> Any:
    """进程级单例 `httpx.AsyncClient`（跨循环时自动重建，见上方注释）。"""
    global _async_client, _async_client_loop
    import asyncio

    import httpx

    loop = asyncio.get_running_loop()
    if _async_client is None or _async_client_loop is not loop:
        if _async_client is not None:
            # 🔴 上一个事件循环已经关了，它的连接池在上面 ⇒ 复用会得到
            #   `got Future ... attached to a different loop`（§6 坑 #3）。
            #   httpx 的 `aclose()` 在已关的循环上也会炸 ⇒ 直接弃用，
            #   让它随 GC 走（同 §5 的做法）。
            _async_client = None
        _async_client = httpx.AsyncClient(
            base_url=get_settings().visual_service_url,
            timeout=httpx.Timeout(
                get_settings().visual_service_timeout_s, connect=1.0
            ),
            # 🔑 与 `common/qdrant.py` 同一条理由：httpx 默认读 HTTP_PROXY，
            #   本机的 fclash（127.0.0.1:7890）会把回环请求也送去代理。
            trust_env=False,
        )
        _async_client_loop = loop
    return _async_client


def _get_remote_sem() -> Any:
    """远端编码的并发闸（anyio 信号量，懒建）。

    ⚠️ **上限 4 不是随手取的**：实测 6 路并发吃 **8.7 个核**、单条 p95
    涨到 **905ms**（`CONVENTIONS.md` §4.1c）。放行无限并发不会更快，
    只会让每一条都更慢 —— 而检索有 2s 的总预算（§4 编码 p95 的两倍余量）。
    超限就在这里排队；**排队也算进那 2s**，排不到就降级。
    """
    global _remote_sem, _remote_sem_size
    import anyio

    want = get_settings().visual_service_max_concurrency
    if _remote_sem is None or _remote_sem_size != want:
        _remote_sem = anyio.Semaphore(want)
        _remote_sem_size = want
    return _remote_sem


def _remote_detail(status: int, body: Any) -> str:
    """把远端的错误体变成人话（`{"code","detail"}` 是协议的一部分）。"""
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("code") or "")[:200]
        if detail:
            return detail
    return f"HTTP {status}"


async def visual_service_health(*, ttl: float | None = None) -> dict[str, Any]:
    """探一次视觉服务 `/health`。**结果缓存 `probe_ttl_s` 秒**。

    🔑 为什么缓存：这条在**每次检索**的路径上。不缓存的话每次检索多一次
    RTT，而服务没起时那一次 RTT 是**连接超时**（本机 connect 收紧到 1s）——
    于是"服务挂了"会把每次检索都拖慢 1s，而它本来完全可以立刻降级。
    ⇒ TTL 取 5s：够短（服务起来后最多 5s 就开始用），
    够长（不会把一次探活变成每次检索的固定开销）。

    ⚠️ **绝不抛错**：探测失败就是 `{"reachable": False, ...}`。
    这个函数的唯一用途是回答"能不能用"，让它抛错等于把诊断变成故障。
    """
    import time as _time

    settings = get_settings()
    if not settings.visual_service_url:
        return {"reachable": False, "reason": "未配置 VISUAL_SERVICE_URL（读侧关闭）"}

    effective_ttl = settings.visual_service_probe_ttl_s if ttl is None else ttl
    now = _time.monotonic()
    if effective_ttl > 0 and (now - _probe_cache["at"]) < effective_ttl:
        cached = _probe_cache["value"]
        if cached is not None:
            return cached

    def _settle(out: dict[str, Any]) -> dict[str, Any]:
        """🔑 **失败也要缓存** —— 那才是最需要缓存的那种。
        服务没起时每次探测都要等 connect 超时（本机收紧到 1s），
        而"视觉服务没起"是**稳态**（直到有人去起它），不是瞬时抖动。
        只缓存成功的话，`/health` 与每次检索都会白等那 1s。
        """
        _probe_cache.update(at=now, value=out)
        return out

    try:
        r = await _get_async_client().get("/health")
    except Exception as exc:  # noqa: BLE001
        return _settle({
            "reachable": False,
            "reason": f"连不上 {_probe_target()}",
            "error": type(exc).__name__,
        })
    if r.status_code != 200:
        return _settle({
            "reachable": False,
            "reason": f"{_probe_target()} /health → {r.status_code}",
        })
    body = r.json()
    out: dict[str, Any] = {
        "reachable": bool(body.get("model_loaded")),
        "url": settings.visual_service_url,
        "model_loaded": bool(body.get("model_loaded")),
        "pid": body.get("pid"),
        "rss_gb": body.get("rss_gb"),
        "encode_p95_ms": body.get("encode_p95_ms"),
    }
    if not body.get("model_loaded"):
        out["reason"] = (
            f"服务在跑但模型未就绪：{body.get('load_error') or '仍在加载（实测 13s）'}"
        )
    return _settle(out)


def _probe_target() -> str:
    return get_settings().visual_service_url


def invalidate_visual_probe() -> None:
    """丢掉探活缓存（测试与"服务刚重启"用）。"""
    _probe_cache.update(at=0.0, value=None)


async def encode_vdr_query_remote(text: str) -> list[list[float]]:
    """把查询文本编码成 multi-vectors，**编码发生在另一个进程**（D158）。

    @return `[[128], [128], ...]`，形状 `(n_tokens, 128)` —— 与本地
        `encode_vdr_query` **逐字一致**，所以下游（`_query_sync` → MaxSim）
        两种来源都吃得下。

    ## 🔴 最重要的一条：**失败抛 `VisualServiceError`，由调用方降级成 `skipped`**

    视觉腿是**增强项**：它挂掉不能让检索整体挂掉（D158 的核心行为约束）。
    本函数**只**负责把四类失败翻译成带修复指引的 `VisualServiceError`，
    绝不把 `httpx` 的异常原样抛出去 —— 那样的错误信息指不到真正的原因
    （"ConnectError" 不会告诉运维"去起那个进程"）。

    ## 超时与并发

    `anyio.fail_after` 包住的是**排队 + 请求**整体：超限在 `_get_remote_sem`
    那里排队，而排队的等待也算进 2s（设计 §5 第 2 条："排队超时就降级"）。
    ⚠️ 不能只给 `httpx` 设 timeout：那样"等闸"的时间不计入，
    于是并发一高，5 个请求会串成 10s 的等待，而调用方以为它有 2s。
    """
    import time as _time

    import anyio

    settings = get_settings()
    budget = settings.visual_service_timeout_s
    client = _get_async_client()

    try:
        with anyio.fail_after(budget):
            async with _get_remote_sem():
                r = await client.post("/encode", json={"text": text})
    except TimeoutError:
        raise VisualServiceError(
            f"视觉服务 {settings.visual_service_url} 超时（预算 {budget:.1f}s）",
            hint=f"它在 {budget:.1f}s 内没回（实测 6 路并发时单条 p95 905ms）；"
                 "先看它自己的负载或把它降级停掉。" + _REMOTE_HINT,
        ) from None
    except Exception as exc:  # noqa: BLE001 — httpx 的异常有好几类，都一样降级
        raise VisualServiceError(
            f"视觉服务不可达（{settings.visual_service_url}）：{type(exc).__name__}",
            hint=_REMOTE_HINT,
        ) from exc

    if r.status_code != 200:
        # ⚠️ 503 是**服务侧的问题**（模型没就绪），不是调用方的错 ⇒
        #   同样降级，但文案要说"等服务就绪"，不是"你的请求有问题"。
        raise VisualServiceError(
            f"视觉服务返回 {r.status_code}："
            f"{_remote_detail(r.status_code, _safe_json(r))}",
            hint="服务在跑但模型没就绪（实测加载 12.8~15.0s）；看 /tmp/pkh_visual.log。",
        )

    body = _safe_json(r)
    vectors = body.get("vectors") if isinstance(body, dict) else None
    if not isinstance(vectors, list) or not vectors:
        raise VisualServiceError(
            "视觉服务返回的响应里没有 vectors",
            hint="服务返回了非预期形状（协议见 DESIGN-HISTORY.md §1）。",
        )
    return vectors


def _safe_json(r: Any) -> Any:
    """`r.json()` 也不许抛（远端返回 HTML 错误页时会）。"""
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return None


# ---------------- 远端图片编码（写侧，D178） ----------------
#
# 🔑 与上面的读侧**分开**：worker 是同步进程（handler 在同步上下文里跑），
#   用 async client 得自己搭 loop（§5.17 那一族坑）⇒ 这里单开一个同步 client。

_sync_client: Any = None


def _get_sync_client() -> Any:
    """进程级单例**同步** `httpx.Client`（worker 写侧用）。"""
    global _sync_client
    if _sync_client is None:
        import httpx

        s = get_settings()
        _sync_client = httpx.Client(
            base_url=s.visual_service_url,
            timeout=httpx.Timeout(s.visual_service_image_timeout_s, connect=2.0),
            # 与 qdrant / 读侧同一理由：httpx 默认读 HTTP_PROXY，本机代理会把回环请求也送出去
            trust_env=False,
        )
    return _sync_client


def encode_image_remote_sync(png_bytes: bytes) -> list[list[float]]:
    """把**一页图**（PNG 字节）编码成 multi-vectors，编码发生在视觉服务（D178）。

    @return `[[128], [128], ...]`，与本地 `encode_images([img])[0]` **逐字一致**
        （同一个常驻模型）。

    ⚠️ 失败抛 `VisualServiceError`（`RuntimeError` 子类）—— `jobs.is_retryable`
        默认 True ⇒ 这一页的 `embed_visual` 任务会**重试**。
        **不要**吞成空向量：空向量写进 Qdrant 会得到一个"永远召回不到的点"，
        而任务显示成功（这类静默失败正是本项目最防的）。
    """
    s = get_settings()
    if not s.visual_service_url:
        raise VisualServiceError(
            "未配置 VISUAL_SERVICE_URL（写侧无法走服务）", hint=_REMOTE_HINT
        )
    try:
        r = _get_sync_client().post(
            "/encode_image", content=png_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
    except Exception as exc:  # noqa: BLE001
        raise VisualServiceError(
            f"视觉服务不可达（{s.visual_service_url}）：{type(exc).__name__}",
            hint=_REMOTE_HINT,
        ) from exc
    if r.status_code != 200:
        raise VisualServiceError(
            f"视觉服务返回 {r.status_code}："
            f"{_remote_detail(r.status_code, _safe_json(r))}",
            hint="服务在跑但模型没就绪或图片编码失败；看 /tmp/pkh_visual.log。",
        )
    body = _safe_json(r)
    vectors = body.get("vectors") if isinstance(body, dict) else None
    if not isinstance(vectors, list) or not vectors:
        raise VisualServiceError(
            "视觉服务返回的响应里没有 vectors",
            hint="服务返回了非预期形状（协议见 DESIGN-HISTORY.md §1）。",
        )
    return vectors


# ---------------- 预热与状态 ----------------


def preload_core() -> None:
    """核心预热：只装 bge-small（约 100MB），不装 colSmol。"""
    get_text_model()
    # 跑一条假查询把图编译进内存，避免首个真实请求慢 1s
    encode_texts(["预热"])


def model_status() -> dict[str, Any]:
    """给 /api/v1/health 用的模型状态。"""
    from ..common.qdrant import VDR_DIM

    with _lock:
        return {
            "text_model": {
                "name": _settings.text_embed_model,
                "dim": _settings.text_embed_dim,
                "loaded": _text_model is not None,
                "load_ms": _load_stats["text_load_ms"],
                "enabled": True,
            },
            "visual_model": {
                "name": _settings.vdr_model,
                # 🔑 报 `VDR_DIM` 而不是写死 128：这两个数字必须与
                # `kb_visual` collection 的维度一致，写死就会出现
                # "健康检查说 128、collection 是 960" 这种没法对账的状态
                # （第一版正是拿裸 AutoModel 的 960 维去写 128 的 collection）。
                "dim": VDR_DIM,
                "loaded": _visual_model is not None,
                "load_ms": _load_stats["visual_load_ms"],
                # API 进程里是 False，这是**预期行为**（D50）
                "enabled": _enabled["visual"],
            },
            "embed_cache": {
                "size": len(_embed_cache),
                "max": _EMBED_CACHE_MAX,
                "hits": _load_stats["embed_cache_hits"],
                "misses": _load_stats["embed_cache_misses"],
            },
        }


def clear_cache() -> None:
    with _lock:
        _embed_cache.clear()
        _load_stats["embed_cache_hits"] = 0
        _load_stats["embed_cache_misses"] = 0


def unload_all() -> None:
    """进程退出前释放。colSmol 有 2.5GB，不释放会让 docker stop 变慢。"""
    global _text_model, _visual_model
    with _lock:
        _text_model = None
        _visual_model = None
        _embed_cache.clear()

