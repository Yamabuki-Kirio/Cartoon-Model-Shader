"""MVP-03：CI 配置守卫。

``.github/workflows/test.yml`` 必须满足需求里写死的那几条 ——
Windows Runner、Python 3.11/3.12、只装 requirements.txt、只跑 pytest、不上传产物。

之所以单独成文件：工作流文件本身受 GitHub 的 ``workflow`` scope 约束，
只有持有该权限的凭据才能推送。把守卫与工作流绑在一起提交，可以让
「不含工作流的提交」保持自洽（此时守卫也一并不存在），避免出现
「测试要求工作流存在、而工作流还没推上去」的自相矛盾状态。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


def test_ci_workflow_exists() -> None:
    assert WORKFLOW.is_file(), "缺 .github/workflows/test.yml"


def test_ci_runs_on_windows_with_supported_pythons() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "runs-on: windows-latest" in text
    assert '"3.11"' in text
    assert '"3.12"' in text


def test_ci_installs_requirements_and_runs_pytest() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pip install -r requirements.txt" in text
    assert "pytest" in text


def test_ci_triggers_on_pull_request_and_main_push() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request:" in text
    assert "push:" in text
    assert "- main" in text


def test_ci_does_not_upload_artifacts_or_use_secrets() -> None:
    """不进 GitHub Runner 的东西：模型、预览图、日志与任何凭据。"""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "upload-artifact" not in text
    assert "actions/cache" not in text
    assert "${{ secrets." not in text
    assert "ghp_" not in text


def test_ci_does_not_ship_engine_assets() -> None:
    """工作流不得引用模型 / 贴图 / 预览图 / .blend 之类工程素材。"""
    text = WORKFLOW.read_text(encoding="utf-8").lower()
    for token in (".blend", ".png", ".jpg", "reference/", "previews"):
        assert token not in text, f"工作流不应出现 {token}"
