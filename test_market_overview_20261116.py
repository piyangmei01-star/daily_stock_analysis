# -*- coding: utf-8 -*-
"""市场全局概况接入（2026-11-16）本地确定性测试：
1) market_overview 优先复用 regime.breadth，字段齐全
2) regime 无 breadth 且 akshare 不可用时返回 None（不编造）
3) market_overview_text 格式确定、None 时明确降级
4) PROMPT_TMPL 拼接 {market_overview} 后 format 成功且文本进入 Prompt
不联网、不动真实归档、不动推送。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import market_regime as mr
import daily_ai_top10 as dai

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


# ---- 1. regime.breadth 齐全时直接复用 ----
regime = {"breadth": {"adv": 2345, "dec": 2567, "flat": 123, "amount_yi": 18976,
                      "snapshot_source": "eastmoney_clist"},
          "limits": {"zt": 45, "dt": 12},
          "turnover": {"ratio": 0.857}}
ov = mr.market_overview(regime)
check("复用 breadth: adv", ov and ov["adv"] == 2345)
check("复用 breadth: amount_yi", ov and ov["amount_yi"] == 18976)
check("复用 breadth: 涨停/跌停", ov and ov["zt"] == 45 and ov["dt"] == 12)
check("复用 breadth: 环比", ov and ov["turnover_ratio"] == 0.857)
check("复用 breadth: 来源标注", ov and ov["source"] == "eastmoney_clist")

# ---- 2. regime 无 breadth + akshare 置空 → None（不编造）----
_real_ak = mr.ak
try:
    mr.ak = None
    ov2 = mr.market_overview({"breadth": {"error": "MarketDataError"}})
    check("兜底失败返回 None", ov2 is None)
    check("None 文本明确降级", "数据不足" in mr.market_overview_text(ov2)
          and "不编造" in mr.market_overview_text(ov2))
finally:
    mr.ak = _real_ak

# ---- 3. 文本格式确定 ----
txt = mr.market_overview_text(ov)
check("文本含成交额", "两市总成交额 18976亿" in txt)
check("文本含涨跌家数", "上涨 2345家/下跌 2567家" in txt)
check("文本含来源", "来源: eastmoney_clist" in txt)

# ---- 4. Prompt 拼接 ----
snap = json.dumps({"pct": 5.2})
row = (None, None, None, None, "600498", "烽火通信", 1, 88,
       json.dumps({"trend": 30}), "测试理由", "测试风险", 26.35, snap,
       "通信设备", None, None, None)
prompt = dai.PROMPT_TMPL.format(
    name=row[5], code=row[4], industry="通信设备", price=row[11],
    pct=json.loads(snap).get("pct"), score=row[7], scores=row[8],
    reason="测试理由", risk_tags="测试风险", snapshot=snap, market_overview=txt)
check("Prompt 含市场环境段", "市场环境: " + txt in prompt)
check("Prompt 含成交额数值", "18976亿" in prompt)
check("Prompt 模板仍含五选一要求", "看多 / 偏多 / 观望 / 偏空 / 高风险" in prompt)
check("analyze_one 签名含 market_text", "market_text" in dai.analyze_one.__code__.co_varnames)

print(f"\n结果: PASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
