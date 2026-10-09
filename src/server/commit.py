"""「应用到工程」：准备 → 确认令牌 → 应用草稿 → 回读校验 → 备份 → 保存。

为什么必须两段式
----------------
保存是**不可逆**的写盘动作。一次性把「校验 + 备份 + 落盘」压进单个请求，用户就
没机会看到「到底会写到哪个绝对路径、备份会落在哪里」。因此拆成：

1. ``prepare``：只做校验与准备，**不碰任何文件**。返回一次性短效令牌、目标绝对路径、
   预计备份路径、以及与本次保存相关的全部告警（含「工程有未保存改动」）。
2. ``commit``：消费令牌，按 prepare 时**冻结的绑定**执行。

令牌绑定与一次性
----------------
令牌绑定 ``baseline_id`` + ``完整草稿``（规范化后的指纹）+ ``保存模式`` + ``目标路径``。
提交时逐项比对：任何一项与 prepare 阶段不一致都判为篡改；已用过的令牌再提交判为复用；
超时判为过期。这样「先给用户看路径」与「真正落盘」之间不存在可被替换的窗口。

失败时一律**不保存**，并**尝试把 Blender 恢复到提交前的基线**
--------------------------------------------------------------
回读不一致、备份失败、保存失败 —— 三种情况都不落盘，且都走同一条失败路径：

1. **不保存**：绝不落到下一步；
2. **回滚基线**：草稿已经写进场景，必须恢复，否则界面显示的参数与 Blender 里的实际取值脱节；
   回滚后**回读校验**，结果放进 ``status.rollback``（``attempted`` / ``verified`` / ``mismatches``）；
3. **明确报出**：回滚没能确认成功时，``status.rollback.code`` 置为 ``ROLLBACK_FAILED``，
   并在错误文案里直说「Blender 里的取值可能仍不是提交前的状态，请人工确认」。

顶层错误码始终保留**原始失败原因**（否则「为什么失败」会被盖掉），回滚结果只作为附加信息。

备份路径**只在 prepare 阶段计算一次**
------------------------------------
展示给用户并绑进确认令牌的那条备份路径，就是 commit 阶段真正使用的那条 ——
不再按当前时间重算。否则跨过时间边界（哪怕只差 1 秒）实际备份位置就会与用户确认过的不一致。
连带的后果是：确认之后目标文件凭空出现或消失，「会不会有备份」的前提已变，一律拒绝并要求重新确认。
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import errors, project_ops, session as session_module, surface_probe
from .binder import BlenderBinder
from .project_ops import MODE_OVERWRITE, MODE_SAVE_AS, SAVE_MODES
from .session import Baseline, PreviewService, validate_draft, verify_values
from .surface import param_values, verify_ops

#: 确认令牌有效期（秒）。刻意很短：prepare 只是为了「让用户看一眼」。
TOKEN_TTL_SECONDS = 120
#: 同时最多保留的待确认令牌数
MAX_PENDING_TOKENS = 32

WARNING_DIRTY = (
    "Blender 里存在未保存的改动：本次备份只包含磁盘上「上一次保存的版本」，"
    "不包含这些未保存改动；这些改动会在本次保存中一并写入工程文件"
    "（其中受本工具管理的曝光/辉光参数会被设成当前草稿值）。"
)
WARNING_TARGET_EXISTS = "目标文件已存在，将被覆盖：保存前会在同目录生成带时间戳的备份。"
WARNING_OVERWRITE = "将覆盖当前已保存的工程文件。保存前会在同目录生成带时间戳的可恢复备份。"


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def draft_fingerprint(coerced: dict[str, Any]) -> str:
    """完整草稿的稳定指纹（用于令牌绑定）。

    绑定的是**规范化之后**的草稿：``1`` 与 ``1.0`` 视为同一份草稿（前端滑块本来就
    可能给出整数），但任何真实的取值改动都会改变指纹。
    """
    canonical = json.dumps(coerced, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class CommitTicket:
    """一次性的保存确认票据。"""

    token: str
    baseline_id: str
    draft_hash: str
    mode: str
    target_path: str
    issued_at: float
    expires_at: float
    expires_at_iso: str
    backup_path: str | None = None
    warnings: list[str] = field(default_factory=list)
    used: bool = False
    #: v4：令牌是否绑定了参数面草稿（旧客户端为 False，行为完全不变）
    surface_bound: bool = False
    #: v4：签发时的结构指纹。结构一变，令牌立即失效。
    structure_hash: str | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def to_public(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "mode": self.mode,
            "target_path": self.target_path,
            "backup_path": self.backup_path,
            "warnings": list(self.warnings),
            "expires_at": self.expires_at_iso,
            "expires_in_seconds": TOKEN_TTL_SECONDS,
            "confirmation_required": self.backup_path is not None,
            "structure_hash": self.structure_hash,
        }


class CommitService:
    """保存流程的唯一权威。"""

    def __init__(self, binder: BlenderBinder, preview: PreviewService) -> None:
        self._binder = binder
        self._preview = preview
        self._tickets: dict[str, CommitTicket] = {}

    # -- 准备 -------------------------------------------------------------
    async def prepare(
        self,
        *,
        mode: Any = MODE_SAVE_AS,
        draft: Any,
        target_path: Any = None,
        confirm: bool = False,
        surface_draft: Any = None,
        structure_hash: Any = None,
    ) -> dict[str, Any]:
        normalised_mode = self._normalise_mode(mode)
        baseline = self._require_baseline()
        coerced = validate_draft(draft, baseline)
        effective, notes = session_module.normalize_values(
            {**baseline.values, **coerced}, baseline.look_map
        )

        # v4 接线：只完成**绑定**，不改旧客户端的任何行为。
        # 一旦客户端带了 surface_draft / structure_hash，就要求 v4 基线存在、
        # 结构指纹一致，并把参数面草稿一并绑进确认令牌 ——
        # 否则「用户确认过的那份草稿」与实际落盘的内容可以不是一回事。
        surface_plan: list[Any] | None = None
        surface_values: dict[str, Any] | None = None
        token_structure: str | None = None
        if surface_draft is not None or structure_hash is not None:
            token_structure = self._require_structure_match(structure_hash)
            surface_plan = self._surface().plan(surface_draft or {})
            surface_values = param_values(surface_plan)

        target, warnings, needs_backup = await self._resolve_target(
            normalised_mode, target_path
        )
        # ★ 备份路径在**整个 prepare 里只算这一次**：既用于「二次确认」里展示给用户的
        #   预计路径，也用于绑进票据。两处各算一次的话，跨过整秒边界就会得到两个名字，
        #   「用户看到的位置」与「实际会写的位置」立刻就对不上了。
        planned_backup = project_ops.backup_path_for(target) if needs_backup else None

        # 覆盖（含「另存为」落到已存在文件）必须二次确认：第一次调用只把
        # 绝对路径与备份路径摊开给用户看，**不签发令牌**。
        if needs_backup and not confirm:
            raise errors.ToonTunerError(
                errors.SAVE_CONFIRM_REQUIRED,
                f"即将覆盖已有文件：{target}。需要看清绝对路径与备份位置后二次确认。",
                details={
                    "mode": normalised_mode,
                    "target_path": str(target),
                    "backup_path": str(planned_backup),
                    "warnings": warnings,
                    "confirm_required": True,
                },
            )

        ticket = self._issue(
            baseline_id=baseline.baseline_id,
            draft_hash=(
                draft_fingerprint(coerced)
                if surface_values is None
                else draft_fingerprint({"l0": coerced, "surface": surface_values})
            ),
            mode=normalised_mode,
            target_path=target,
            backup_path=planned_backup,
            warnings=warnings,
            surface_bound=surface_values is not None,
            structure_hash=token_structure,
        )
        payload = dict(ticket.to_public())
        payload.update(
            {
                "ok": True,
                "baseline_id": baseline.baseline_id,
                "parameter_count": len(effective),
                "surface_parameter_count": len(surface_values or {}),
                "normalized": notes,
                "draft_hash": ticket.draft_hash,
            }
        )
        return payload

    # -- v4 结构绑定 -------------------------------------------------------
    def _surface(self):
        return self._preview.surface

    def _require_structure_match(self, provided: Any) -> str:
        """v4 结构指纹对账（**本地比较**，不调 Blender）。

        真正的「工程侧结构有没有变」需要只读探针，那一步在 ``commit`` 里、
        在**任何写入之前**做。这里只保证「客户端手里的指纹」与基线一致。
        """
        current = self._surface().baseline
        if current is None:
            raise errors.ToonTunerError(
                errors.NO_BASELINE,
                "尚未建立 v4 参数面基线，无法绑定结构指纹。请先建立基线。",
            )
        if provided and str(provided) != current.structure_hash:
            raise errors.ToonTunerError(
                errors.STRUCTURE_CHANGED,
                "客户端持有的结构指纹与当前基线不一致：工程结构已变化，请刷新基线。",
                details={"expected": str(provided), "current": current.structure_hash},
            )
        return current.structure_hash

    async def _assert_surface_fresh(self) -> dict[str, Any]:
        """写入之前的 v4 结构闸门：用只读探针实测一次，绝不只信本地指纹。"""
        verdict = await self._preview.check_surface_structure()
        if verdict.get("identity_missing"):
            raise errors.ToonTunerError(
                errors.IDENTITY_MISSING,
                "受管对象被重命名或删除（本工具不做猜测迁移）："
                + "、".join(verdict["identity_missing"][:4]),
                details={"identity_missing": verdict["identity_missing"]},
            )
        if verdict.get("structure_changed"):
            raise errors.ToonTunerError(
                errors.STRUCTURE_CHANGED,
                "工程结构已变化（节点增删或色标数量）：确认令牌已失效，请刷新基线后重新准备。",
                details={"structure_changed": verdict["structure_changed"]},
            )
        return verdict

    def _normalise_mode(self, mode: Any) -> str:
        if mode is None:
            return MODE_SAVE_AS
        if not isinstance(mode, str) or mode not in SAVE_MODES:
            raise errors.ToonTunerError(
                errors.SAVE_TARGET_INVALID,
                f"未知保存模式：{mode!r}；只能是 {list(SAVE_MODES)}。",
            )
        return mode

    def _require_baseline(self) -> Baseline:
        baseline = self._preview.baseline
        if baseline is None:
            raise errors.ToonTunerError(
                errors.NO_BASELINE, "尚未建立内存基线，无法准备保存。请先建立基线。"
            )
        return baseline

    async def _resolve_target(
        self, mode: str, target_path: Any
    ) -> tuple[Path, list[str], bool]:
        """决定目标路径，并给出告警与「是否需要备份」。

        * ``save_as``：目标来自请求，必须是绝对 ``.blend``；若该文件已存在，
          仍然按「覆盖」对待（要求二次确认 + 备份），绝不允许静默覆盖用户文件。
        * ``overwrite``：目标**只能**来自 Blender 当前工程，客户端不得指定。
        """
        warnings: list[str] = []

        if mode == MODE_OVERWRITE:
            if target_path:
                raise errors.ToonTunerError(
                    errors.SAVE_TARGET_INVALID,
                    "覆盖模式下目标路径由 Blender 当前工程决定，不接受客户端指定。",
                )
            state = await self._read_project_state()
            current = state["filepath"]
            if not current:
                raise errors.ToonTunerError(
                    errors.PROJECT_NOT_SAVED,
                    "当前工程尚未保存到磁盘，无法使用「覆盖当前工程」；请改用「另存为」。",
                    details={"mode": mode},
                )
            target = Path(current)
            if target.suffix.lower() != project_ops.BLEND_SUFFIX:
                raise errors.ToonTunerError(
                    errors.SAVE_TARGET_INVALID,
                    f"当前工程路径不是 {project_ops.BLEND_SUFFIX} 文件：{target.name}",
                )
            warnings.append(WARNING_OVERWRITE)
            if state["is_dirty"]:
                warnings.append(WARNING_DIRTY)
            return target, warnings, target.is_file()

        target = project_ops.validate_save_target(target_path)
        if target.is_file():
            warnings.append(WARNING_TARGET_EXISTS)
        return target, warnings, target.is_file()

    async def _read_project_state(self) -> dict[str, Any]:
        return await self._call(self._binder.read_project)

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        """所有 Blender 访问都经预览服务串行化（同一把锁），避免并发写场景。"""
        return await self._preview.call_binder(fn, *args)

    # -- 令牌 -------------------------------------------------------------
    def _issue(
        self,
        *,
        baseline_id: str,
        draft_hash: str,
        mode: str,
        target_path: Path,
        backup_path: Path | None,
        warnings: list[str],
        surface_bound: bool = False,
        structure_hash: str | None = None,
    ) -> CommitTicket:
        now = time.monotonic()
        ticket = CommitTicket(
            token=secrets.token_urlsafe(24),
            baseline_id=baseline_id,
            draft_hash=draft_hash,
            mode=mode,
            target_path=str(target_path),
            issued_at=now,
            expires_at=now + TOKEN_TTL_SECONDS,
            expires_at_iso=(
                _dt.datetime.now().astimezone() + _dt.timedelta(seconds=TOKEN_TTL_SECONDS)
            ).isoformat(timespec="seconds"),
            backup_path=str(backup_path) if backup_path is not None else None,
            warnings=list(warnings),
            surface_bound=surface_bound,
            structure_hash=structure_hash,
        )
        self._tickets[ticket.token] = ticket
        self._trim()
        return ticket

    def _trim(self) -> None:
        if len(self._tickets) <= MAX_PENDING_TOKENS:
            return
        for token in [key for key, item in self._tickets.items() if item.expired]:
            self._tickets.pop(token, None)
        while len(self._tickets) > MAX_PENDING_TOKENS:
            oldest = min(self._tickets.items(), key=lambda pair: pair[1].issued_at)[0]
            self._tickets.pop(oldest, None)

    def consume(
        self,
        *,
        token: Any,
        mode: Any,
        draft: Any,
        target_path: Any = None,
        surface_draft: Any = None,
        structure_hash: Any = None,
    ) -> CommitTicket:
        """校验并**消费**令牌（一次性：校验通过即刻作废，防止重复落盘）。"""
        if not isinstance(token, str) or not token:
            raise errors.ToonTunerError(errors.COMMIT_TOKEN_INVALID, "缺少确认令牌。")

        ticket = self._tickets.get(token)
        if ticket is None:
            raise errors.ToonTunerError(errors.COMMIT_TOKEN_INVALID, "确认令牌不存在。")
        if ticket.used:
            raise errors.ToonTunerError(
                errors.COMMIT_TOKEN_USED, "确认令牌已被使用过，不能复用。"
            )
        if ticket.expired:
            self._tickets.pop(token, None)
            raise errors.ToonTunerError(
                errors.COMMIT_TOKEN_EXPIRED, "确认令牌已过期，请重新准备。"
            )

        baseline = self._require_baseline()
        if not hmac.compare_digest(baseline.baseline_id, ticket.baseline_id):
            raise errors.ToonTunerError(
                errors.BASELINE_STALE,
                "基线已变化（被刷新或重新采集），确认令牌失效。",
                details={
                    "token_baseline_id": ticket.baseline_id,
                    "current_baseline_id": baseline.baseline_id,
                },
            )

        normalised_mode = self._normalise_mode(mode)
        if normalised_mode != ticket.mode:
            raise errors.ToonTunerError(
                errors.COMMIT_TOKEN_MISMATCH,
                f"保存模式与准备阶段不一致：{normalised_mode!r} ≠ {ticket.mode!r}。",
            )

        supplied_target = self._canonical_target(target_path, ticket.target_path)
        if supplied_target != ticket.target_path:
            raise errors.ToonTunerError(
                errors.COMMIT_TOKEN_MISMATCH,
                "目标路径与准备阶段不一致。",
                details={"expected": ticket.target_path, "received": supplied_target},
            )

        # v4：结构指纹 + 参数面草稿都必须与准备阶段完全一致。
        if ticket.surface_bound:
            self._require_structure_match(
                ticket.structure_hash if structure_hash is None else structure_hash
            )
            current_surface: dict[str, Any] | None = param_values(
                self._surface().plan(surface_draft or {})
            )
        elif surface_draft:
            raise errors.ToonTunerError(
                errors.COMMIT_TOKEN_MISMATCH,
                "准备阶段未绑定参数面草稿，提交时却带了 surface_draft。请重新准备。",
            )
        else:
            current_surface = None

        coerced = validate_draft(draft, baseline)
        expected_hash = (
            draft_fingerprint(coerced)
            if current_surface is None
            else draft_fingerprint({"l0": coerced, "surface": current_surface})
        )
        if not hmac.compare_digest(expected_hash, ticket.draft_hash):
            raise errors.ToonTunerError(
                errors.COMMIT_TOKEN_MISMATCH,
                "草稿与准备阶段不一致（令牌绑定了完整草稿，不允许中途改动）。",
            )

        ticket.used = True
        return ticket

    @staticmethod
    def _canonical_target(supplied: Any, ticket_target: str) -> str:
        """把提交里给的目标路径规范到与 prepare 相同的形态再比较。

        覆盖模式下客户端可以不传目标（以票据为准）；传了就必须一致。
        """
        if supplied is None or (isinstance(supplied, str) and not supplied.strip()):
            return ticket_target
        try:
            return str(project_ops.validate_save_target(supplied))
        except errors.ToonTunerError:
            # 不把原始输入回显给调用方，只用一个不可能等于票据路径的哨兵值
            return ""

    # -- 失败处理 ---------------------------------------------------------
    async def _rollback_to_baseline(
        self, surface_ops: list[Any] | None = None
    ) -> dict[str, Any]:
        """把 Blender 恢复到提交前的基线取值，并**回读校验**（L0 与 Cel 两部分分开报）。

        只在失败路径调用：草稿已经写进场景，不恢复的话界面显示的参数与 Blender 里的
        实际取值就会脱节。恢复之后必须回读 —— 否则「已恢复」只是一句没有依据的自述。

        ``code`` 为 ``ROLLBACK_FAILED`` 表示「尝试过但没能确认恢复」，调用方必须把它
        明确报给用户，而不是含糊地说一句「已回滚」。
        """
        l0 = await self._restore_l0()
        cel = await self._restore_cel(surface_ops)
        verified = bool(l0["verified"]) and bool(cel["verified"])
        return {
            "attempted": bool(l0["attempted"] or cel["attempted"]),
            "verified": verified,
            "mismatches": list(l0["mismatches"]) + list(cel["mismatches"]),
            "code": None if verified else errors.ROLLBACK_FAILED,
            "error": l0.get("error") or cel.get("error"),
            "reason": l0.get("reason") or cel.get("reason"),
            "l0": l0,
            "cel": cel,
        }

    async def _restore_l0(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempted": False,
            "verified": False,
            "mismatches": [],
            "code": None,
            "error": None,
            "reason": None,
        }
        if self._preview.baseline is None:
            out["code"] = errors.ROLLBACK_FAILED
            out["reason"] = "基线已丢失，无法恢复到提交前的状态。"
            out["error"] = {
                "code": errors.NO_BASELINE,
                "message": "基线已丢失，无法恢复到提交前的状态。",
            }
            return out

        out["attempted"] = True
        try:
            outcome = await self._preview.restore_baseline()
        except errors.ToonTunerError as exc:
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = exc.to_payload()["error"]
            return out
        except Exception as exc:  # noqa: BLE001 - 回滚自身不得再抛，否则会盖掉原始失败原因
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = {
                "code": errors.INTERNAL_ERROR,
                "message": f"{type(exc).__name__}: {exc}",
            }
            return out

        out["verified"] = bool(outcome.get("verified"))
        out["mismatches"] = list(outcome.get("mismatches") or [])
        if not out["verified"]:
            out["code"] = errors.ROLLBACK_FAILED
        return out

    async def _restore_cel(self, surface_ops: list[Any] | None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "attempted": False,
            "verified": False,
            "mismatches": [],
            "code": None,
            "error": None,
            "reason": None,
        }
        if not surface_ops:
            # 「无事可做」不等于「没恢复成」：整体 verified 取两者的与，
            # 这里若留 False，一次纯 L0 保存的回滚就会被误报成失败。
            out["verified"] = True
            out["applicable"] = False
            out["reason"] = "本次保存没有 Cel 写入，无需恢复。"
            return out
        out["attempted"] = True
        try:
            payload = await self._call(
                self._binder.apply_surface_ops, self._surface().baseline_ops()
            )
        except errors.ToonTunerError as exc:
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = exc.to_payload()["error"]
            return out
        except Exception as exc:  # noqa: BLE001 - 同上
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = {
                "code": errors.INTERNAL_ERROR,
                "message": f"{type(exc).__name__}: {exc}",
            }
            return out

        baseline = self._surface().baseline
        if baseline is None:  # pragma: no cover - 上面 baseline_ops 已隐含存在
            out["code"] = errors.ROLLBACK_FAILED
            out["error"] = {
                "code": errors.NO_BASELINE,
                "message": "v4 基线已丢失，无法恢复 Cel 取值。",
            }
            return out
        verified, mismatches = verify_ops(baseline.baseline_ops, payload.get("values") or {})
        out["verified"] = verified
        out["mismatches"] = mismatches
        if not verified:
            out["code"] = errors.ROLLBACK_FAILED
        return out

    @staticmethod
    def _rollback_suffix(rollback: dict[str, Any]) -> str:
        """把回滚结果说清楚：成功就断言成功，失败就直说没恢复成。"""
        if rollback.get("verified"):
            return "已恢复到提交前的基线，并回读校验通过。"
        if rollback.get("attempted"):
            return (
                "**恢复到基线后回读不一致或恢复失败**（rollback.code = "
                f"{rollback.get('code')}）：Blender 里的取值可能仍不是提交前的状态，请人工确认。"
            )
        return "**未能恢复基线**：Blender 里的取值可能仍不是提交前的状态，请人工确认。"

    async def _abort(
        self,
        code: str,
        message: str,
        *,
        status: dict[str, Any],
        steps: list[str],
        notes: list[dict[str, Any]] | None = None,
        scene_touched: bool = True,
        surface_ops: list[Any] | None = None,
    ) -> errors.ToonTunerError:
        """构造失败响应：尝试回滚（仅当草稿确实写进过场景），并把四态 + 回滚结果带上。

        返回异常对象而不是直接抛，调用方写 ``raise await self._abort(...)``：
        这样「一定会抛」在调用点可见，也便于把内层错误的 details 合并进来。
        """
        if scene_touched:
            rollback = await self._rollback_to_baseline(surface_ops)
            suffix = self._rollback_suffix(rollback)
        else:
            rollback = {
                "attempted": False,
                "verified": False,
                "mismatches": [],
                "code": None,
                "error": None,
                "reason": "尚未写入场景，无需回滚。",
                "l0": {
                    "attempted": False,
                    "verified": False,
                    "mismatches": [],
                    "code": None,
                    "error": None,
                    "reason": "尚未写入场景，无需回滚。",
                },
                "cel": {
                    "attempted": False,
                    "verified": False,
                    "mismatches": [],
                    "code": None,
                    "error": None,
                    "reason": "尚未写入场景，无需回滚。",
                },
            }
            suffix = "（尚未写入场景，无需回滚。）"

        status["rollback"] = rollback
        details: dict[str, Any] = {"status": status, "steps": list(steps)}
        if notes:
            details["normalized"] = notes
        return errors.ToonTunerError(code, f"{message} {suffix}", details=details)

    # -- 提交 -------------------------------------------------------------
    async def commit(
        self,
        *,
        token: Any,
        mode: Any,
        draft: Any,
        target_path: Any = None,
        surface_draft: Any = None,
        structure_hash: Any = None,
    ) -> dict[str, Any]:
        """消费令牌并执行保存。任一步失败都**不保存**，并回完整的四态状态块。"""
        ticket = self.consume(
            token=token,
            mode=mode,
            draft=draft,
            target_path=target_path,
            surface_draft=surface_draft,
            structure_hash=structure_hash,
        )

        baseline = self._require_baseline()
        coerced = validate_draft(draft, baseline)
        expected, notes = session_module.normalize_values(
            {**baseline.values, **coerced}, baseline.look_map
        )
        # v4：票据绑定的参数面草稿在这里重新编译成计划（与 prepare 用的是同一份基线，
        # 因此结果与 prepare 阶段逐字节一致；不一致会在 consume 里就被判篡改）。
        surface_ops: list[Any] | None = (
            self._surface().plan(surface_draft or {}) if ticket.surface_bound else None
        )
        if surface_ops is not None and not surface_ops:
            surface_ops = None
        target = Path(ticket.target_path)
        # ★ 必须用**票据里已确认的那条**备份路径，不重新计算：
        #   跨过时间边界（哪怕只差 1 秒）算出来的名字就不同，备份就会落到
        #   与用户确认过的位置不一致的地方。
        backup_path = Path(ticket.backup_path) if ticket.backup_path else None

        status: dict[str, Any] = {
            "applied": False,
            "readback_verified": False,
            "mismatches": [],
            "backup": {
                "required": backup_path is not None,
                "created": False,
                "path": None,
                "path_confirmed": str(backup_path) if backup_path is not None else None,
            },
            "rollback": {
                "attempted": False,
                "verified": False,
                "mismatches": [],
                "code": None,
                "error": None,
                "reason": None,
            },
            "saved": False,
            "target_path": ticket.target_path,
            "mode": ticket.mode,
            "draft_preserved": True,
            "applied_surface": {},
            "surface_readback_verified": False,
            "surface_mismatches": [],
        }
        steps: list[str] = []

        # 0) 复核「确认时」的前提是否仍然成立。这两个前提决定要不要备份，
        #    确认之后一旦变了，用户确认过的「会不会有备份」就与实际不符，必须重新确认。
        steps.append("复核确认时的前提")
        if backup_path is not None and not target.is_file():
            raise await self._abort(
                errors.BACKUP_FAILED,
                f"确认之后原文件已消失（{target.name}），无法按已确认的路径生成备份，已拒绝继续。",
                status=status,
                steps=steps,
                notes=notes,
                scene_touched=False,
            )
        if backup_path is None and target.is_file():
            raise await self._abort(
                errors.SAVE_CONFIRM_REQUIRED,
                f"确认之后目标文件才出现（{target.name}）；覆盖它需要重新确认（会先生成备份）。",
                status=status,
                steps=steps,
                notes=notes,
                scene_touched=False,
            )

        # 1) 应用完整草稿 → 2) 回读校验 → 3) 备份 → 4) 保存
        # 这四步共用一条失败路径：草稿已经写进场景，因此失败时必须尝试回滚基线。
        try:
            steps.append("应用完整草稿")
            applied_payload = await self._call(self._binder.apply_values, dict(expected))
            status["applied"] = True
            status["applied_values"] = session_module.values_from_read(applied_payload)

            steps.append("回读校验")
            verified, mismatches = verify_values(expected, applied_payload)
            status["readback_verified"] = verified
            status["mismatches"] = mismatches
            if not verified:
                raise errors.ToonTunerError(
                    errors.APPLY_VERIFY_FAILED,
                    "草稿写入后回读不一致，已中止保存（磁盘上的工程文件未被写入）。",
                )

            if surface_ops:
                # v4 结构闸门：在写 Cel 之前用只读探针实测一次。本地指纹对账证明不了
                # 「工程这一刻的结构没变」，而结构一变写下去就是错的。
                steps.append("复核 v4 结构")
                await self._assert_surface_fresh()

                steps.append("应用 Cel 草稿")
                applied_surface = await self._call(
                    self._binder.apply_surface_ops, surface_ops
                )
                sur_verified, sur_mismatches = verify_ops(
                    surface_ops, applied_surface.get("values") or {}
                )
                status["applied_surface"] = param_values(surface_ops)
                status["surface_readback_verified"] = sur_verified
                status["surface_mismatches"] = sur_mismatches
                if not sur_verified:
                    raise errors.ToonTunerError(
                        errors.APPLY_VERIFY_FAILED,
                        "Cel 草稿写入后回读不一致，已中止保存（磁盘上的工程文件未被写入）。",
                    )

            if backup_path is not None:
                steps.append("生成备份")
                created = await self._call(project_ops.make_backup, target, backup_path)
                status["backup"]["created"] = True
                status["backup"]["path"] = str(created)
            else:
                steps.append("无需备份")

            steps.append("保存工程")
            save_payload = await self._call(self._binder.save_project, str(target), ticket.mode)
        except errors.ToonTunerError as exc:
            abort = await self._abort(
                exc.code,
                exc.message,
                status=status,
                steps=steps,
                notes=notes,
                surface_ops=surface_ops,
            )
            # 保留内层细节（例如 target_name / failure_type / stage），但不覆盖
            # 我们刚写好的 status / steps / normalized。
            for key, value in (exc.details or {}).items():
                abort.details.setdefault(key, value)
            raise abort

        status["save"] = {
            "path_after": save_payload.get("path_after"),
            "path_matches": bool(save_payload.get("path_matches")),
            "file_exists": bool(save_payload.get("file_exists")),
            "is_dirty_after": bool(save_payload.get("is_dirty_after")),
        }
        if not (save_payload.get("path_matches") and save_payload.get("file_exists")):
            raise await self._abort(
                errors.SAVE_FAILED,
                "Blender 报告保存完成，但回读的工程路径/文件与目标不一致；"
                "磁盘上的实际写入位置无法确认，请到 Blender 里核对。",
                status=status,
                steps=steps,
                notes=notes,
                surface_ops=surface_ops,
            )

        status["saved"] = True
        status["rollback"] = {
            "attempted": False,
            "verified": False,
            "mismatches": [],
            "code": None,
            "error": None,
            "reason": "保存成功，未触发回滚。",
            "l0": {
                "attempted": False,
                "verified": False,
                "mismatches": [],
                "code": None,
                "error": None,
                "reason": "保存成功，未触发回滚。",
            },
            "cel": {
                "attempted": False,
                "verified": False,
                "mismatches": [],
                "code": None,
                "error": None,
                "reason": "保存成功，未触发回滚。",
            },
        }
        steps.append("刷新基线")

        # 5) 文件内容已经就是草稿，必须重新采集基线；否则后续预览会「恢复到旧基线」，
        #    与磁盘上的工程不一致。
        baseline_refreshed = False
        refreshed: dict[str, Any] | None = None
        try:
            refreshed = await self._preview.capture_baseline()
            baseline_refreshed = True
        except errors.ToonTunerError as exc:  # pragma: no cover - 保存已成功，刷新失败仅告警
            status["baseline_refresh_error"] = exc.to_payload()["error"]

        return {
            "ok": True,
            "mode": ticket.mode,
            "target_path": ticket.target_path,
            "backup_path": status["backup"]["path"] or status["backup"]["path_confirmed"],
            "saved": True,
            "status": status,
            "steps": steps,
            "normalized": notes,
            "baseline_id": (refreshed or {}).get("baseline_id") if refreshed else None,
            "baseline_refreshed": baseline_refreshed,
            "saved_at": _now_iso(),
            "warnings": list(ticket.warnings),
        }
