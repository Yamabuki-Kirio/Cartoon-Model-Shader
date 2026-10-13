"""`.blend` Web 入口修复的守卫测试（2026-10-14）。

背景：Web 模型注册表收录 `.blend`，但入口脚本（开始渲染.py）过去只认 `.pmx`——
locate() 走查不到、驱动只能收 --pmx、预检解析器读不了 .blend，用户在 Web 里
选到 .blend 模型也必然失败。本文件守住修复后的关键连接点：

* 入口脚本：``model_kind`` / ``locate`` 扩展名、``render()`` 的 --blend/--resolve 分支、
  ``write_job`` 的 blend 目标键；
* 确认服务：自动续跑 argv 与 job 校验对 blend 目标放行；
* 适配层：显式传模型路径（--pmx/--blend 二选一）+ 整文件 sha256 口径。

真机口径由 2026-10-14 凌晨 91.blend 三连跑实证：strict 无映射 REJECTED、
diagnostic DIAGNOSTIC、strict + 侧车映射 SUCCESS（--blend 自身 --resolve 自身）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "render_pipeline"
ENTRY = PIPELINE / "开始渲染.py"
CONFIRM = PIPELINE / "_tools" / "confirm_server.py"
ADAPTER = ROOT / "src" / "server" / "render_pipeline_adapter.py"


def _load_entry():
    spec = importlib.util.spec_from_file_location("toon_entry_blend", ENTRY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- 入口脚本：单元级 ---------------------------------------------------------


def test_model_kind_classifies_extensions() -> None:
    mod = _load_entry()
    assert mod.model_kind("a/blend/scene.blend") == "blend"
    assert mod.model_kind("a/scene.BLEND") == "blend"
    assert mod.model_kind("a/model.pmx") == "pmx"
    assert mod.model_kind("a/model.pmd") == "unsupported"
    assert mod.model_kind("a/model.x") == "unsupported"


def test_locate_walks_blend_files(tmp_path: Path) -> None:
    """locate() 必须能找到 .blend —— 与 Web 注册表扩展名对齐。"""
    mod = _load_entry()
    (tmp_path / "scene.blend").write_bytes(b"BLENDER-scene")
    mod.ROOT = str(tmp_path)
    fp = _sha256(tmp_path / "scene.blend")
    found = mod.locate(fingerprint=fp)
    assert found == str(tmp_path / "scene.blend")
    # 按文件名反查也要能命中
    assert mod.locate(name="scene.blend") == str(tmp_path / "scene.blend")


def test_locate_accepts_explicit_blend_path(tmp_path: Path) -> None:
    mod = _load_entry()
    p = tmp_path / "explicit.blend"
    p.write_bytes(b"BLENDER-explicit")
    assert mod.locate(blend=str(p)) == str(p)
    # 不存在的路径必须返回 None（而不是把调用方带进沟里）
    assert mod.locate(blend=str(tmp_path / "nope.blend")) is None


def test_render_uses_blend_flags_for_blend_model(tmp_path: Path, monkeypatch) -> None:
    """.blend 模型：驱动 argv 必须走 --blend 且 --resolve 指向自身（91.blend 实证口径）。"""
    mod = _load_entry()
    model = tmp_path / "scene.blend"
    model.write_bytes(b"BLENDER-scene")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mod.LOCK_DIR = str(tmp_path / ".locks")
    mod.BLENDER = "blender"
    mod.SOURCE_BLEND = str(tmp_path / "some_other_source.blend")  # blend 模式不得用它

    captured: dict[str, list[str]] = {}

    def fake_launch(argv, log_path):
        captured["argv"] = argv
        # 立即写完成标记，让 render() 的轮询立刻结束
        with open(argv[argv.index("--done") + 1], "w", encoding="utf-8") as f:
            f.write("SUCCESS\n")
        return {"__raw__": '@@LAUNCH@@{"pid": 1}'}

    monkeypatch.setattr(mod, "launch_blender", fake_launch)
    rc, outcome = mod.render(str(model), str(out_dir), "faithful", False, str(tmp_path / "maps"))
    assert rc == 0
    assert outcome["status"] == "SUCCESS"
    argv = captured["argv"]
    assert "--blend" in argv and "--pmx" not in argv
    assert argv[argv.index("--blend") + 1] == str(model)
    assert argv[argv.index("--resolve") + 1] == str(model)
    # 共同开关不受影响
    assert "--material-policy" in argv and "strict" in argv
    assert "--lighting" in argv and "volume_fog" in argv
    assert "--maps-dir" in argv


def test_render_uses_pmx_flags_for_pmx_model(tmp_path: Path, monkeypatch) -> None:
    """PMX 模型：回归保护 —— 仍走 --pmx + 源工程，不被 blend 分支带偏。"""
    mod = _load_entry()
    model = tmp_path / "m.pmx"
    model.write_bytes(b"PMX ")
    source = tmp_path / "source.blend"
    source.write_bytes(b"BLENDER-source")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    mod.LOCK_DIR = str(tmp_path / ".locks")
    mod.BLENDER = "blender"
    mod.SOURCE_BLEND = str(source)

    captured: dict[str, list[str]] = {}

    def fake_launch(argv, log_path):
        captured["argv"] = argv
        with open(argv[argv.index("--done") + 1], "w", encoding="utf-8") as f:
            f.write("SUCCESS\n")
        return {"__raw__": '@@LAUNCH@@{"pid": 1}'}

    monkeypatch.setattr(mod, "launch_blender", fake_launch)
    rc, _outcome = mod.render(str(model), str(out_dir), "faithful", False, str(tmp_path / "maps"))
    assert rc == 0
    argv = captured["argv"]
    assert "--pmx" in argv and "--blend" not in argv
    assert argv[argv.index("--pmx") + 1] == str(model)
    assert argv[argv.index("--resolve") + 1] == str(source)


def test_write_job_records_blend_target(tmp_path: Path) -> None:
    mod = _load_entry()
    model = tmp_path / "scene.blend"
    model.write_bytes(b"BLENDER-scene")
    mod.JOB_DIR = str(tmp_path / "jobs")
    mod.BLENDER = "blender"
    mod.SOURCE_BLEND = ""
    job_path = mod.write_job(str(model), str(tmp_path / "out"), "enhanced", False,
                             str(tmp_path / "maps"), lighting="legacy", fog_density=0.03)
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    assert job["model_kind"] == "blend"
    tgt = job["targets"][0]
    assert tgt["blend"] == str(model)
    assert tgt["pmx"] is None
    assert tgt["fingerprint"] == _sha256(model)
    # 步骤 1 的灯光/辉光字段不受影响
    assert job["lighting"] == "legacy"
    assert job["fog_density"] == 0.03


def _run_main_isolating_stdout(mod) -> int:
    """跑 main() 并把它对 sys.stdout 的重绑定关在测试内。

    入口脚本 main() 会 ``sys.stdout = io.TextIOWrapper(sys.stdout.buffer, …)``。
    直接丢引用的话，TextIOWrapper 析构会**关闭底层 buffer**（pytest 的捕获临时文件），
    后续测试的捕获装置全部报「I/O operation on closed file」。必须先 detach 再丢。
    """
    import sys

    saved = sys.stdout
    try:
        rc = mod.main()
    finally:
        cur = sys.stdout
        sys.stdout = saved
        if cur is not saved:
            try:
                cur.detach()
            except Exception:
                pass
    return rc


def test_main_routes_blend_model_to_direct_render(tmp_path: Path, monkeypatch) -> None:
    """main() 对 .blend：跳过 PMX 预检，直接进 render()（不碰 socket/确认页）。"""
    mod = _load_entry()
    model = tmp_path / "scene.blend"
    model.write_bytes(b"BLENDER-scene")
    calls: dict[str, object] = {}

    def fake_render(m, out_dir, mode, auto_frame, maps_dir=None,
                    lighting="volume_fog", fog_density=0.018, glow=None):
        calls["model"] = m
        calls["out_dir"] = out_dir
        calls["lighting"] = lighting
        calls["fog_density"] = fog_density
        return 0, {"status": "SUCCESS", "token": "SUCCESS"}

    monkeypatch.setattr(mod, "render", fake_render)
    monkeypatch.setattr(mod, "channel_ready", lambda: True)
    monkeypatch.setattr(
        mod.sys, "argv",
        ["开始渲染.py", "--blend", str(model),
         "--blender", str(model),  # 任意已存在文件即可通过存在性校验
         "--runtime-dir", str(tmp_path / "rt"),
         "--out", str(tmp_path / "out"),
         "--mode", "enhanced"],
    )
    rc = _run_main_isolating_stdout(mod)
    assert rc == 0
    assert calls["model"] == str(model)
    assert calls["out_dir"] == str(tmp_path / "out")
    assert calls["lighting"] == "volume_fog"


def test_main_rejects_unsupported_extension(tmp_path: Path, monkeypatch) -> None:
    """.pmd/.x 暂不支持：必须给明确错误，而不是「找不到模型」或预检崩溃。"""
    mod = _load_entry()
    model = tmp_path / "m.pmd"
    model.write_bytes(b"PMX ")
    monkeypatch.setattr(mod, "channel_ready", lambda: True)
    monkeypatch.setattr(
        mod.sys, "argv",
        ["开始渲染.py", "--blend", str(model),
         "--blender", str(model),
         "--runtime-dir", str(tmp_path / "rt"),
         "--out", str(tmp_path / "out")],
    )
    assert _run_main_isolating_stdout(mod) == 2


# -- 源码级契约（与 test_render_lighting_port.py 同一风格） -------------------


def test_entry_source_supports_blend_flow() -> None:
    text = ENTRY.read_text(encoding="utf-8")
    assert 'ap.add_argument("--blend")' in text
    # 定位与种类判定
    assert 'MODEL_EXTS = (".pmx", ".pmd", ".blend", ".x")' in text
    assert "def model_kind(path):" in text
    assert "def locate(fingerprint=None, pmx=None, blend=None, name=None):" in text
    # 渲染 argv 的 blend 分支
    assert 'argv += ["--blend", model, "--resolve", model]' in text
    # job 文件的 blend 目标键
    assert '"blend": model if kind == "blend" else None,' in text
    assert '"model_kind": kind,' in text


def test_confirm_server_supports_blend_targets() -> None:
    text = CONFIRM.read_text(encoding="utf-8")
    assert 'blend = tgt.get("blend")' in text
    assert 'argv += ["--blend", blend, "--resolve", blend]' in text
    assert 'argv += ["--pmx", tgt["pmx"], "--resolve", job.get("source_blend") or SOURCE_BLEND]' in text
    # job 校验：blend 目标必须能过（内容指纹 + 根目录约束不变）
    assert "model = blend or pmx" in text
    assert "if sha256_of(model) != fp:" in text


def test_adapter_passes_explicit_path_and_pipeline_fingerprint() -> None:
    text = ADAPTER.read_text(encoding="utf-8")
    # 显式传路径：不再用注册表指纹反查
    assert '"--blend" if kind_blend else "--pmx",' in text
    assert 'str(model_path),' in text
    assert '"--fingerprint",' not in text
    # 确认清单用整文件 sha256（管线口径），不是注册表指纹
    assert "def _sha256_file(" in text
    assert "def _pipeline_fingerprint(" in text
    assert "_sha256_file(path)" in text
