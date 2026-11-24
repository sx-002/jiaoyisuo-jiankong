"""0 模型规则判危：有序规则、首个命中即停、命中规则名留痕。

两个维度：发生了什么（规则）+ 影响范围（全站/单链/单币）。
判危关键：给不给具体恢复时间、限额数字、涉及哪条链；不给时间又反复变化=高风险。
只输出事实、证据和可解释理由，不给行情判断、不给点位。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

HIGH, MEDIUM, LOW = "high", "medium", "low"
STAGE_LABEL = {1: "阶段1 限额", 2: "阶段2 延迟排队", 3: "阶段3 全面暂停", 4: "阶段4 客服失联",
               5: "阶段5 登录不了", 0: "未入阶段"}
SCOPE_LABEL = {"site": "全站", "chain": "单链", "coin": "单币", "unknown": "未说明范围"}

#: 中文过滤：标题里没有汉字、且标题+摘要的中文占比低于这个值，就判为纯英文不推。
MIN_CHINESE_RATIO = 0.05
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


@dataclass
class Decision:
    level: str
    stage: int
    scope: str
    reason: str
    rule_name: str
    signal: str
    resolved: bool = False
    has_details: bool = False
    pushable: bool = False
    recovery: str = ""
    limit: str = ""
    risk_limit: bool = False

    def to_dict(self) -> dict:
        return {
            "level": self.level, "stage": STAGE_LABEL[self.stage], "scope": SCOPE_LABEL[self.scope],
            "reason": self.reason, "rule": self.rule_name, "signal": self.signal,
            "resolved": self.resolved, "has_details": self.has_details,
            "recovery": self.recovery, "limit": self.limit,
            "risk_limit": self.risk_limit,
        }


# 顺序即优先级：第一个命中的规则决定事件类型，后面的规则不再参与。
# 下限等级只是初始值，最终等级还会按阶段与影响范围调整。
RULES: List[Tuple[str, str, str, int, str]] = [
    ("R01_exit_scam", r"(?<!没)(?<!沒)(?<!不会)(?<!不會)(?<!否认)(?<!否認)跑路|卷款|exit scam|rug pull|"
     r"insolvency|破产清算|有序停运|停止运营|关停提现",
     HIGH, 5, "跑路/停运"),
    ("R02_login_down", r"登录不了|无法登录|登不上|app[^。！？]{0,12}打不开|官网[^。！？]{0,12}打不开|login failed|site unavailable|can'?t log in",
     HIGH, 5, "登录异常"),
    ("R03_hacked", r"被黑|被盗|黑客|hacked|exploit|breach|stolen|安全事件",
     HIGH, 4, "安全事件"),
    ("R04_regulatory_freeze", r"监管冻结|冻结账户|资产冻结|seized|freeze|监管[^。！？]{0,16}查封|美国管控|OFAC|"
     r"提现授权|提币授权|未获批.*无法提币",
     HIGH, 4, "监管冻结"),
    ("R05_withdraw_suspended", r"暂停.{0,40}(提币|提现|充提|充值|充值提现)|停止.{0,40}(提币|提现)|全面暂停|"
     r"withdrawal[s]? (suspended|paused|halted|disabled)|deposit[s]? disabled|withdrawals? closed",
     HIGH, 3, "暂停提币/提现"),
    ("R06_support_gone", r"客服失联|客服不回复|工单[^。！？]{0,20}无人|support not responding|support[^.]{0,20}unavailable|社群禁言",
     HIGH, 4, "客服失联"),
    ("R07_withdraw_delay", r"提现延迟|提币延迟|提现不到账|提币不到账|延迟排队|审核中|提现失败|提币失败|"
     r"withdrawal[s]? (delayed|pending|failed|stalled)|funds stuck|not credited|提幣卡單",
     MEDIUM, 2, "提现延迟/失败"),
    ("R08_maintenance_extended", r"维护延期|延期|再次延期|延长维护|maintenance extended|extended maintenance|"
     r"delay(ed)? (until|maintenance)|rescheduled",
     MEDIUM, 2, "维护延期"),
    ("R09_withdraw_limit", r"提现限制|提币限制|限制提现|限制提币|提现限额|提币限额|限额|门槛|"
     r"仅允许.{0,8}小额提现|小额提现|单日.{0,6}(?:上限|限额)|每日.{0,6}(?:上限|限额)|"
     r"手续费上调|withdrawal limit|withdrawal fee increased|fee increase",
     MEDIUM, 1, "提现限额"),
    ("R10_reserve_redeem", r"储备金|储备率下降|赎回关闭|暂停赎回|高息理财|理财[^。！？]{0,12}加息|"
     r"proof of reserves|redeem (closed|disabled)",
     MEDIUM, 1, "储备金/理财"),
    ("R11_token_site", r"平台币暴跌|平台币[^。！？]{0,12}脱锚|depeg|token[^.]{0,20}collapse",
     MEDIUM, 1, "平台币异常"),
    ("R12_maintenance", r"维护升级|系统升级|维护|maintenance|upgrade|scheduled",
     LOW, 0, "常规维护"),
]
RULES = [(name, re.compile(pattern, re.I), level, stage, signal)
         for name, pattern, level, stage, signal in RULES]

RESOLVED_RE = re.compile(r"恢复(提币|提现|充值提现)|(提币|提现)已恢复|已恢复提(币|现)|"
                         r"maintenance completed|service restored|withdrawals? resumed|deposits? enabled", re.I)
NOISE_RE = re.compile(r"上线|上币|活动|airdrop|listing|promotion|giveaway|竞猜|抽奖|new token", re.I)
#: 下架/下市：单个币种例行下线，不当作平台级提现中断。
DELIST_RE = re.compile(r"下架|下市|delist|delisting", re.I)
#: 正文里的下架上下文：即使标题是「交易所的公告」也能认出是例行业务。
DELIST_CONTEXT_RE = re.compile(
    r"交易对下线|下架交易对|下线时间|币种名称|不再满足上架|流动性不佳|交易量持续低迷", re.I)
COIN_RE = re.compile(r"\b(BTC|ETH|USDT|USDC|BNB|SOL|XRP|DOGE|TRX|ADA|DOT|LTC|BCH|ARB|OP|TON|APT|SUI)\b|"
                     r"单币|某币|个别币种|小币种|某个币种", re.I)
CHAIN_RE = re.compile(r"链上|单链|ERC[- ]?20|TRC[- ]?20|BEP[- ]?20|Solana 链|以太坊链|"
                      r"某条链|链|网络|chain|network", re.I)
SITE_RE = re.compile(r"全站|全平台|所有币|所有币种|全部提现|全部提币|site[- ]?wide|all coins|全链", re.I)
DETAIL_RE = re.compile(r"\d{1,2}:\d{2}|\d+\s*(分钟|小时|分钟|minutes?|hours?|days?)|"
                       r"\d{4}-\d{2}-\d{2}|限额|额度|具体时间|恢复时间", re.I)

#: 提现/充提异常类：消息里必须带恢复信息与限额数字。
WITHDRAWAL_RULES = frozenset({
    "R05_withdraw_suspended", "R07_withdraw_delay", "R09_withdraw_limit",
})
#: 需要用「有没有恢复时间」定级的异常类（限额类 R09 只带数字，不按时钟升级）。
WITHDRAWAL_TIME_RULES = frozenset({
    "R05_withdraw_suspended", "R07_withdraw_delay",
})
#: 具体恢复时间：20:20 / 20:20 已恢复 / 预计 20:20 恢复 / 恢复时间 20:20。
RECOVERY_TIME_RE = re.compile(
    r"(\d{1,2}[:：]\d{2})\s*(?:UTC\s*\+?\s*\d*)?\s*(?:已)?(?:恢复|解除|开放|重启)"
    r"|恢复(?:时间)?\s*[:：]?\s*(\d{1,2}[:：]\d{2})"
    r"|(\d{1,2}[:：]\d{2})\s*(?:UTC\s*\+?\s*\d*)?\s*(?:已)?(?:恢复|解除|开放)",
    re.I)
#: 只说“另告/待定/另行通知”：等于没给时间。
RECOVERY_UNKNOWN_RE = re.compile(
    r"恢复时间\s*(?:另告|待定|另行通知|另期通知|稍后通知)|另行通知|稍后通知|待通知", re.I)
#: 带数字的时限：预计 2 小时内恢复 / 30 分钟后恢复。
RECOVERY_ETA_RE = re.compile(
    r"(\d+\s*(?:个)?\s*(?:分钟|分|小时|个小时|天|工作日))\s*(?:内|后|以内)?")
#: 限额/门槛数字：每日限额 1000 USDT、单笔上限 5000 USDT 等。
LIMIT_RE = re.compile(
    r"((?:每日|单笔|每次|单次)?\s*(?:限额|上限|额度|阈值)[^0-9\n]{0,8}"
    r"[0-9][0-9,]*(?:\.[0-9]+)?\s*[A-Za-z]{0,10})")
#: 已经恢复：提现/充提 + 15 字内出现“已恢复”。
RECOVERED_WITH_TIME_RE = re.compile(r"(提币|提现|充提|充值)[^。；\n]{0,15}已恢复")


def recovery_note(text: str) -> str:
    """从正文抽恢复信息：具体时间、已恢复、或“恢复时间另告”。"""
    text = text or ""
    match = RECOVERY_TIME_RE.search(text)
    if match:
        stamp = next((group for group in match.groups() if group), "")
        if "已恢复" in text:
            return f"{stamp} 已恢复"
        return f"{stamp} 恢复"
    eta = RECOVERY_ETA_RE.search(text)
    if eta and re.search(r"恢复|开放|解除|重开|到账", text):
        span = re.sub(r"\s+", "", eta.group(1))
        return f"预计 {span} 恢复"
    if RECOVERY_UNKNOWN_RE.search(text):
        return "恢复时间另告"
    return ""


def limit_note(text: str) -> str:
    """从正文抽限额/门槛数字（只作参考，进消息但不改分级本身）。"""
    match = LIMIT_RE.search(text or "")
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


#: 真正影响资金安全的限额：只允许小额提现、单日/每日上限等，才进推送。
RISK_LIMIT_RE = re.compile(
    r"(?:仅|只)允许.{0,8}小额|小额提现|单日.{0,6}(?:上限|限额)|每日.{0,6}(?:上限|限额)|"
    r"提现.{0,8}上限.{0,12}(?:\d|[一二三四五六七八九十])", re.I)
MAINTENANCE_RE = re.compile(r"维护|升级|钱包维护|scheduled|maintenance", re.I)


def risk_limit_note(text: str) -> str:
    """只把「卡住资金」的限额当风险；维护类限额只留内部字段。"""
    note = limit_note(text or "")
    if not note or MAINTENANCE_RE.search(text or ""):
        return ""
    return note if RISK_LIMIT_RE.search(text or "") else ""


def is_push_worthy(decision: "Decision", text: str) -> bool:
    """防跑路口径：只推真正威胁资金安全的事，日常维护/恢复只存档。

    进推送：跑路/停运、登录不了、被黑被盗、监管冻结、客服失联；
    全站或范围未明且没给恢复时间的暂停提现；卡住资金的限额。
    只存档：单个小币/小链维护、给了恢复时间或已恢复的暂停、延期、
    常规维护、下架、平台币波动、储备金/理财类软信号。
    """
    rule, scope, recovery = decision.rule_name, decision.scope, decision.recovery
    if rule in ("R01_exit_scam", "R02_login_down", "R03_hacked",
                "R04_regulatory_freeze", "R06_support_gone"):
        return True
    if rule == "R05_withdraw_suspended":
        # 全站/链级/范围未明的硬暂停且没有「具体」恢复时间，才像跑路前兆。
        # 单币种小维护不算（scope=coin 直接落空）。
        timed = bool(re.search(r"\d", recovery or "")) or "已恢复" in (recovery or "")
        return decision.level == HIGH and scope in ("site", "chain", "unknown") and not timed
    if rule in ("R07_withdraw_delay", "R09_withdraw_limit"):
        return decision.risk_limit
    return False


def infer_scope(text: str) -> str:
    if SITE_RE.search(text):
        return "site"
    if CHAIN_RE.search(text):
        return "chain"
    if COIN_RE.search(text):
        return "coin"
    return "unknown"


def _level_for(level: str, stage: int, scope: str) -> str:
    if stage >= 4:
        return HIGH
    if stage == 3:
        return HIGH if scope in ("site", "chain", "unknown") else MEDIUM
    if stage in (1, 2):
        return MEDIUM if scope in ("site", "chain", "unknown") else LOW
    return level


def is_editorial_noise(title: str, summary: str = "") -> bool:
    """行业解读/科普/教程/访谈/活动推广这类非运营内容。"""
    return bool(HELP_PAGE_TITLE_RE.search(title or "") or EDITORIAL_RE.search(title or ""))


def judge(title: str, summary: str = "", body: str = "", *, prior_stage: int = 0,
          cloud_sources: int = 1, cloud_users: int = 0) -> Optional[Decision]:
    """规则判定；没有命中任何风险词返回 None（普通公告不入库推送）。"""
    text = " ".join(part for part in (title, summary, body[:8000]) if part).strip()
    if not text:
        return None
    # 内容层过滤：只留运营事件。行业新闻/科普/访谈/活动即使命中风险词也不推，
    # 否则真正的提现异常会被淹没。标题+摘要不够时再看正文开头。
    if not is_operational_event(title, summary):
        if not (body and is_operational_event(title, body[:2000])):
            return None
    resolved = bool(RESOLVED_RE.search(text)) or bool(RECOVERED_WITH_TIME_RE.search(text))
    if resolved:
        scope = infer_scope(text)
        recovery = recovery_note(text)
        limit = limit_note(text)
        reason = "命中恢复信号，按已解除处理，只推一次解除"
        withdrawal_related = bool(re.search(r"提币|提现|充提|充值|withdraw|deposit", text, re.I))
        if withdrawal_related:
            # 提现/充提类恢复：写明具体恢复时间，没有具体时间就写「恢复时间另告」。
            if recovery:
                reason += f"；恢复信息：{recovery}"
            else:
                recovery = "已恢复（未给具体时间）"
                reason += f"；恢复信息：{recovery}"
            if limit:
                reason += f"；限额数字：{limit}"
        else:
            # 其他类型的恢复事件保持原样，不加恢复/限额行。
            recovery = ""
            limit = ""
        level = MEDIUM if withdrawal_related else LOW
        # 防跑路口径：已恢复只存档，不推给用户。
        return Decision(level, 0, scope, reason,
                        "R00_resolved", "恢复", True, bool(DETAIL_RE.search(text)), False,
                        recovery=recovery, limit=limit)

    for name, pattern, base_level, stage, signal in RULES:
        match = pattern.search(text)
        if not match:
            continue
        scope = infer_scope(text)
        # 纯上币/活动噪音：没有风险链时不推，只交给调用方存档。
        if name.startswith("R12") and NOISE_RE.search(text) and stage == 0:
            return None
        if name.startswith("R12") and stage == 0 and not re.search(r"维护升级|维护|maintenance", text, re.I):
            return None
        # 下架公告：单币种例行下线，不按平台提现中断判危。
        # 标题/摘要直接写下架，或正文有完整下架上下文（泛标题「交易所的公告」也覆盖）；
        # 正文顺带提一句下架、但本身是全站暂停的真实事件不在这里排除。
        delist_signal = DELIST_RE.search(f"{title} {summary}") or (
            DELIST_RE.search(text) and DELIST_CONTEXT_RE.search(text))
        if name in ("R05_withdraw_suspended", "R07_withdraw_delay") \
                and delist_signal and not SITE_RE.search(text):
            return None
        has_details = bool(DETAIL_RE.search(text))
        level = _level_for(base_level, stage, scope)
        reason_parts = [f"命中规则 {name}：{match.group(0)[:40]}", f"阶段 {stage}",
                        SCOPE_LABEL[scope]]
        recovery = ""
        limit = ""
        if name in WITHDRAWAL_RULES:
            # 提现/充提异常类：给不给恢复时间与限额数字，直接决定等级。
            recovery = recovery_note(text)
            limit = limit_note(text)
            has_time = bool(re.search(r"\d", recovery)) and recovery != "恢复时间另告"
            if name in WITHDRAWAL_TIME_RULES:
                if not recovery:
                    # 公告没写恢复时间，按定稿统一写「恢复时间另告」。
                    recovery = "恢复时间另告"
                if has_time:
                    level = MEDIUM
                    reason_parts.append(f"给了具体恢复时间（{recovery}），按普通维护处理")
                elif limit:
                    level = MEDIUM
                    reason_parts.append(f"未给恢复时间，但给了限额数字（{limit}）作参考")
                else:
                    level = HIGH
                    reason_parts.append("未给恢复时间也未给限额数字，按高风险处理")
            elif limit:
                reason_parts.append(f"限额类事件，带限额数字（{limit}）作参考")
        elif not has_details and stage >= 2:
            reason_parts.append("正文未给明确恢复时间或限额数字，按风险上调")
            if level != HIGH:
                level = HIGH
        if prior_stage and stage > prior_stage:
            reason_parts.append(f"由阶段{prior_stage}升级为阶段{stage}")
            level = HIGH if stage >= 3 else max(level, MEDIUM, key=lambda x: [LOW, MEDIUM, HIGH].index(x))
        if cloud_sources >= 2 or cloud_users >= 3:
            reason_parts.append(f"独立来源 {cloud_sources} 个/独立用户 {cloud_users} 个，达到升级门槛")
            level = HIGH if stage >= 2 else level
        risk_limit = risk_limit_note(text) if name in WITHDRAWAL_RULES else ""
        decision = Decision(level=level, stage=stage, scope=scope, reason="；".join(reason_parts),
                            rule_name=name, signal=signal, has_details=has_details,
                            pushable=False, recovery=recovery, limit=limit,
                            risk_limit=risk_limit)
        decision.pushable = is_push_worthy(decision, text)
        return decision
    return None


def clean_title_for_match(title: str) -> str:
    """去重比对用的标题清洗：只去空白与常见分隔，保留数字和大小写。"""
    return re.sub(r"[\s\u3000]+", "", (title or "")).strip()


#: 一眼能看出是两件事的反义词/不同主体；相似度再高也不合并。
CONFLICT_PAIRS = (
    ("暂停", "恢复"), ("关闭", "开放"), ("加息", "降息"),
    ("上涨", "下跌"), ("增持", "减持"), ("通过", "否决"),
    ("被黑", "辟谣"), ("跑路", "辟谣"), ("上市", "退市"),
)


def event_conflict(left: str, right: str) -> bool:
    a, b = (left or ""), (right or "")
    return any((x in a and y in b) or (y in a and x in b) for x, y in CONFLICT_PAIRS)


#: 运营事件关键词：跟用户资金直接相关，才进判定。
OPERATIONAL_RE = re.compile(
    r"提币|提现|充币|充值|充提|暂停|恢复|维护|升级|登录|登不上|下架|停止交易|"
    r"限额|上限|限额|冻结|查封|被黑|被盗|黑客攻击|安全事件|破产|跑路|清盘|兑付|"
    r"客服|工单|储备金|赎回|平台币|手续费|地址更换|迁移|withdraw|deposit|"
    r"maintenance|suspend|resume|hack|delist", re.I)
#: 行业解读/技术科普/观点访谈/活动推广：不是运营事件。
EDITORIAL_RE = re.compile(
    r"科普|一文读懂|解读|观点|访谈|专访|人物|故事|教程|指南|百科|研究|报告|复盘|"
    r"预测|展望|盘点|盘点|如何理解|背后|意味着什么|深度|专栏|评论|"
    r"活动预告|活动|空投|抽奖|征文|AMA|直播|上新|产品发布|推广", re.I)
#: 帮助页/科普/教程标题：命中就挡，正文里出现「暂停」也不能救回。
#: bianbtc「公告里的暂停提现通常意味着什么？先分清三种情况」就是被这条挡下的。
HELP_PAGE_TITLE_RE = re.compile(
    r"意味着什么|先分清|科普|教程|怎么回事|怎么办|如何理解|怎么理解|"
    r"一文读懂|一文看懂|什么是|如何判断|怎么判断|常见问题|问答|"
    r"how[\s-]?to|what[\s-]?is|tutorial|faq|explained?", re.I)

#: 内容站/博客/资讯文章路径：官方公告路径先豁免，其余按内容页挡。
CONTENT_PAGE_PATH_RE = re.compile(
    r"/(?:post|posts|article|blog|news|learn|academy|guide|guides|tutorial|tutorials|"
    r"faq|help|wiki|edu|education|story|stories|insight|research)(?:/|$|\.)", re.I)

#: 已知非官方内容站：没有官方公告路径就不推。
CONTENT_SITE_HOSTS = (
    "bianbtc.com", "8btc.com", "chaincatcher.com", "odaily.news",
    "panewslab.com", "theblockbeats.info", "jinse.cn", "blockbeats.info",
)

#: 官方公告/帮助中心路径：即使带 news/article 字样也算官方，避免误杀真公告。
OFFICIAL_ANNOUNCEMENT_PATH_RE = re.compile(
    r"/(?:announcement|announcements|support|notice|notices|product-launch|"
    r"newsflash|gong-gao|hc|status|incidents)(?:/|$)", re.I)


def is_help_or_content_page(title: str, url: str = "") -> bool:
    """帮助页/科普/教程/内容站文章：命中即挡，运营词不能救回。

    真实运营事件（暂停提现/恢复/维护/下架/监管）标题不含这些说法；
    官方公告路径（/support/announcement、/hc/... 等）永远豁免。
    """
    if HELP_PAGE_TITLE_RE.search(title or ""):
        return True
    from urllib.parse import urlsplit
    parts = urlsplit(url or "")
    path = parts.path or ""
    host = (parts.hostname or "").lower()
    if OFFICIAL_ANNOUNCEMENT_PATH_RE.search(path):
        return False
    if host and any(host == h or host.endswith("." + h) for h in CONTENT_SITE_HOSTS):
        return True
    return bool(CONTENT_PAGE_PATH_RE.search(path))


def is_operational_event(title: str, summary: str = "") -> bool:
    """只保留运营事件（提现/充值/维护/登录/下架/限额/监管/安全事故）。

    行业解读、技术科普、市场观点、人物访谈、活动预告、产品推广、空投活动
    一律不算——它们会把真正的提现异常埋掉。帮助页/科普标题即使带「暂停提现」
    也不放行（否则 bianbtc 这类文章会冒充高风险推送）。
    """
    text = f"{title or ''} {summary or ''}"
    if not OPERATIONAL_RE.search(text):
        return False
    if HELP_PAGE_TITLE_RE.search(title or "") or EDITORIAL_RE.search(title or ""):
        return False
    return True


def chinese_ratio(text: str) -> float:
    """标题+摘要里汉字占「有意义字符（字母、数字、汉字）」的比例。"""
    chars = [ch for ch in (text or "") if ch.isalnum() or CJK_RE.match(ch)]
    if not chars:
        return 0.0
    cjk = sum(1 for ch in chars if CJK_RE.match(ch))
    return cjk / len(chars)


def is_non_chinese(title: str, summary: str = "") -> bool:
    """纯英文（用户看不懂）判 True，挡在推送之前。

    标题里只要有汉字就保留：中文标题配一段英文摘要的条目仍然看得懂；
    产品名、代码、币种符号是英文没关系。
    """
    if CJK_RE.search(title or ""):
        return False
    return chinese_ratio(f"{title or ''} {summary or ''}") < MIN_CHINESE_RATIO


def non_chinese_decision() -> Decision:
    return Decision(LOW, 0, "unknown", "纯英文内容，用户看不懂，挡在推送之前",
                    "non_chinese_filter", "语言")


def non_operational_decision() -> Decision:
    return Decision(LOW, 0, "unknown", "行业新闻/科普/访谈/活动，不是运营事件，挡在判定之前",
                    "non_operational_filter", "内容类型")


def format_message(*, exchange: str, title: str, published_cst: str, url: str,
                   decision: Decision, status: str, official: bool) -> str:
    """推送给用户看的文案。

    只写用户要的：哪家、出了什么事、多严重、什么时候、原文在哪。
    恢复时间只参与内部判级，不在消息里展示（用户不关心小币种多久恢复）。
    限额只有「卡住资金」的风险（仅允许小额提现、单日上限等）才展示；
    维护类限额只留在报告 json / state / 日志里。
    规则名、阶段编号、信号分类、命中文本一律不进 Telegram。
    """
    level_cn = {"high": "高", "medium": "中", "low": "低"}[decision.level]
    signal_type = "官方公告" if official else "用户端反馈（非官方公告）"
    lines = [
        f"【{level_cn}风险 · {status}】{exchange} · {signal_type}",
        f"标题：{title[:200]}",
        f"时间：{published_cst or '原文未给时间'}（北京时间）",
    ]
    if decision.risk_limit:
        lines.append(f"限额/门槛：{decision.risk_limit}")
    lines.append(f"原文：{url}")
    if not official:
        lines.append("说明：未在官方监控名单，仅线索")
    return "\n".join(lines)
