"""``view_transform`` → 合法 ``look`` 的**依赖枚举**探测、迁移与校验。

为什么必须单独做这一层
----------------------
``scene.view_settings.look`` 不是独立参数，而是**依赖 ``view_transform`` 的枚举**。
同一个字符串在 AgX 下合法，换到 Standard 就会直接抛::

    TypeError: bpy_struct: item.attr = val: enum "AgX - Punchy"
               not found in ('None', 'Very High Contrast', 'High Contrast', ...)

而它的两个「看起来能用」的来源都不能单独采信：

* ``bl_rna.properties['look'].enum_items`` —— 无 UI 上下文里只返回 ``NONE``
  （Blender 5.2.1 实测 ``items_count == 1``），**完全不可用**；
* ``PyOpenColorIO.getLookNames()`` —— 那是 OCIO 配置的**全局** look 名单，**不是**
  当前 ``view_transform`` 下 Blender 接受的枚举。旧实现直接拿它当候选，
  于是把 ``"AgX - High Contrast"`` 写进了只接受通用档位的视图，炸成
  ``BLENDER_SCRIPT_ERROR``。

所以合法性只能问 Blender 本人。本模块的探测流程：

1. 记录原 ``view_transform`` / ``look``；
2. 切到目标 ``view_transform``；
3. 写入一个**必然非法**的哨兵值，触发 Blender 的枚举报错，**从报错文本里解析出
   权威允许列表** —— 那是 Blender RNA 自己的输出，比逐项赋值探测既快又准；
4. 解析失败再退化为逐个赋值探测（同样必须完整恢复）；
5. ``finally`` 恢复原 ``view_transform`` / ``look``。

铁律
----
**写进 Blender 的只能是 ``value``（真实 identifier），永远不能是 ``label``。**
两者只在 AgX / False Color 这类「族前缀」视图下才不同，见 ``look_label()``。
"""

from __future__ import annotations

import json
from typing import Any

from . import errors

#: 本模块结构化输出的标记（与 framing.py 同样采用「每模块独立标记」的既有约定）
LOOK_MARKER = "__TOON_LOOK_JSON__"

#: 「 None 」在 Blender 里的真实 identifier 就是字符串 None（不是 Python 的 None）
NONE_LOOK = "None"

#: Blender 为所有非族前缀视图提供的**通用**对比度档位（OCIO 里 process_space = Filmic Log）
GENERIC_LOOKS: tuple[str, ...] = (
    "Very High Contrast",
    "High Contrast",
    "Medium High Contrast",
    "Medium Contrast",
    "Medium Low Contrast",
    "Low Contrast",
    "Very Low Contrast",
)

#: Blender 界面会给这些视图的通用档位加上「族前缀」显示（value 仍是通用 identifier）。
#: 仅用于**组合显示标签**，绝不参与 value 的构造。
COMPOSED_LABEL_PREFIXES: tuple[str, ...] = ("AgX", "False Color")

#: 非法 look 哨兵；只要 Blender 拒绝它并报出允许列表即可
SENTINEL_LOOK = "__TOON_TUNER_INVALID_LOOK__"

_SEPARATOR = " - "


# =============================================================================
#  枚举报错解析（唯一实现：宿主与 Blender 侧同源）
# =============================================================================

#: ``_parse_enum_error_text`` 的**唯一**源码。宿主侧用 ``exec`` 取出可调用对象，
#: Blender 侧把同一段源码内嵌进下发代码 —— 两边共用一份实现，
#: 从根上杜绝「主机解析得住、Blender 侧解析不住」这类漂移。
_PARSE_ENUM_ERROR_TEXT_SRC = '''
def _parse_enum_error_text(text):
    """从 Blender 的枚举报错文本里取出权威允许列表；解析不出来返回 None。

    实测格式（Blender 5.2.1）::

        bpy_struct: item.attr = val: enum "X" not found in ('None', 'High Contrast', ...)

    先按 ``ast.literal_eval`` 严格解析元组文本，失败再退化为逗号切分，
    这样既吃得下带转义/嵌套的标识符，也不会因为一个畸形字符串整段放弃。
    """
    marker = "not found in ("
    pos = text.find(marker)
    if pos < 0:
        return None
    tail = text[pos + len(marker):]
    end = tail.rfind(")")
    if end < 0:
        return None
    body = tail[:end]
    try:
        import ast
        parsed = ast.literal_eval("(" + body + ")")
        if isinstance(parsed, (tuple, list)):
            return [str(v) for v in parsed]
    except Exception:
        pass
    parts = []
    for chunk in body.split(","):
        cleaned = chunk.strip().strip("'").strip('"').strip()
        if cleaned:
            parts.append(cleaned)
    return parts or None
'''

#: 把上面那段源码在宿主进程里「落地」成一个真函数。
#: 这是唯一允许出现 ``exec`` 的地方：它执行的是本模块内写死的常量，
#: 不含任何用户输入，因此不构成注入面。
_host_namespace: dict[str, Any] = {}
exec(_PARSE_ENUM_ERROR_TEXT_SRC, _host_namespace)  # noqa: S102 - 源码为本模块常量
_parse_enum_error_text = _host_namespace["_parse_enum_error_text"]


# =============================================================================
#  Blender 侧内嵌助手（由本模块生成，注入到下发代码里）
# =============================================================================

LOOK_HELPERS = '''

def _look_candidates():
    # 候选全集：OCIO 全局 look 名 + None。
    # 注意：**只作探测原料**，绝不作为合法性依据（这正是旧实现的 bug）。
    names = ["None"]
    try:
        import PyOpenColorIO as ocio
        cfg = ocio.GetCurrentConfig()
        for item in cfg.getLookNames():
            text = str(item)
            if text not in names:
                names.append(text)
    except Exception:
        pass
    return names
'''

#: 会话层的 ``_parse_enum_error`` 只是同一份源码的薄封装：Blender 侧拿到的是异常对象，
#: 宿主侧拿到的是文本。两者共用 ``_PARSE_ENUM_ERROR_TEXT_SRC``。
LOOK_HELPERS = LOOK_HELPERS + _PARSE_ENUM_ERROR_TEXT_SRC + '''

def _parse_enum_error(exc):
    return _parse_enum_error_text(str(exc))


def _allowed_looks(vs, sentinel):
    # 返回 (allowed, how)：当前 view_transform 下 Blender 真正接受的 look identifier。
    # 无论走哪条路径，都保证 vs.look 回到进入时的值。
    original = vs.look
    try:
        try:
            vs.look = sentinel
        except Exception as exc:
            parsed = _parse_enum_error(exc)
            if parsed:
                return parsed, "rna"
        out = []
        for name in _look_candidates():
            try:
                vs.look = name
                out.append(name)
            except Exception:
                pass
        return out, "probe"
    finally:
        try:
            vs.look = original
        except Exception:
            pass
'''


# =============================================================================
#  只读能力探测
# =============================================================================


def _wrap(body: str, extra: str = "") -> str:
    """套上统一的 import / 场景取用 / 内嵌助手骨架。"""
    return f"""
import bpy, json

scene = bpy.context.scene
vs = scene.view_settings
{LOOK_HELPERS}{extra}
{body}
print({LOOK_MARKER!r} + json.dumps(payload, ensure_ascii=False, default=str))
""".strip()


def build_sweep_code() -> str:
    """一次扫出**每个** ``view_transform`` 对应的合法 look（需求 3）。

    建立基线时调用一次即可，之后前端切换视图变换**无需**再问 Blender。
    """
    extra = f"""
_views_seen = []


def _available_views():
    out = []
    try:
        import PyOpenColorIO as ocio
        cfg = ocio.GetCurrentConfig()
        for item in cfg.getViews(scene.display_settings.display_device):
            text = str(item)
            if text not in out:
                out.append(text)
    except Exception:
        pass
    return out
"""
    body = f"""
_orig_view = vs.view_transform
_orig_look = vs.look

_views = _available_views()
if _orig_view not in _views:
    _views.insert(0, _orig_view)

looks = {{}}
sources = {{}}
failed = {{}}
try:
    for _vt in _views:
        try:
            vs.view_transform = _vt
        except Exception as exc:
            failed[_vt] = str(exc)[:200]
            continue
        _allowed, _how = _allowed_looks(vs, {SENTINEL_LOOK!r})
        looks[_vt] = _allowed
        sources[_vt] = _how
finally:
    try:
        vs.view_transform = _orig_view
    except Exception:
        pass
    try:
        vs.look = _orig_look
    except Exception:
        pass

payload = {{
    "looks": looks,
    "sources": sources,
    "failed": failed,
    "view_transforms": _views,
    "original": {{"view_transform": _orig_view, "look": _orig_look}},
    "restored": {{
        "view_transform": vs.view_transform,
        "look": vs.look,
        "ok": vs.view_transform == _orig_view and vs.look == _orig_look,
    }},
}}
"""
    return _wrap(body, extra)


def build_probe_code(view_transform: str, identifier: str | None = None) -> str:
    """探测**单个** ``view_transform`` 的合法 look（需求 2）。

    ``identifier`` 给出时额外回报它在该视图下是否合法 —— 省掉一次往返。
    全程只读语义：``finally`` 恢复原 ``view_transform`` / ``look``。
    """
    # 注意：identifier=None 必须传 Python 的 None，不能退化成字符串 "None" ——
    # 后者恰好是一个合法的 look identifier，会把探测结果带偏。
    probe_literal = "None" if identifier is None else repr(str(identifier))
    body = f"""
_target = {view_transform!r}
_probe_id = {probe_literal}

_orig_view = vs.view_transform
_orig_look = vs.look

allowed = []
how = None
error = None
try:
    vs.view_transform = _target
    allowed, how = _allowed_looks(vs, {SENTINEL_LOOK!r})
except Exception as exc:
    error = str(exc)[:300]
finally:
    try:
        vs.view_transform = _orig_view
    except Exception:
        pass
    try:
        vs.look = _orig_look
    except Exception:
        pass

_probe_allowed = None
if _probe_id is not None:
    _probe_allowed = _probe_id in allowed

payload = {{
    "view_transform": _target,
    "allowed": allowed,
    "source": how,
    "error": error,
    "probe": {{"identifier": _probe_id, "allowed": _probe_allowed}},
    "original": {{"view_transform": _orig_view, "look": _orig_look}},
    "restored": {{
        "view_transform": vs.view_transform,
        "look": vs.look,
        "ok": vs.view_transform == _orig_view and vs.look == _orig_look,
    }},
}}
"""
    return _wrap(body)


def parse_payload(captured_stdout: str) -> dict[str, Any]:
    """取出最后一行 ``__TOON_LOOK_JSON__`` 载荷。"""
    payload: str | None = None
    for line in captured_stdout.splitlines():
        if line.startswith(LOOK_MARKER):
            payload = line[len(LOOK_MARKER):]
    if payload is None:
        raise errors.BlenderUnexpectedResponse(
            "look 能力探针输出中未找到结构化结果行。",
            details={"stdout_tail": captured_stdout[-500:]},
        )
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise errors.BlenderUnexpectedResponse(
            "look 能力探针输出不是合法 JSON。",
            details={"stdout_tail": captured_stdout[-500:]},
        ) from exc
    if not isinstance(parsed, dict):
        raise errors.BlenderUnexpectedResponse("look 能力探针输出的结构不是对象。")
    return parsed


# =============================================================================
#  纯 Python：标签组合、迁移、校验
# =============================================================================


def look_label(view_transform: str | None, identifier: str) -> str:
    """组合 **显示标签**；``identifier`` 保持 Blender 原样。

    * 已经是「族前缀」形式（含 ``" - "``）→ 原样；
    * 通用档位 + 族前缀视图（AgX / False Color）→ ``"<视图> - <档位>"``；
      这正是需求里 ``value="High Contrast" / label="AgX - High Contrast"`` 的来历；
    * 其余 → 与 identifier 相同。
    """
    text = str(identifier)
    if not text or text == NONE_LOOK or _SEPARATOR in text:
        return text
    if text in GENERIC_LOOKS and view_transform in COMPOSED_LABEL_PREFIXES:
        return f"{view_transform}{_SEPARATOR}{text}"
    return text


def normalize_options(view_transform: str, identifiers: list[str]) -> list[dict[str, str]]:
    """把 identifier 列表整理成 ``[{value, label}]``（value 永远是 Blender 原样）。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in identifiers or []:
        value = str(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        out.append({"value": value, "label": look_label(view_transform, value)})
    return out


def normalize_look_map(raw: dict[str, Any] | None) -> dict[str, list[dict[str, str]]]:
    """把探针原始结果整理成 ``{view_transform: [{value, label}]}``。"""
    result: dict[str, list[dict[str, str]]] = {}
    for view in sorted((raw or {}).keys()):
        identifiers = (raw or {}).get(view) or []
        options = normalize_options(str(view), [str(v) for v in identifiers])
        if options:
            result[str(view)] = options
    return result


def allowed_values(look_map: dict[str, list[dict[str, str]]] | None, view_transform: str | None) -> list[str]:
    options = (look_map or {}).get(view_transform or "") or []
    return [str(opt.get("value")) for opt in options]


def labels_for_view(look_map: dict[str, list[dict[str, str]]] | None, view_transform: str | None) -> dict[str, str]:
    options = (look_map or {}).get(view_transform or "") or []
    return {str(opt.get("value")): str(opt.get("label")) for opt in options}


def _candidates(raw: str) -> list[str]:
    """按「规范化等价」的优先级，列出 raw 可能对应的 identifier 候选。"""
    text = str(raw)
    out = [text]
    if _SEPARATOR in text:
        # "AgX - High Contrast" -> "High Contrast"
        out.append(text.split(_SEPARATOR, 1)[1])
    lowered = {c.lower(): c for c in out}
    return out + [v for k, v in lowered.items() if v not in out]


def _match_suffix(raw: str, allowed: list[str]) -> str | None:
    """``"High Contrast"`` 命中 ``"AgX - High Contrast"``。

    这是让「AgX + High Contrast 成功」成立的关键：Blender 5.2 的 AgX 只接受
    带前缀的 identifier，而老版本只接受通用档位，两者都要能吃得下。
    """
    if _SEPARATOR in raw:
        return None
    for value in allowed:
        if _SEPARATOR in value and value.split(_SEPARATOR, 1)[1] == raw:
            return value
    return None


def resolve_look(
    raw: Any, view_transform: str | None, look_map: dict[str, list[dict[str, str]]] | None
) -> dict[str, Any]:
    """把任意形状的 look 入参**规范化**成可直接写入 Blender 的 ``value``。

    返回 ``{value, label, ok, migrated, reason}``。``ok=False`` 表示在当前视图下
    没有任何等价项，调用方应据此报 ``INVALID_DEPENDENT_ENUM``。

    接受（按优先级）：
      1. 命中 allowed 的**真实 identifier**；
      2. 命中 allowed 项的**显示标签**（例：老版 Blender 里 ``"AgX - High Contrast"``
         是 ``"High Contrast"`` 的标签）；
      3. 通用短名命中族前缀 identifier（例：``"High Contrast"`` → ``"AgX - High Contrast"``）；
      4. ``" - "`` 前缀形式的**后缀**命中 allowed（例：``"AgX - High Contrast"`` → ``"High Contrast"``）。
    以上均忽略大小写；全部落空才判为非法。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {"value": None, "label": None, "ok": True, "migrated": False, "reason": "empty"}
    if not isinstance(raw, str):
        return {
            "value": None,
            "label": None,
            "ok": False,
            "migrated": False,
            "reason": "not_a_string",
            "allowed": allowed_values(look_map, view_transform),
        }

    allowed = allowed_values(look_map, view_transform)
    # 拿不到该视图的能力表时不猜，交给调用方（通常意味着探针失败）
    if not allowed:
        return {
            "value": raw,
            "label": raw,
            "ok": False,
            "migrated": False,
            "reason": "no_capability_map",
            "allowed": [],
        }

    exact = {v: v for v in allowed}
    by_label = {label: value for value, label in labels_for_view(look_map, view_transform).items()}
    lowered = {v.lower(): v for v in allowed}

    for candidate in _candidates(raw):
        if candidate in exact:
            return {
                "value": exact[candidate],
                "label": look_label(view_transform, exact[candidate]),
                "ok": True,
                "migrated": candidate != raw,
                "reason": "identifier",
            }
    for candidate in _candidates(raw):
        if candidate in by_label:
            value = by_label[candidate]
            return {
                "value": value,
                "label": look_label(view_transform, value),
                "ok": True,
                "migrated": candidate != raw,
                "reason": "label",
            }
    for candidate in _candidates(raw):
        suffix = _match_suffix(candidate, allowed)
        if suffix is not None:
            return {
                "value": suffix,
                "label": look_label(view_transform, suffix),
                "ok": True,
                "migrated": True,
                "reason": "suffix_of_family_name",
            }
    for candidate in _candidates(raw):
        hit = lowered.get(candidate.lower())
        if hit is not None:
            return {
                "value": hit,
                "label": look_label(view_transform, hit),
                "ok": True,
                "migrated": hit != raw,
                "reason": "case_insensitive",
            }

    return {
        "value": None,
        "label": None,
        "ok": False,
        "migrated": False,
        "reason": "not_allowed_for_view_transform",
        "allowed": allowed,
    }


def invalid_dependent_enum(
    parameter: str,
    value: Any,
    depends_on: dict[str, Any],
    allowed: list[str],
    *,
    view_transform: str | None = None,
) -> errors.ToonTunerError:
    """构造 ``INVALID_DEPENDENT_ENUM``（需求 6：必须是稳定错误，不能落成 BLENDER_SCRIPT_ERROR）。"""
    parent = next(iter(depends_on.items()), ("", ""))
    message = (
        f"{parameter} = {value!r} 在当前 {parent[0]} = {parent[1]!r} 下不合法。"
        f"允许取值：{allowed}"
    )
    return errors.ToonTunerError(
        errors.INVALID_DEPENDENT_ENUM,
        message,
        details={
            "parameter": parameter,
            "value": value,
            "depends_on": dict(depends_on),
            "allowed": list(allowed),
            "view_transform": view_transform,
        },
    )


def validate_look(
    raw: Any, view_transform: str | None, look_map: dict[str, list[dict[str, str]]] | None
) -> dict[str, Any]:
    """严格校验（后端在**调用 Blender 之前**用，需求 6）。

    与 ``resolve_look`` 的差别：只有真的对不上任何等价项才抛错；能规范化就规范化。
    """
    outcome = resolve_look(raw, view_transform, look_map)
    if not outcome["ok"]:
        raise invalid_dependent_enum(
            "color.look",
            raw,
            {"color.view_transform": view_transform},
            outcome.get("allowed") or allowed_values(look_map, view_transform),
            view_transform=view_transform,
        )
    return outcome


def migrate_values(
    values: dict[str, Any], look_map: dict[str, list[dict[str, str]]] | None
) -> dict[str, Any]:
    """把一份「旧预设」里的参数值迁移成 ``{configured_value, effective_value, display_label}``（需求 9）。

    旧预设可能存的是**显示标签**（例：``look = "AgX - High Contrast"``），这里按
    当前视图把它规范化成真实 identifier，并把原字符串保留为 ``display_label``。
    无法迁移时回退到 ``None``，**绝不**把非法字符串原样留下。
    """
    view_transform = values.get("color.view_transform")
    records: dict[str, dict[str, Any]] = {}
    for param_id, raw in (values or {}).items():
        if param_id == "color.look":
            outcome = resolve_look(raw, view_transform, look_map)
            if outcome["ok"]:
                records[param_id] = {
                    "configured_value": outcome["value"],
                    "effective_value": outcome["value"],
                    "display_label": outcome["label"],
                    "migrated_from": raw if outcome["migrated"] else None,
                }
                continue
            fallback = NONE_LOOK if NONE_LOOK in allowed_values(look_map, view_transform) else None
            records[param_id] = {
                "configured_value": fallback,
                "effective_value": fallback,
                "display_label": look_label(view_transform, fallback) if fallback else None,
                "migrated_from": raw,
                "warning": f"{raw!r} 在 {view_transform!r} 下无等价项，已回退到 {fallback!r}。",
            }
            continue
        records[param_id] = {
            "configured_value": raw,
            "effective_value": raw,
            "display_label": str(raw) if isinstance(raw, str) else raw,
            "migrated_from": None,
        }
    return records


def remap_for_view_transform(
    look_map: dict[str, list[dict[str, str]]] | None,
    old_value: Any,
    new_view_transform: str | None,
) -> dict[str, Any]:
    """切换 ``view_transform`` 后，按同一套规范化规则迁移旧 look（需求 4）。

    无等价项时回退 ``None``；**不要在切换的瞬间把旧值发给 Blender**。
    """
    outcome = resolve_look(old_value, new_view_transform, look_map)
    if outcome["ok"]:
        return outcome
    fallback = NONE_LOOK if NONE_LOOK in allowed_values(look_map, new_view_transform) else None
    return {
        "value": fallback,
        "label": look_label(new_view_transform, fallback) if fallback else None,
        "ok": True,
        "migrated": True,
        "reason": "fallback_none",
        "from": old_value,
    }


def describe_capability(probe: dict[str, Any]) -> dict[str, Any]:
    """把单视图探针结果整理成前端契约。"""
    view = str(probe.get("view_transform") or "")
    return {
        "view_transform": view,
        "options": normalize_options(view, list(probe.get("allowed") or [])),
        "source": probe.get("source"),
        "error": probe.get("error"),
        "restored": probe.get("restored"),
    }
