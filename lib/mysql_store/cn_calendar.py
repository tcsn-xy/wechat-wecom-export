"""中国 2026 年法定节假日 / 调休上班日的判定（纯 Python，无第三方依赖）。

数据来源
--------
[国务院办公厅关于 2026 年部分节假日安排的通知](https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm)
（国办发明电〔2025〕7 号，2025-11-04 发布）。原文逐条：

```
一、元旦：  1月1日(周四)至3日(周六)放假调休，共3天。1月4日(周日)上班。
二、春节：  2月15日(周日)至23日(周一)放假调休，共9天。2月14日(周六)、2月28日(周六)上班。
三、清明节：4月4日(周六)至6日(周一)放假，共3天。
四、劳动节：5月1日(周五)至5日(周二)放假调休，共5天。5月9日(周六)上班。
五、端午节：6月19日(周五)至21日(周日)放假，共3天。
六、中秋节：9月25日(周五)至27日(周日)放假，共3天。
七、国庆节：10月1日(周四)至7日(周三)放假调休，共7天。9月20日(周日)、10月10日(周六)上班。
```

口径
----
``workday`` = 周一~周五 **且** 不在法定假日里，**或** 是调休上班的周六周日。
``restday`` = 法定假日，**或** 未调休的周六周日。

想换一份日历（例如政策变了 / 换年份）用 ``--calendar xx.json`` 覆盖，
JSON 形如 ``{"holidays": {"2026-10-01": "国庆"}, "workdays": {"2026-10-10": "国庆调休"}}``。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Dict, Iterator, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 法定放假（这些日期即使落在周一~周五也算休息日）
HOLIDAYS_2026: Dict[str, str] = {}
for _d in ("2026-01-01", "2026-01-02", "2026-01-03"):
    HOLIDAYS_2026[_d] = "元旦"
for _i in range(15, 24):                       # 2/15 ~ 2/23
    HOLIDAYS_2026["2026-02-%02d" % _i] = "春节"
for _i in range(4, 7):                         # 4/4 ~ 4/6
    HOLIDAYS_2026["2026-04-%02d" % _i] = "清明"
for _i in range(1, 6):                         # 5/1 ~ 5/5
    HOLIDAYS_2026["2026-05-%02d" % _i] = "劳动节"
for _i in range(19, 22):                       # 6/19 ~ 6/21
    HOLIDAYS_2026["2026-06-%02d" % _i] = "端午"
for _i in range(25, 28):                       # 9/25 ~ 9/27
    HOLIDAYS_2026["2026-09-%02d" % _i] = "中秋"
for _i in range(1, 8):                         # 10/1 ~ 10/7
    HOLIDAYS_2026["2026-10-%02d" % _i] = "国庆"

# 调休上班（这些日期即使落在周六/周日也算工作日）
MAKEUP_WORKDAYS_2026: Dict[str, str] = {
    "2026-01-04": "元旦调休",
    "2026-02-14": "春节调休",
    "2026-02-28": "春节调休",
    "2026-05-09": "劳动节调休",
    "2026-09-20": "国庆调休",
    "2026-10-10": "国庆调休",
}

OVERRIDE_ENV = "WX_CN_CALENDAR"


def _load_override(path: Optional[str]) -> tuple:
    """读一份自定义日历；返回 (holidays, workdays)。失败就抛，不静默。"""
    if not path:
        path = os.environ.get(OVERRIDE_ENV) or ""
    if not path or not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    hol = {str(k): str(v) for k, v in (d.get("holidays") or {}).items()}
    wrk = {str(k): str(v) for k, v in (d.get("workdays") or {}).items()}
    return hol, wrk


class Calendar:
    """一年（默认 2026）的工作日/休息日判定。"""

    def __init__(self, holidays: Optional[Dict[str, str]] = None,
                 workdays: Optional[Dict[str, str]] = None,
                 override_file: Optional[str] = None, year: int = 2026):
        base_hol = dict(HOLIDAYS_2026) if year == 2026 else {}
        base_wrk = dict(MAKEUP_WORKDAYS_2026) if year == 2026 else {}
        oh, ow = _load_override(override_file)
        base_hol.update(oh)
        base_wrk.update(ow)
        if holidays:
            base_hol.update({str(k): str(v) for k, v in holidays.items()})
        if workdays:
            base_wrk.update({str(k): str(v) for k, v in workdays.items()})
        self.holidays = base_hol
        self.makeup = base_wrk
        self.year = year

    # ---------------------------------------------------------------- #
    def kind(self, d: _dt.date) -> str:
        """``"workday"`` 或 ``"restday"``。"""
        return "workday" if self.is_workday(d) else "restday"

    def is_workday(self, d: _dt.date) -> bool:
        key = d.isoformat()
        if key in self.makeup:          # 调休上班优先
            return True
        if key in self.holidays:        # 法定假日
            return False
        return d.weekday() < 5          # 周一~周五

    def reason(self, d: _dt.date) -> str:
        """人能读的原因，例如 ``"国庆调休(周六上班)"`` / ``"清明假期(周一)"``。"""
        key = d.isoformat()
        wd = WEEKDAY_NAMES[d.weekday()]
        if key in self.makeup:
            return "%s(%s上班)" % (self.makeup[key], wd)
        if key in self.holidays:
            return "%s假期(%s)" % (self.holidays[key], wd)
        if d.weekday() < 5:
            return wd
        return "%s休息" % wd

    def label(self, d: _dt.date) -> str:
        return "工作日" if self.is_workday(d) else "休息日"

    # ---------------------------------------------------------------- #
    def classify_range(self, start: _dt.date, end: _dt.date):
        """逐日给出 ``{date, kind, label, reason}``（含首含尾）。"""
        out = []
        cur = start
        while cur <= end:
            out.append({
                "date": cur.isoformat(),
                "weekday": WEEKDAY_NAMES[cur.weekday()],
                "kind": self.kind(cur),
                "label": self.label(cur),
                "reason": self.reason(cur),
            })
            cur += _dt.timedelta(days=1)
        return out

    def daterange(self, start: _dt.date, end: _dt.date) -> Iterator[_dt.date]:
        cur = start
        while cur <= end:
            yield cur
            cur += _dt.timedelta(days=1)


def calendar_json_template(year: int = 2026) -> str:
    """``--dump-calendar`` 用：吐出当前生效的日历，方便用户改。"""
    c = Calendar(year=year)
    return json.dumps({"year": year, "holidays": c.holidays,
                       "workdays": c.makeup},
                      ensure_ascii=False, indent=2)
