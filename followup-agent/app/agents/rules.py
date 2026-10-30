"""随访方案合规规则引擎（阶段 4 · P2）。

为什么必须是**确定性代码**而不是提示词：
  「这组随访节点是否合规」是一个可以被逐条列举的判定，不是语言任务。
  把判定交给模型，等于让被审对象自己给自己打分：
  同一份方案今天过、明天不过，出了事也无从复现。
  所以这里只做三件事：把方案档位写成常量 → 逐条比对 → 给出可引用的违规明细。

判定口径（与《出院后随访管理制度》对齐，规则版本见 RULE_VERSION）：
  1. 节点必须落在 0–365 天；同一天不允许重复排两条（会变成两条 09:00 的任务）；
  2. 渠道必须在白名单内（电话/短信/企业微信/门诊）；
  3. 每个方案档位有「必检节点」（例如骨科术后 3/7/30 天），缺一个就不合规；
  4. 允许比必检节点多排（多排只提示、不阻断）——临床常有加做，不该被系统拦死；
  5. 判定必须附制度证据：知识库里检索不到对应条文时，宁可返回"无法校验"，
     也不返回 passed=true。**没有依据的通过，比拒绝更难追责。**

注意：规则只覆盖"排期结构"，不覆盖临床适应症。红旗症状走 assess_red_flag，
两者互不替代。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# 规则版本：任何档位/阈值改动都必须改这个号，它会被写进票据与审计记录，
# 让"当时的 passed 是按哪版规则算的"可回溯。
RULE_VERSION = "rule-v1.0"

MAX_OFFSET_DAYS = 365
VALID_CHANNELS = {"phone", "sms", "wecom", "outpatient"}

SEVERITY_ERROR = "error"  # 阻断：不得创建草稿
SEVERITY_WARN = "warn"  # 提示：可以创建，但要在确认卡片上让医护看见


@dataclass(frozen=True)
class RequiredNode:
    """一个必检节点。"""

    offset_days: int
    name_hint: str
    # 该节点必须使用的渠道（空 = 不限）。红旗类节点只允许电话，短信通知不等于随访。
    channel: str = ""


@dataclass(frozen=True)
class PlanProfile:
    """方案档位：别名用于把模型/医护的说法归一到同一档。"""

    key: str
    label: str
    aliases: tuple[str, ...]
    required: tuple[RequiredNode, ...]


# 档位定义刻意保守：只收录制度里写死了节点的方案。
# 没收录的方案走 DEFAULT_PROFILE（只校验结构，不校验必检节点），
# 并在返回值里标 profile_known=false，提示"该方案尚无结构化规则"。
PLAN_PROFILES: tuple[PlanProfile, ...] = (
    PlanProfile(
        key="ortho_postop",
        label="骨科术后随访",
        aliases=("骨科", "关节", "髋", "膝", "脊柱", "置换", "骨折"),
        required=(
            RequiredNode(3, "出院前评估（伤口敷料/下地情况）"),
            RequiredNode(7, "电话随访（切口与疼痛）", channel="phone"),
            RequiredNode(30, "门诊复查（评估功能恢复）", channel="outpatient"),
        ),
    ),
    PlanProfile(
        key="cardio_postop",
        label="冠心病/心梗出院随访",
        aliases=("心梗", "冠心病", "冠脉", "心内", "PCI", "支架", "心衰"),
        # 心梗出院后的 30 天是再入院与用药依从的关键窗口，制度里写死为必检
        required=(
            RequiredNode(30, "门诊复查（用药依从性 + 症状评估）", channel="outpatient"),
            RequiredNode(90, "电话随访（胸痛/气促症状复核）", channel="phone"),
            RequiredNode(180, "门诊复查（心功能与危险因素）", channel="outpatient"),
        ),
    ),
)

DEFAULT_PROFILE = PlanProfile(
    key="generic_postop",
    label="常规术后随访",
    aliases=(),
    required=(
        RequiredNode(7, "电话随访"),
        RequiredNode(30, "门诊复查", channel="outpatient"),
    ),
)


@dataclass
class Violation:
    """一条违规明细：机器可判、人能读懂、能对上制度。"""

    code: str
    message: str
    severity: str = SEVERITY_ERROR
    detail: dict = field(default_factory=dict)


@dataclass
class RuleVerdict:
    passed: bool
    profile_key: str
    profile_label: str
    profile_known: bool
    violations: list[Violation]
    required_offsets: list[int]
    normalized_offsets: list[int]
    rule_version: str = RULE_VERSION

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "rule_version": self.rule_version,
            "profile": self.profile_key,
            "profile_label": self.profile_label,
            "profile_known": self.profile_known,
            "required_offsets": self.required_offsets,
            "normalized_offsets": self.normalized_offsets,
            "violations": [
                {"code": v.code, "message": v.message, "severity": v.severity, "detail": v.detail}
                for v in self.violations
            ],
        }


def match_profile(plan_type: str) -> tuple[PlanProfile, bool]:
    """把方案名归一到档位。返回 (档位, 是否命中已知档位)。"""
    text = (plan_type or "").lower()
    for profile in PLAN_PROFILES:
        if profile.key in text or profile.label in (plan_type or ""):
            return profile, True
        if any(alias.lower() in text for alias in profile.aliases):
            return profile, True
    return DEFAULT_PROFILE, False


def coerce_items(raw_items: object) -> list[dict]:
    """把模型给的 items 洗成可判定的结构。

    只做「能判定所需」的清洗，不与 task_service 的落库归一化抢职责：
    这里保留原始 offset（不合法的值不丢，交给 evaluate 判定为违规并说明原因），
    因为"模型给了 -3 天"本身就是要报给医护看的事实，静默丢弃会掩盖模型的问题。
    """
    items: list[dict] = []
    if not isinstance(raw_items, list):
        return items
    for raw in raw_items[:12]:
        if not isinstance(raw, dict):
            continue
        try:
            offset = int(raw.get("offset_days"))
        except (TypeError, ValueError):
            offset = None
        items.append(
            {
                "name": str(raw.get("name") or "").strip()[:64],
                "offset_days": offset,
                "channel": str(raw.get("channel") or "").strip(),
                "notes": str(raw.get("notes") or "").strip()[:200],
            }
        )
    return items


def evaluate(plan_type: str, raw_items: object, *, evidence_ids: list[int] | None = None) -> RuleVerdict:
    """逐条判定。evidence_ids = 本次校验引用的制度切片 id（用于把结论钉在条文上）。"""
    items = coerce_items(raw_items)
    profile, known = match_profile(plan_type)
    violations: list[Violation] = []

    if not items:
        violations.append(Violation("NO_ITEMS", "没有任何随访节点，无法校验"))
        return RuleVerdict(
            passed=False,
            profile_key=profile.key,
            profile_label=profile.label,
            profile_known=known,
            violations=violations,
            required_offsets=[n.offset_days for n in profile.required],
            normalized_offsets=[],
        )

    seen: dict[int, str] = {}
    valid_offsets: list[int] = []
    for item in items:
        offset = item["offset_days"]
        if offset is None:
            violations.append(
                Violation(
                    "OFFSET_NOT_INTEGER",
                    f"节点「{item['name'] or '未命名'}」的天数不是整数，无法排期",
                    detail={"name": item["name"], "raw": item["offset_days"]},
                )
            )
            continue
        if not 0 <= offset <= MAX_OFFSET_DAYS:
            violations.append(
                Violation(
                    "OFFSET_OUT_OF_RANGE",
                    f"节点「{item['name'] or offset}」位于第 {offset} 天，超出 0–{MAX_OFFSET_DAYS} 天范围",
                    detail={"offset_days": offset},
                )
            )
            continue
        if offset in seen:
            violations.append(
                Violation(
                    "DUPLICATE_NODE",
                    f"第 {offset} 天排了两个节点（{seen[offset]} / {item['name']}），会生成两条同一时间的任务",
                    detail={"offset_days": offset},
                )
            )
            continue
        seen[offset] = item["name"] or f"第 {offset} 天"
        valid_offsets.append(offset)

    # 渠道白名单：空值按"未指定"处理，不算违规（落库时会归一为 phone）
    for item in items:
        if item["channel"] and item["channel"] not in VALID_CHANNELS:
            violations.append(
                Violation(
                    "CHANNEL_INVALID",
                    f"节点「{item['name'] or '未命名'}」的随访方式「{item['channel']}」不在允许范围内",
                    severity=SEVERITY_WARN,
                    detail={"channel": item["channel"]},
                )
            )

    valid_offsets = sorted(set(valid_offsets))
    required = [n.offset_days for n in profile.required]
    if known:
        for node in profile.required:
            if node.offset_days not in valid_offsets:
                violations.append(
                    Violation(
                        "MISSING_REQUIRED_NODE",
                        f"{profile.label}要求第 {node.offset_days} 天做「{node.name_hint}」，方案里没有这一天",
                        detail={"offset_days": node.offset_days, "name_hint": node.name_hint},
                    )
                )
            else:
                # 必检节点的渠道要求（如红旗复核必须电话）
                actual = next(
                    (i["channel"] for i in items if i["offset_days"] == node.offset_days),
                    "",
                )
                if node.channel and actual and actual != node.channel:
                    violations.append(
                        Violation(
                            "NODE_CHANNEL_MISMATCH",
                            f"第 {node.offset_days} 天「{node.name_hint}」要求使用"
                            f"{'电话' if node.channel == 'phone' else node.channel}，当前是 {actual}",
                            severity=SEVERITY_WARN,
                            detail={"offset_days": node.offset_days},
                        )
                    )
        extras = [o for o in valid_offsets if o not in required]
        if extras:
            violations.append(
                Violation(
                    "EXTRA_NODE",
                    f"方案比制度多排了第 {', '.join(map(str, extras))} 天，允许，但请在确认时核对是否有依据",
                    severity=SEVERITY_WARN,
                    detail={"offsets": extras},
                )
            )
    else:
        violations.append(
            Violation(
                "PROFILE_UNKNOWN",
                f"「{plan_type or '未命名方案'}」还没有结构化随访规则，本次只校验了排期结构",
                severity=SEVERITY_WARN,
            )
        )

    if not evidence_ids:
        # 没有制度证据就不给 pass：这是硬口径，见文件头第 5 条
        violations.append(
            Violation(
                "NO_POLICY_EVIDENCE",
                "知识库中没有检索到该方案的随访制度条文，无法完成合规校验",
            )
        )

    passed = not any(v.severity == SEVERITY_ERROR for v in violations)
    return RuleVerdict(
        passed=passed,
        profile_key=profile.key,
        profile_label=profile.label,
        profile_known=known,
        violations=violations,
        required_offsets=required,
        normalized_offsets=valid_offsets,
    )


def rule_sha(verdict: RuleVerdict, evidence_ids: list[int]) -> str:
    """规则指纹：票据与审计都记它。

    组成 = 规则版本 + 档位 + 实际节点 + 制度切片 id。
    任一项变了（改了方案、换了依据条文、升级了规则），指纹就变，
    旧票据自然失效——这比"记得改配置"可靠。
    """
    raw = "|".join(
        [
            verdict.rule_version,
            verdict.profile_key,
            ",".join(map(str, verdict.normalized_offsets)),
            ",".join(map(str, sorted(evidence_ids))),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 红旗症状

RED_FLAGS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    # (类别, 关键词, 风险等级, 制度要求的处置动作)
    (
        "cardiac",
        ("胸痛", "胸闷", "心前区", "压榨", "气促", "呼吸困难", "冒冷汗"),
        "high",
        "立即停止问卷，10 分钟内联系主管医生/胸痛中心，必要时拨打急救电话；不得让患者自行前往",
    ),
    (
        "infection",
        ("发热", "发烧", "高热", "38.5", "39 度", "39度", "渗液", "红肿", "感染", "脓"),
        "high",
        "立即终止问卷，2 小时内电话通知主管医生并记录接手人；24 小时内回访确认处理结果",
    ),
    (
        "neuro",
        ("意识", "晕厥", "昏迷", "抽搐", "说不清话", "肢体无力"),
        "high",
        "立即终止随访并联系主管医生，按急症流程处理",
    ),
    (
        "bleeding",
        ("出血", "血肿", "渗血", "黑便", "呕血"),
        "high",
        "立即终止问卷并电话联系主管医生，记录出血量与时间",
    ),
    (
        "pain",
        ("剧痛", "剧烈疼痛", "疼痛加重", "疼得"),
        "medium",
        "当日电话复核疼痛评分与用药情况，必要时提前门诊复查",
    ),
)


def match_red_flags(text: str) -> list[dict]:
    """关键词命中表。返回命中的类别与依据，不做同义扩展猜测。"""
    body = text or ""
    hits: list[dict] = []
    for category, keywords, level, action in RED_FLAGS:
        matched = [k for k in keywords if k in body]
        if matched:
            hits.append(
                {
                    "category": category,
                    "keywords": matched,
                    "level": level,
                    "required_action": action,
                }
            )
    return hits


def risk_level(hits: list[dict]) -> str:
    """取最高等级：high > medium > low。"""
    levels = {h["level"] for h in hits}
    if "high" in levels:
        return "high"
    if "medium" in levels:
        return "medium"
    return "low"
