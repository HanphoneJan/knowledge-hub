"""🔑 D178：视觉**写**侧也能走常驻服务 ⇒ worker 不加载 colSmol ⇒ 可多开 worker。

## 为什么

worker 自己加载 colSmol 要 **2.65GB/进程**；SMB 要的 4 个 worker 就是
**+10.6GB**。把写侧改成 HTTP 调那个常驻服务，worker RSS 回落，才谈得上多开。
默认**关**（保持既有行为）；`VISUAL_WRITE_VIA_SERVICE=true` 打开。

## 这一组钉

1. **配置**：两个新键存在且默认保守（`visual_write_via_service=False`）。
2. **服务**：多了 `POST /encode_image`，且用 `encode_images`（不重造编码）。
3. **池**：`encode_image_remote_sync` 打 `/encode_image`，失败抛
   `VisualServiceError`（**绝不吞成空向量**）。
4. **接线**：`handle_visual` 按开关分派；worker 在远端模式下**不** `enable_visual`。
5. **部署**：`dev.sh` 支持 `PKH_WORKERS`（start 循环 / stop 扫全部 pid 文件）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ============================================================
# 1. 配置
# ============================================================


def test_write_via_service_defaults_off():
    from services.common.config import Settings

    s = Settings(_env_file=None)
    assert s.visual_write_via_service is False, (
        "写侧走远端默认应关（改了默认会动既有行为：worker 本地编码）"
    )
    assert s.visual_service_image_timeout_s >= 60, (
        "单页 ~15s，超时给太少会把正常编码判成失败"
    )


# ============================================================
# 2. 视觉服务：/encode_image
# ============================================================


def test_visual_service_exposes_image_endpoint():
    from services.visual_service.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/encode_image" in paths, "视觉服务没有写侧端点 /encode_image（D178）"


def test_image_endpoint_reuses_encode_images():
    """⚠️ 图片端点必须复用 `encode_images`（DPI/dtype/占位那一堆坑只有一处）。"""
    src = (ROOT / "services" / "visual_service" / "main.py").read_text(encoding="utf-8")
    start = src.index("async def encode_image(")
    end = src.index("@app.get(\"/health\"")
    body = src[start:end]
    assert "encode_images(" in body, "图片端点没复用 encode_images —— 会踩 D124 的坑"


def test_image_and_query_gates_are_separate():
    """🔑 图片闸与查询闸分开：一页 15s 不能把查询（预算 2s）堵住。"""
    src = (ROOT / "services" / "visual_service" / "main.py").read_text(encoding="utf-8")
    assert "_MAX_IMAGE_CONCURRENCY" in src
    assert "_img_sem" in src, "图片编码没有独立闸"


# ============================================================
# 3. 池：远端图片编码
# ============================================================


def test_remote_image_encoder_requires_url(monkeypatch):
    from services.retrieval import pool

    class _S:
        visual_service_url = ""

    monkeypatch.setattr(pool, "get_settings", lambda: _S())
    with pytest.raises(pool.VisualServiceError):
        pool.encode_image_remote_sync(b"\x89PNG")


def test_remote_image_encoder_posts_and_returns_vectors(monkeypatch):
    from services.retrieval import pool

    calls: list[tuple[str, str]] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"vectors": [[0.1, 0.2]], "n_tokens": 1, "dim": 2}

    class _Client:
        def post(self, url, content=None, headers=None):
            calls.append((url, headers.get("Content-Type")))
            return _Resp()

    class _S:
        visual_service_url = "http://127.0.0.1:8020"

    monkeypatch.setattr(pool, "get_settings", lambda: _S())
    monkeypatch.setattr(pool, "_get_sync_client", lambda: _Client())

    out = pool.encode_image_remote_sync(b"\x89PNG...")
    assert out == [[0.1, 0.2]]
    assert calls == [("/encode_image", "application/octet-stream")]


def test_remote_image_encoder_raises_on_empty_vectors(monkeypatch):
    """🔑 返回空向量必须**报错**，不能当成成功 —— 否则任务"成功"却召回不到。"""
    from services.retrieval import pool

    class _Resp:
        status_code = 200

        def json(self):
            return {"vectors": []}

    class _Client:
        def post(self, *a, **k):
            return _Resp()

    class _S:
        visual_service_url = "http://127.0.0.1:8020"

    monkeypatch.setattr(pool, "get_settings", lambda: _S())
    monkeypatch.setattr(pool, "_get_sync_client", lambda: _Client())
    with pytest.raises(pool.VisualServiceError):
        pool.encode_image_remote_sync(b"\x89PNG")


# ============================================================
# 4. 接线：handler 分派 + worker 不加载本地模型
# ============================================================


def _func_src(rel: str, marker: str, nxt: str) -> str:
    src = (ROOT / rel).read_text(encoding="utf-8")
    return src[src.index(marker): src.index(nxt)]


def test_handle_visual_branches_on_the_setting():
    body = _func_src(
        "services/worker/handlers/__init__.py",
        "def handle_visual(",
        "def _visual_payload(",
    )
    assert "visual_write_via_service" in body, "handle_visual 没读写侧开关"
    assert "encode_image_remote_sync" in body, "远端分支没有调用远端编码器"
    assert "encode_images(" in body, "本地分支丢了（开关关掉时应当本地编码）"


def test_worker_skips_local_visual_model_in_remote_mode():
    """🔑 远端模式下 worker **不** `enable_visual(True)`（否则白占 2.65GB）。"""
    src = (ROOT / "services" / "worker" / "main.py").read_text(encoding="utf-8")
    assert "visual_write_via_service" in src, (
        "worker 启动逻辑没读写侧开关 —— 远端模式下仍会加载 colSmol"
    )
    assert src.count("enable_visual(True)") == 1, (
        "enable_visual(True) 应只在 else 分支里出现一次"
    )


# ============================================================
# 5. 部署：dev.sh 支持多 worker
# ============================================================


def test_dev_sh_supports_multiple_workers():
    src = (ROOT / "deploy" / "dev.sh").read_text(encoding="utf-8")
    assert "PKH_WORKERS" in src, "dev.sh 不支持多 worker（D178）"
    assert 'for _i in $(seq 1 "$WORKERS")' in src, "start 没有按 WORKERS 循环起 worker"
    # stop 要扫**全部** pid 文件（不止 worker.pid），否则残留 worker 会抢任务
    assert '"$RUN_DIR"/worker.*.pid' in src, "stop_worker 没扫 worker.*.pid"
