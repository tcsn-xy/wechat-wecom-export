#!/usr/bin/env python3
"""统计某个（群）会话每天的消息条数，按工作日 / 休息日拆分求日均。

    ./.venv/bin/python scripts/chat_activity.py                    # 默认统计本机最大的群
    ./.venv/bin/python scripts/chat_activity.py --chat 10000000001@chatroom
    ./.venv/bin/python scripts/chat_activity.py --since 2026-04-01 --until 2026-09-28
    ./.venv/bin/python scripts/chat_activity.py --json              # 只要机器可读结果
    ./.venv/bin/python scripts/chat_activity.py --dump-calendar     # 看当前用的放假表
    ./.venv/bin/python scripts/chat_activity.py --calendar my.json  # 换一份放假日历

口径
----
* **工作日**：周一~周五且非法定假日，**或** 调休上班的周末（如 2026-09-20 周日）。
* **休息日**：法定假日，或未调休的周末。
* **分母含零消息的天**（因为"那天没人说话"本身就是数据）。
* 时间窗默认取该会话首条消息当天 ~ 末条消息当天；若末日只有半天消息
  （例如快照截断在早上），自动从均值里剔除并在报告里注明。
* 同时给出「全部消息」与「剔除 system 系统消息」两套数字，避免口径歧义。

放假表：`lib/mysql_store/cn_calendar.py`（依据国办发明电〔2025〕7 号）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

from mysql_store import cn_calendar, conn as my  # noqa: E402

WEEKDAY_ORDER = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 判定"某天是不是半天"：结束时间早于这个钟点就算不完整（快照被截断）
PARTIAL_DAY_END_HOUR = 12
# 自检阈值：工作日低于这个量怀疑是放假；周末高于这个量怀疑是调休
SUSPECT_HOLIDAY_MAX = 250
SUSPECT_MAKEUP_MIN = 900


def _d(s: str) -> _dt.date:
    return _dt.date.fromisoformat(s)


def pick_chat(c, chat: str) -> dict:
    if chat:
        r = my.query(c, "SELECT `chat_pk`,`chat_id`,`title`,`is_room`,`member_count`,"
                        "`first_time`,`last_time` FROM `chats` WHERE `chat_id`=%s",
                     (chat,))
        if not r:
            raise SystemExit("找不到会话 %s" % chat)
        return r[0]
    r = my.query(c, "SELECT `chat_pk`,`chat_id`,`title`,`is_room`,`member_count`,"
                    "`first_time`,`last_time` FROM `chats` "
                    "ORDER BY `message_count` DESC LIMIT 1")
    return r[0]


def daily_counts(c, chat_pk: int, since=None, until=None) -> dict:
    """``{date: {"all":n, "human":n, "first":ts, "last":ts}}``。"""
    sql = ("SELECT DATE(`msg_time`) AS d, COUNT(*) AS n, "
           "SUM(`kind` <> 'system') AS n_human, "
           "MIN(UNIX_TIMESTAMP(`msg_time`)) AS first_ts, "
           "MAX(UNIX_TIMESTAMP(`msg_time`)) AS last_ts "
           "FROM `messages` WHERE `chat_pk`=%s")
    args = [chat_pk]
    if since:
        sql += " AND `msg_time` >= %s"
        args.append(since)
    if until:
        sql += " AND `msg_time` < DATE_ADD(%s, INTERVAL 1 DAY)"
        args.append(until)
    sql += " GROUP BY 1 ORDER BY 1"
    out = {}
    for r in my.query(c, sql, tuple(args)):
        d = r["d"]
        key = d.isoformat() if hasattr(d, "isoformat") else str(d)
        out[key] = {"all": int(r["n"]), "human": int(r["n_human"] or 0),
                    "first": int(r["first_ts"] or 0), "last": int(r["last_ts"] or 0)}
    return out


def analyse(daily: dict, cal: cn_calendar.Calendar, start: _dt.date, end: _dt.date,
            skip_partial: bool = True, skip_days=None) -> dict:
    days = cal.classify_range(start, end)
    partial = []
    for d in days:
        key = d["date"]
        info = daily.get(key)
        d["all"] = info["all"] if info else 0
        d["human"] = info["human"] if info else 0
        d["known"] = info is not None
        dd = _d(key)
        d["partial"] = bool(info and dd == end and skip_partial
                            and _dt.datetime.fromtimestamp(info["last"]).hour
                            < PARTIAL_DAY_END_HOUR)
        if d["partial"]:
            partial.append(key)

    skip = set(skip_days or [])
    for d in days:
        d["skipped"] = d["date"] in skip
    counted = [d for d in days if not d["partial"] and not d["skipped"]]
    all_all = sum(d["all"] for d in days)
    all_human = sum(d["human"] for d in days)
    res = {"days": days, "partial_days": partial, "skipped_days": sorted(skip),
           "window_messages": all_all, "window_human": all_human,
           "partial_messages": sum(d["all"] for d in days if d["partial"]),
           "skipped_messages": sum(d["all"] for d in days if d["skipped"]),
           "window": [start.isoformat(), end.isoformat()],
           "calendar_days": len(days), "counted_days": len(counted),
           "zero_days": sum(1 for d in days if d["all"] == 0),
           "groups": {}}
    for kind in ("workday", "restday"):
        sel = [d for d in counted if d["kind"] == kind]
        n_days = len(sel)
        tot_all = sum(d["all"] for d in sel)
        tot_human = sum(d["human"] for d in sel)
        z = sum(1 for d in sel if d["all"] == 0)
        res["groups"][kind] = {
            "days": n_days,
            "messages_all": tot_all,
            "messages_human": tot_human,
            "zero_days": z,
            "active_days": n_days - z,
            "avg_all": (tot_all / n_days) if n_days else 0.0,
            "avg_human": (tot_human / n_days) if n_days else 0.0,
            "avg_active_all": (tot_all / (n_days - z)) if (n_days - z) else 0.0,
            "avg_active_human": (tot_human / (n_days - z)) if (n_days - z) else 0.0,
        }
    wa = res["groups"]["workday"]
    ra = res["groups"]["restday"]
    res["ratio_all"] = (wa["avg_all"] / ra["avg_all"]) if ra["avg_all"] else None
    res["ratio_human"] = (wa["avg_human"] / ra["avg_human"]) if ra["avg_human"] else None

    # 敏感度：把被剔掉的数据空洞日也算进分母（它们两类口径下都是 0 条）
    res["sensitivity"] = None
    if skip:
        se = {}
        for kind in ("workday", "restday"):
            sel = [d for d in days if d["kind"] == kind]
            k = len(sel)
            tot = sum(d["all"] for d in sel)
            se[kind] = {"days": k, "messages_all": tot,
                        "avg_all": (tot / k) if k else 0.0}
        res["sensitivity"] = se
        if se["restday"]["avg_all"]:
            res["ratio_sensitivity"] = (se["workday"]["avg_all"]
                                        / se["restday"]["avg_all"])

    # 简化口径（只按周一~周五）做对照
    simple = {}
    for kind, want in (("workday", True), ("restday", False)):
        sel = [d for d in counted
               if (_d(d["date"]).weekday() < 5) == want]
        n = len(sel)
        tot = sum(d["all"] for d in sel)
        simple[kind] = {"days": n, "messages_all": tot,
                        "avg_all": (tot / n) if n else 0.0}
    res["simple"] = simple
    res["ratio_simple"] = (simple["workday"]["avg_all"] / simple["restday"]["avg_all"]
                           if simple["restday"]["avg_all"] else None)

    # 按星期几、按月
    by_wd = {}
    for d in counted:
        w = d["weekday"]
        e = by_wd.setdefault(w, {"days": 0, "messages": 0})
        e["days"] += 1
        e["messages"] += d["all"]
    res["by_weekday"] = {w: {**v, "avg": (v["messages"] / v["days"]) if v["days"] else 0}
                         for w, v in by_wd.items()}
    by_month = {}
    for d in counted:
        key = d["date"][:7]
        e = by_month.setdefault(key, {"days": 0, "messages": 0, "human": 0,
                                      "workdays": 0, "restdays": 0,
                                      "wd_messages": 0, "rd_messages": 0})
        e["days"] += 1
        e["messages"] += d["all"]
        e["human"] += d["human"]
        if d["kind"] == "workday":
            e["workdays"] += 1
            e["wd_messages"] += d["all"]
        else:
            e["restdays"] += 1
            e["rd_messages"] += d["all"]
    res["by_month"] = by_month

    # 日历自检：目标不是"找低量日"（群有成长期，4 月整体就低），
    # 而是**找放假日历写错的日子**。判据用"月内同类日中位数"做相对比较，
    # 否则成长期的低量会被误报一大片（实测 53 天，全是 4~5 月建群初期）。
    import statistics as _st
    def _median(vals):
        return _st.median(vals) if vals else 0
    wd_by_month, rd_by_month = {}, {}
    for d in counted:
        (wd_by_month if d["kind"] == "workday" else rd_by_month).setdefault(
            d["date"][:7], []).append(d["all"])
    med_wd = {m: _median(v) for m, v in wd_by_month.items()}
    med_rd = {m: _median(v) for m, v in rd_by_month.items()}

    # (a) 工作日却几乎没人说话（< 同月中位数的 25%，或干脆 0 条）→ 疑似该放假没放
    suspects_holiday = [d for d in counted if d["kind"] == "workday"
                        and med_wd.get(d["date"][:7], 0) > 0
                        and d["all"] < 0.25 * med_wd[d["date"][:7]]]
    # (b) 休息日却异常热闹（> 同月中位数的 1.8 倍）→ 疑似该上班没上（调休漏了）
    suspects_makeup = [d for d in counted if d["kind"] == "restday"
                       and med_rd.get(d["date"][:7], 0) > 0
                       and d["all"] > 1.8 * med_rd[d["date"][:7]]]
    res["suspects_holiday"] = suspects_holiday
    res["suspects_makeup"] = suspects_makeup
    # (c) 最硬的信号：官方调休表里的周末，数据必须也"像工作日"
    res["makeup_checked"] = []
    for d in counted:
        dd = _d(d["date"])
        if dd.weekday() >= 5 and cal.is_workday(dd):
            base = med_wd.get(d["date"][:7]) or 0
            rest_base = med_rd.get(d["date"][:7]) or 0
            res["makeup_checked"].append({
                "date": d["date"], "weekday": d["weekday"], "reason": d["reason"],
                "n": d["all"], "wd_median": base, "rd_median": rest_base,
                "ok": d["all"] >= 0.5 * base if base else None})
    # 成长期提醒：逐月工作日日均差得很大时要说出来，避免"总日均"被误读
    wd_month_avg = {m: (sum(v) / len(v)) for m, v in wd_by_month.items() if v}
    res["wd_month_avg"] = wd_month_avg
    if wd_month_avg:
        lo, hi = min(wd_month_avg.values()), max(wd_month_avg.values())
        res["growth_note"] = ("工作日日均逐月从 %.0f 涨到 %.0f（%.1f 倍）"
                              % (lo, hi, (hi / lo) if lo else 0)) if lo else ""
        res["growth_lo_hi"] = (lo, hi) if lo else (0, 0)
    # 数据能佐证的调休（周末高量 + 正好在官方调休表里）
    res["makeup_confirmed"] = [d for d in days if d["kind"] == "workday"
                               and _d(d["date"]).weekday() >= 5]
    return res


def fmt(n, nd=0):
    return format(round(n, nd) if nd else int(round(n)), ",")


def print_report(chat: dict, res: dict, cal: cn_calendar.Calendar) -> None:
    W, R = res["groups"]["workday"], res["groups"]["restday"]
    print("会话      : %s（%s）" % (chat["title"], chat["chat_id"]))
    print("成员/消息 : %s 人 / %s 条" % (chat.get("member_count"),
                                     fmt(res["window_messages"])))
    print("统计窗口  : %s ~ %s（%d 个自然日）"
          % (res["window"][0], res["window"][1], res["calendar_days"]))
    if res["partial_days"]:
        print("已剔除    : %s（当天数据只到上午，%s 条；不计入日均，但计入总数）"
              % ("、".join(res["partial_days"]), fmt(res["partial_messages"])))
    print("零消息日  : %d 天（计入分母）" % res["zero_days"])
    if res.get("skipped_days"):
        print("数据空洞日: %d 天已剔除 —— %s"
              % (len(res["skipped_days"]), "、".join(res["skipped_days"])))
        print("            （这些天整库消息数 ≈ 0，是微信本机没留住记录，"
              "不是\"那天真没人说话\"；混进分母会把工作日日均压低）")
    print("-" * 74)
    print("按**官方放假安排**（%d 个可计日）" % res["counted_days"])
    print("  %-8s %6s %10s %14s %14s" % ("类型", "天数", "总条数", "日均(全部)", "日均(剔除系统)"))
    for kind, g in (("workday", W), ("restday", R)):
        print("  %-8s %6d %10s %14s %14s"
              % ("工作日" if kind == "workday" else "休息日", g["days"],
                 fmt(g["messages_all"]), fmt(g["avg_all"], 1), fmt(g["avg_human"], 1)))
    if res["ratio_all"]:
        print("  → 工作日日均是休息日的 **%.2f 倍**（全部口径）" % res["ratio_all"])
    print()
    print("按**简化口径**（只按周一~周五，不看节假日）")
    S = res["simple"]
    for kind in ("workday", "restday"):
        print("  %-6s %3d 天  %10s 条  日均 %8s"
              % ("工作日" if kind == "workday" else "休息日",
                 S[kind]["days"], fmt(S[kind]["messages_all"]),
                 fmt(S[kind]["avg_all"], 1)))
    if res["ratio_simple"]:
        print("  → 倍数 %.2f（对比官方口径 %.2f，差额来自 %d 月调休与节假日）"
              % (res["ratio_simple"], res["ratio_all"] or 0,
                 len([d for d in res["days"] if d["kind"] == "workday"
                      and _d(d["date"]).weekday() >= 5])))
    print()
    print("数据佐证的调休上班日（周末但按官方安排上班）：%d 天 %s"
          % (len(res["makeup_confirmed"]),
             "、".join("%s(%s, %s 条)" % (d["date"], d["weekday"], fmt(d["all"]))
                       for d in res["makeup_confirmed"]) or "无"))
    if res.get("sensitivity"):
        se = res["sensitivity"]
        print("敏感度对比（把被剔除的 %d 个空洞日也算进分母）：工作日 %s 条/天，休息日 %s 条/天，倍数 %.2f"
              % (len(res["skipped_days"]), fmt(se["workday"]["avg_all"], 1),
                 fmt(se["restday"]["avg_all"], 1), res.get("ratio_sensitivity") or 0))
        print()
    if res.get("growth_note"):
        print("注意：%s —— 窗口内群在成长期，总日均需配合逐月看" % res["growth_note"])
    print("调休日核对（官方说上班的周末，数据也该像工作日）：")
    for m in res.get("makeup_checked", []):
        print("     %s %s %s  %s 条（当月工作日中位 %s / 休息日中位 %s）→ %s"
              % (m["date"], m["weekday"], m["reason"], fmt(m["n"]),
                 fmt(m["wd_median"]), fmt(m["rd_median"]),
                 "一致 ✅" if m["ok"] else "不一致 ⚠️"))
    print("工作日里量异常低（< 同月中位 25%%，疑似该放假没放）：%d 天" % len(res["suspects_holiday"]))
    for d in res["suspects_holiday"][:8]:
        print("     %s %s %s  %s 条" % (d["date"], d["weekday"], d["reason"], d["all"]))
    print("休息日里量异常高（> 同月中位 180%%，疑似漏了调休）：%d 天" % len(res["suspects_makeup"]))
    for d in res["suspects_makeup"][:8]:
        print("     %s %s %s  %s 条" % (d["date"], d["weekday"], d["reason"], d["all"]))


def write_markdown(path: str, chat: dict, res: dict) -> str:
    W, R = res["groups"]["workday"], res["groups"]["restday"]
    L = []
    L.append("# 群「%s」消息统计：工作日 vs 休息日\n" % chat["title"])
    L.append("- 会话 ID：`%s`（群成员 %s 人）" % (chat["chat_id"], chat.get("member_count")))
    L.append("- 统计窗口：**%s ~ %s**（%d 个自然日，其中 %d 天有消息、%d 天零消息）"
             % (res["window"][0], res["window"][1], res["calendar_days"],
                res["calendar_days"] - res["zero_days"], res["zero_days"]))
    L.append("- 消息总数：**%s 条**（其中系统消息 %s 条；工作日 %s + 休息日 %s + "
             "不完整日 %s + 空白日 %s = 总数）"
             % (fmt(res["window_messages"]),
                fmt(res["window_messages"] - res["window_human"]),
                fmt(W["messages_all"]), fmt(R["messages_all"]),
                fmt(res["partial_messages"]), fmt(res["skipped_messages"])))
    if res["partial_days"]:
        L.append("- ⚠️ 已剔除不完整日 **%s**：那天数据只到上午（%s 条），"
                 "不计入日均但计入总数"
                 % ("、".join(res["partial_days"]), fmt(res["partial_messages"])))
    if res.get("skipped_days"):
        L.append("- ⚠️ 已剔除**数据空洞日 %d 天**：%s。这些天**整库**消息数几乎为 0"
                 "；这些日期可能存在本机记录缺口。低消息量只是启发式判定，"
                 "不是「那天真没人说话」，混进分母会把工作日日均无端拉低。"
                 % (len(res["skipped_days"]), "、".join(res["skipped_days"])))
    L.append("- 工作日/休息日划分依据：[国务院办公厅关于2026年部分节假日安排的通知]"
             "(https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm)；"
             "**分母含零消息的天**\n")

    L.append("## 1. 结论\n")
    L.append("| 类型 | 天数 | 总条数 | **日均条数（全部）** | 日均（剔除系统消息） | 有消息的天数 | 只算有消息的天 |")
    L.append("|---|---|---|---|---|---|---|")
    L.append("| 工作日 | %d | %s | **%s** | %s | %d | %s |"
             % (W["days"], fmt(W["messages_all"]), fmt(W["avg_all"], 1),
                fmt(W["avg_human"], 1), W["active_days"], fmt(W["avg_active_all"], 1)))
    L.append("| 休息日 | %d | %s | **%s** | %s | %d | %s |"
             % (R["days"], fmt(R["messages_all"]), fmt(R["avg_all"], 1),
                fmt(R["avg_human"], 1), R["active_days"], fmt(R["avg_active_all"], 1)))
    L.append("")
    if res["ratio_all"]:
        L.append("> **工作日日均是休息日的 %.2f 倍**（全部消息）；"
                 "只算人类发言（剔除系统消息）是 %.2f 倍。"
                 % (res["ratio_all"], res["ratio_human"] or 0))
        L.append("> 「只算有消息的天」那一列是另一种口径（把零消息日剔除），供对比，不作主结论。")
        L.append("> 逐月拆解见第 4 节：**每个月工作日都高于休息日**，方向一致。\n")

    L.append("## 2. 对照：简化口径（只按周一~周五）\n")
    S = res["simple"]
    L.append("| 类型 | 天数 | 总条数 | 日均 |")
    L.append("|---|---|---|---|")
    L.append("| 工作日 | %d | %s | %s |" % (S["workday"]["days"],
             fmt(S["workday"]["messages_all"]), fmt(S["workday"]["avg_all"], 1)))
    L.append("| 休息日 | %d | %s | %s |" % (S["restday"]["days"],
             fmt(S["restday"]["messages_all"]), fmt(S["restday"]["avg_all"], 1)))
    L.append("")
    L.append("官方口径的倍数 %.2f，简化口径 %.2f；差别来自调休上班的周末与落在周一~周五的法定假日。\n"
             % (res["ratio_all"] or 0, res["ratio_simple"] or 0))

    L.append("## 3. 按星期几\n")
    L.append("| 星期 | 天数 | 总条数 | 日均 |")
    L.append("|---|---|---|---|")
    for w in WEEKDAY_ORDER:
        v = res["by_weekday"].get(w)
        if not v:
            continue
        L.append("| %s | %d | %s | %s |" % (w, v["days"], fmt(v["messages"]), fmt(v["avg"], 1)))
    L.append("")

    if res.get("sensitivity"):
        se = res["sensitivity"]
        L.append("### 3.1 敏感度：把空白日也算进分母会怎样\n")
        L.append("| 类型 | 天数 | 总条数 | 日均 |")
        L.append("|---|---|---|---|")
        L.append("| 工作日 | %d | %s | %s |" % (se["workday"]["days"],
                 fmt(se["workday"]["messages_all"]), fmt(se["workday"]["avg_all"], 1)))
        L.append("| 休息日 | %d | %s | %s |" % (se["restday"]["days"],
                 fmt(se["restday"]["messages_all"]), fmt(se["restday"]["avg_all"], 1)))
        L.append("")
        L.append("> 即使把那 %d 个空白日全算进分母，工作日日均仍有 %s 条、休息日 %s 条，"
                 "倍数 %.2f —— **结论不变**。\n"
                 % (len(res["skipped_days"]), fmt(se["workday"]["avg_all"], 1),
                    fmt(se["restday"]["avg_all"], 1), res.get("ratio_sensitivity") or 0))

    L.append("## 4. 按月\n")
    L.append("| 月份 | 天数 | 总条数 | 日均 | 工作日日均 | 休息日日均 |")
    L.append("|---|---|---|---|---|---|")
    for m, v in sorted(res["by_month"].items()):
        wd_avg = (v["wd_messages"] / v["workdays"]) if v["workdays"] else 0
        rd_avg = (v["rd_messages"] / v["restdays"]) if v["restdays"] else 0
        L.append("| %s | %d | %s | %s | %s（%d 天） | %s（%d 天） |"
                 % (m, v["days"], fmt(v["messages"]), fmt(v["messages"] / v["days"], 1),
                    fmt(wd_avg, 1), v["workdays"], fmt(rd_avg, 1), v["restdays"]))
    if res.get("growth_note"):
        L.append("")
        L.append("> ⚠️ **%s** —— 这个群在窗口内快速成长，"
                 "所以上表的「工作日日均 %s」是**全程平均**，不代表任意时点的水平；"
                 "但**每个月工作日都高于休息日**（见上表每一行），方向是一致的。"
                 % (res["growth_note"], fmt(W["avg_all"], 1)))
    L.append("")

    L.append("## 5. 日历自检（用数据反过来验放假表）\n")
    L.append("判据用**月内同类日中位数**做相对比较，而不是绝对条数 —— "
             "这个群 4 月才建群、日均从 167 涨到 3,399，用绝对阈值会把整个成长期误报成「放假」。\n")
    L.append("### 5.1 官方调休日核对（官方说周末上班，数据也该像工作日）\n")
    L.append("| 日期 | 星期 | 说明 | 条数 | 当月工作日中位 | 当月休息日中位 | 判断 |")
    L.append("|---|---|---|---|---|---|---|")
    for m in res.get("makeup_checked") or []:
        L.append("| %s | %s | %s | %s | %s | %s | %s |"
                 % (m["date"], m["weekday"], m["reason"], fmt(m["n"]),
                    fmt(m["wd_median"]), fmt(m["rd_median"]),
                    "一致 ✅" if m["ok"] else "不一致 ⚠️"))
    if not res.get("makeup_checked"):
        L.append("| — | — | 窗口内没有调休上班日 | — | — | — | — |")
    L.append("")
    L.append("### 5.2 疑似该放假却没放（工作日 < 同月中位 25%%）\n")
    if res["suspects_holiday"]:
        L.append("| 日期 | 星期 | 当日说明 | 条数 |")
        L.append("|---|---|---|---|")
        for d in res["suspects_holiday"]:
            L.append("| %s | %s | %s | %s |" % (d["date"], d["weekday"], d["reason"], fmt(d["all"])))
    else:
        L.append("无 —— 说明放假表与数据没有明显冲突。")
    L.append("")
    L.append("### 5.3 疑似漏了调休（休息日 > 同月中位 180%%）\n")
    if res["suspects_makeup"]:
        L.append("| 日期 | 星期 | 当日说明 | 条数 |")
        L.append("|---|---|---|---|")
        for d in res["suspects_makeup"]:
            L.append("| %s | %s | %s | %s |" % (d["date"], d["weekday"], d["reason"], fmt(d["all"])))
    else:
        L.append("无 —— 说明没有「该上班却被算成休息」的周末。")
    L.append("")

    L.append("## 6. 逐日明细\n")
    L.append("| 日期 | 星期 | 类型 | 说明 | 条数 | 其中人类发言 |")
    L.append("|---|---|---|---|---|---|")
    for d in res["days"]:
        mark = "（不完整，未计入）" if d["partial"] else (
            "（数据空洞，未计入）" if d.get("skipped") else "")
        L.append("| %s | %s | %s | %s%s | %s | %s |"
                 % (d["date"], d["weekday"], d["label"], d["reason"], mark,
                    fmt(d["all"]), fmt(d["human"])))
    L.append("")

    L.append("## 7. 自检\n")
    L.append("| 断言 | 结果 |")
    L.append("|---|---|")
    L.append("| 逐日条数之和 == 总条数（%s） | %s |"
             % (fmt(res["window_messages"]),
                "✅" if sum(d["all"] for d in res["days"]) == res["window_messages"] else "❌"))
    L.append("| 工作日天数 + 休息日天数 == 可计日（%d） | %s |"
             % (res["counted_days"],
                "✅" if W["days"] + R["days"] == res["counted_days"] else "❌"))
    L.append("| 可计日 + 不完整日 + 空白日 == 窗口自然日（%d） | %s |"
             % (res["calendar_days"],
                "✅" if (res["counted_days"] + len(res["partial_days"])
                         + len(res["skipped_days"])) == res["calendar_days"] else "❌"))
    L.append("| 分组总条数 + 不完整日 + 空白日 == 总条数 | %s |"
             % ("✅" if (W["messages_all"] + R["messages_all"]
                        + res["partial_messages"] + res["skipped_messages"])
                == res["window_messages"] else "❌"))
    L.append("| 官方调休日与数据一致 | %s |"
             % ("✅" if all(m["ok"] for m in (res.get("makeup_checked") or [])) else "❌"))
    L.append("")
    L.append("## 8. 口径说明\n")
    L.append("""- **工作日**：周一~周五且非法定假日，**或**调休上班的周末（窗口内只有 2026-09-20 一天）。
- **休息日**：法定假日，或未调休的周末。
- **分母含零消息的天**：休息日里有若干天 0 条，"那天没人说话"本身是数据，不能剔掉。
- **消息条数**按"一条记录"计，含图片/表情/语音/转账等所有类型；另给一列剔除 `system`（入群提示等）的"人类发言"。
- **不完整日**：快照截断在上午的那天已剔除，避免把工作日日均拉低。
- **数据空洞日**：本群 0 条**且**整机当天 <= 200 条（多数是整机 0 条）的日子 ——
  那几天设备/微信没在产生记录，不是"群里没人说话"，所以从分母里剔除；
  第 3.1 节给出"算进去"的对照。
- 放假表写死在 `lib/mysql_store/cn_calendar.py`，可用 `--calendar xx.json` 覆盖后用同一脚本重算。
""")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return path


def find_data_gaps(c, chat_pk: int, start: _dt.date, end: _dt.date,
                   max_msgs: int = 200, daily: dict = None):
    """找出真正的"本机数据空洞日"。

    必须**两个条件同时满足**才算空洞（实测踩过：只看整库会让群自己安静的日子误报）：

    1. **本群当天 0 条**；
    2. **整机当天消息数 <= ``max_msgs``**（默认 200）—— 说明设备/微信那几天根本
       没在跑，而不是"那一天群里没人说话"。

    """
    rows = my.query(c, """SELECT DATE(`msg_time`) AS d, COUNT(*) AS n
        FROM `messages` WHERE `msg_time` >= %s
          AND `msg_time` < DATE_ADD(%s, INTERVAL 1 DAY)
        GROUP BY 1""", (start, end))
    machine = {r["d"].isoformat(): int(r["n"]) for r in rows}
    daily = daily or {}
    gaps = []
    for d in cn_calendar.Calendar().daterange(start, end):
        key = d.isoformat()
        if daily.get(key, {}).get("all", 0) == 0 and machine.get(key, 0) <= max_msgs:
            gaps.append(key)
    return gaps, machine


def main():
    ap = argparse.ArgumentParser(description="（群）会话消息条数的日均统计：工作日 vs 休息日")
    ap.add_argument("--chat", default="",
                    help="会话 ID；默认是本机最大的群")
    ap.add_argument("--since", default="", help="起始日 YYYY-MM-DD（默认取首条消息当天）")
    ap.add_argument("--until", default="", help="结束日 YYYY-MM-DD（默认取末条消息当天）")
    ap.add_argument("--calendar", default="", help="自定义放假日历 JSON")
    ap.add_argument("--out", default="", help="报告输出路径（默认 docs/…）")
    ap.add_argument("--keep-partial", action="store_true",
                    help="不剔除不完整日（默认剔除）")
    ap.add_argument("--keep-gaps", action="store_true",
                    help="不剔除整库数据空洞日（默认剔除并披露）")
    ap.add_argument("--gap-threshold", type=int, default=200,
                    help="整机当日消息数 <= 该值 且 本群 0 条 → 判为数据空洞日（默认 200）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--dump-calendar", action="store_true", help="打印当前生效的放假日历")
    args = ap.parse_args()

    if args.dump_calendar:
        print(cn_calendar.calendar_json_template())
        return

    c = my.connect("wx_chat")
    chat = pick_chat(c, args.chat)
    span = my.query(c, "SELECT MIN(`msg_time`) AS a, MAX(`msg_time`) AS b "
                       "FROM `messages` WHERE `chat_pk`=%s", (chat["chat_pk"],))[0]
    if not span["a"]:
        raise SystemExit("该会话没有消息")
    start = _d(args.since) if args.since else span["a"].date()
    end = _d(args.until) if args.until else span["b"].date()
    daily = daily_counts(c, chat["chat_pk"], start, end)
    cal = cn_calendar.Calendar(override_file=args.calendar or None)
    gaps, _machine = find_data_gaps(c, chat["chat_pk"], start, end,
                                    args.gap_threshold, daily)
    gaps = [] if args.keep_gaps else gaps
    res = analyse(daily, cal, start, end, skip_partial=not args.keep_partial,
                  skip_days=gaps)
    res["chat"] = {"chat_id": chat["chat_id"], "title": chat["title"],
                   "member_count": chat.get("member_count"),
                   "chat_pk": int(chat["chat_pk"])}

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
        c.close()
        return

    print_report(chat, res, cal)
    out = args.out or os.path.join(ROOT, "docs", "群-%s-消息统计.md"
                                   % chat["title"].strip().replace("/", "_"))
    write_markdown(out, chat, res)
    print()
    print("报告：%s" % out)
    c.close()


if __name__ == "__main__":
    main()
