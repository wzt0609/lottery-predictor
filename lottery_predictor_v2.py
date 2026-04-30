#!/usr/bin/env python3
"""
彩票预测 V2 — 约束筛选 + 随机采样法
与 V1（穷举打分法）采用完全不同的方法论。

核心思路：
  1. 从历史数据中提取约束条件（活跃区、和值、跨度、奇偶、大小）
  2. 用约束过滤出合理的候选号码池
  3. 从池中加权随机采样，用日期作为种子确保当日可重现
  4. 输出 TOP3，与 V1 形成对照
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import itertools
import json
import math
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data_v2"
REPORT_DIR = ROOT / "reports"
CONFIG_PATH = ROOT / "config_v2.json"

LOTTERIES = {
    "fc3d": {
        "name": "中国福利彩票 3D", "digits": 3,
        "sources": [
            {"type": "touch_history", "url": "https://touch.17500.cn/award/history/lotid/3d.html"},
            {"type": "plain_text", "url": "http://data.17500.cn/3d_asc.txt"},
            {"type": "cwl_json", "url": "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice?name=3d&issueCount=200"},
        ],
    },
    "pls": {
        "name": "中国体育彩票 排列三", "digits": 3,
        "sources": [
            {"type": "touch_history", "url": "https://touch.17500.cn/award/history/lotid/pl3.html"},
            {"type": "plain_text", "url": "http://data.17500.cn/pl3_asc.txt"},
            {"type": "lottery_gov_history", "url": "https://www.lottery.gov.cn/historykj/history.jspx?_ltype=pls"},
        ],
    },
    "plw": {
        "name": "中国体育彩票 排列五", "digits": 5,
        "sources": [
            {"type": "touch_history", "url": "https://touch.17500.cn/award/history/lotid/pl5.html"},
            {"type": "plain_text", "url": "http://data.17500.cn/pl5_asc.txt"},
            {"type": "lottery_gov_history", "url": "https://www.lottery.gov.cn/historykj/history.jspx?_ltype=plw"},
        ],
    },
}

DEFAULT_CONFIG = {
    "history_limit": 220, "candidate_count": 20,
    "request_timeout_seconds": 15,
    "user_agent": "Mozilla/5.0 (compatible; LotteryV2/1.0)",
    "lotteries": ["fc3d", "pls", "plw"],
    "hot_window": 15, "warm_window": 30, "sample_rounds": 10,
}

SAMPLE_HISTORY = {
    "fc3d": [("2026101","2026-04-22","058"),("2026102","2026-04-23","314"),("2026103","2026-04-24","769"),("2026104","2026-04-25","206"),("2026105","2026-04-26","482"),("2026106","2026-04-27","137"),("2026107","2026-04-28","590"),("2026108","2026-04-29","826"),("2026109","2026-04-30","641"),("2026110","2026-05-01","275")],
    "pls": [("2026101","2026-04-22","927"),("2026102","2026-04-23","164"),("2026103","2026-04-24","503"),("2026104","2026-04-25","788"),("2026105","2026-04-26","219"),("2026106","2026-04-27","456"),("2026107","2026-04-28","830"),("2026108","2026-04-29","372"),("2026109","2026-04-30","695"),("2026110","2026-05-01","041")],
    "plw": [("2026101","2026-04-22","92713"),("2026102","2026-04-23","16480"),("2026103","2026-04-24","50326"),("2026104","2026-04-25","78841"),("2026105","2026-04-26","21975"),("2026106","2026-04-27","45603"),("2026107","2026-04-28","83062"),("2026108","2026-04-29","37294"),("2026109","2026-04-30","69518"),("2026110","2026-05-01","04157")],
}


@dataclass(frozen=True)
class Draw:
    issue: str
    date: str
    numbers: tuple[int, ...]


class TextTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._row: list[str] = []
        self.rows: list[list[str]] = []
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"td", "th"}:
            self._in_cell = True; self._cell_parts = []
    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td","th"} and self._in_cell:
            self._row.append(" ".join("".join(self._cell_parts).split())); self._in_cell = False
        elif tag == "tr" and any(self._row):
            self.rows.append(self._row); self._row = []
    def handle_data(self, data: str) -> None:
        if self._in_cell: self._cell_parts.append(data)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)
    merged = dict(DEFAULT_CONFIG); merged.update(config)
    return merged

def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def fetch_text(url: str, timeout: int, ua: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Referer": urllib.parse.urljoin(url, "/"), "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        ct = resp.headers.get("Content-Type","")
        enc = "utf-8"
        m = re.search(r"charset=([\w-]+)", ct, re.I)
        if m: enc = m.group(1)
        try: return raw.decode(enc, errors="strict")
        except UnicodeDecodeError: return raw.decode("gb18030", errors="replace")

def parse_digits(value: Any, digits: int) -> tuple[int, ...] | None:
    if value is None: return None
    found = re.findall(r"\d", str(value))
    if len(found) < digits: return None
    return tuple(int(x) for x in found[:digits])

def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else text[:10]

def parse_cwl_json(text: str, digits: int) -> list[Draw]:
    draws = []
    for item in (json.loads(text).get("result") or json.loads(text).get("data") or []):
        if not isinstance(item, dict): continue
        issue = str(item.get("code") or item.get("issue") or item.get("expect") or "").strip()
        date = normalize_date(item.get("date") or item.get("openTime") or item.get("day"))
        nums = parse_digits(item.get("red") or item.get("number") or item.get("openCode"), digits)
        if issue and nums and len(nums) == digits: draws.append(Draw(issue, date, nums))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def parse_lottery_gov_history(text: str, digits: int) -> list[Draw]:
    d = parse_embedded_draws(text, digits)
    return sorted(unique_draws(d), key=lambda x: x.issue) if d else parse_html_table(text, digits)

def parse_plain_text(text: str, digits: int) -> list[Draw]:
    draws = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line: continue
        im = re.search(r"\b(\d{5,})\b", line)
        if not im: continue
        tail = line[im.end():]
        dm = re.search(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}", line)
        cm = re.search(rf"\b(\d{{{digits}}})\b", tail)
        nums = parse_digits(cm.group(1), digits) if cm else None
        if not nums:
            td = re.findall(r"\d", tail)
            if len(td) >= digits: nums = tuple(int(x) for x in td[-digits:])
        if nums: draws.append(Draw(im.group(1), normalize_date(dm.group(0) if dm else ""), nums))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def parse_touch_history(text: str, digits: int) -> list[Draw]:
    clean = re.sub(r"<[^>]+>", " ", html.unescape(re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.I|re.S)))
    clean = " ".join(clean.split())
    draws = []
    pat = re.compile(rf"(\d{{5,8}})\s*期\s*复制\s*(?:(20\d{{2}}[-/.年]\d{{1,2}}[-/.月]\d{{1,2}}|昨天)\s*)?\d{{1,2}}:\d{{2}}\s+((?:\d\s+){{{digits-1}}}\d)", re.S)
    cy = str(dt.datetime.now().year)
    for m in pat.finditer(clean):
        dtxt = m.group(2) or ""
        if dtxt == "昨天": dtxt = (dt.datetime.now()-dt.timedelta(days=1)).strftime("%Y-%m-%d")
        elif re.fullmatch(r"\d{2}-\d{2}", dtxt): dtxt = f"{cy}-{dtxt}"
        nums = parse_digits(m.group(3), digits)
        if nums: draws.append(Draw(m.group(1), normalize_date(dtxt), nums))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def parse_embedded_draws(text: str, digits: int) -> list[Draw]:
    draws = []
    for m in re.finditer(r"\{[^{}]*(?:draw|lottery|issue|code|number|result)[^{}]*\}", text, re.I):
        blob = m.group(0)
        im = re.search(r'"?(?:issue|code|lotteryDrawNum|drawNo)"?\s*:\s*"?(\d{5,})"?', blob, re.I)
        nm = re.search(r'"?(?:number|openCode|lotteryDrawResult|result)"?\s*:\s*"?([0-9,\s|]+)"?', blob, re.I)
        dm = re.search(r'"?(?:date|openTime|lotteryDrawTime)"?\s*:\s*"?(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})"?', blob, re.I)
        if not im or not nm: continue
        nums = parse_digits(nm.group(1), digits)
        if nums: draws.append(Draw(im.group(1), normalize_date(dm.group(1) if dm else ""), nums))
    return draws

def parse_html_table(text: str, digits: int) -> list[Draw]:
    parser = TextTableParser(); parser.feed(text)
    draws = []
    for row in parser.rows:
        joined = " ".join(row)
        im = re.search(r"\b(\d{5,})\b", joined)
        dm = re.search(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}", joined)
        if not im: continue
        chunks = [c for c in row if len(re.findall(r"\d", c)) == digits]
        nums = parse_digits(chunks[-1] if chunks else joined, digits)
        if nums: draws.append(Draw(im.group(1), normalize_date(dm.group(0) if dm else ""), nums))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def unique_draws(draws: Iterable[Draw]) -> list[Draw]:
    by_issue: dict[str, Draw] = {}
    for d in draws: by_issue[d.issue] = d
    return list(by_issue.values())

def data_file(key: str) -> Path: return DATA_DIR / f"{key}.csv"

def read_history(key: str, digits: int) -> list[Draw]:
    path = data_file(key)
    if not path.exists(): return []
    draws = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            nums = parse_digits(row.get("numbers",""), int(row.get("digits","3")))
            if nums: draws.append(Draw(row["issue"], row.get("date",""), nums))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def is_valid_history(draws: list[Draw], digits: int) -> bool:
    return bool(draws) and all(len(d.numbers) == digits for d in draws)

def write_history(key: str, draws: list[Draw]) -> None:
    path = data_file(key); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["issue","date","numbers","digits"])
        w.writeheader()
        for d in sorted(unique_draws(draws), key=lambda x: x.issue):
            w.writerow({"issue": d.issue, "date": d.date, "numbers": "".join(str(x) for x in d.numbers), "digits": len(d.numbers)})

def sample_history(key: str) -> list[Draw]:
    return [Draw(i, d, tuple(int(x) for x in n)) for i, d, n in SAMPLE_HISTORY[key]]

def collect_lottery(key: str, config: dict[str, Any]) -> tuple[list[Draw], str]:
    spec = LOTTERIES[key]; errors = []
    for src in spec["sources"]:
        try:
            text = fetch_text(src["url"], config["request_timeout_seconds"], config["user_agent"])
            parsers = {"cwl_json": parse_cwl_json, "lottery_gov_history": parse_lottery_gov_history, "touch_history": parse_touch_history, "plain_text": parse_plain_text}
            draws = parsers.get(src["type"], parse_html_table)(text, spec["digits"])
            if is_valid_history(draws, spec["digits"]):
                merged = sorted(unique_draws([*read_history(key, spec["digits"]), *draws]), key=lambda x: x.issue)[-int(config["history_limit"]):]
                write_history(key, merged); return merged, src["url"]
        except Exception as e: errors.append(f"{src['url']}: {e}")
    local = read_history(key, spec["digits"])
    if local: return local, "local-cache"
    raise RuntimeError(f"{spec['name']} no data. Errors: {'; '.join(errors)}")


# ═════════════════════════════════════════
# V2 核心算法
# ═════════════════════════════════════════

def build_constraints(draws: list[Draw], digits: int, config: dict[str, Any]) -> dict[str, Any]:
    """从历史数据提取约束条件"""
    hw, ww = config["hot_window"], config["warm_window"]
    recent = draws[-hw:] if len(draws) > hw else draws
    warm = draws[-ww:] if len(draws) > ww else draws

    # 约束1: 每个位置的活跃区
    active_sets = [set(d.numbers[p] for d in recent) or set(range(10)) for p in range(digits)]

    # 约束2: 和值
    sums = [sum(d.numbers) for d in warm]
    sm, ss = mean(sums), stddev(sums)
    sum_range = (max(0, int(sm - ss * 1.2)), min(digits * 9, int(sm + ss * 1.2) + 1))

    # 约束3: 跨度
    spans = [max(d.numbers) - min(d.numbers) for d in warm]
    spm, sps = mean(spans), stddev(spans)
    span_range = (max(0, int(spm - sps)), min(9, int(spm + sps) + 1))

    # 约束4: 奇偶
    odd = [sum(1 for n in d.numbers if n % 2 == 1) for d in warm]
    mo = Counter(odd).most_common(1)[0][0] if odd else digits // 2
    odd_range = (max(0, mo - 1), min(digits, mo + 1))

    # 约束5: 大小
    bigs = [sum(1 for n in d.numbers if n >= 5) for d in warm]
    mb = Counter(bigs).most_common(1)[0][0] if bigs else digits // 2
    big_range = (max(0, mb - 1), min(digits, mb + 1))

    # 约束6: 数字动量（近期趋势）
    half = len(warm) // 2
    first, second = warm[:half], warm[half:]
    momentum = {}
    for p in range(digits):
        scores = []
        for n in range(10):
            f1 = sum(1 for d in first if d.numbers[p] == n) / max(1, len(first))
            f2 = sum(1 for d in second if d.numbers[p] == n) / max(1, len(second))
            scores.append((n, f2 - f1))
        scores.sort(key=lambda x: -x[1])
        momentum[p] = {n: delta for n, delta in scores}

    # 约束7: 位置对关联
    pair_freq: dict[tuple[int, int, int, int], int] = defaultdict(int)
    for d in warm:
        for p1 in range(digits - 1):
            for p2 in range(p1 + 1, digits):
                pair_freq[(p1, p2, d.numbers[p1], d.numbers[p2])] += 1

    return {
        "active_sets": active_sets,
        "sum_range": sum_range,
        "span_range": span_range,
        "odd_range": odd_range,
        "big_range": big_range,
        "momentum": momentum,
        "pair_freq": dict(pair_freq),
    }


def sample_candidates(draws: list[Draw], digits: int, cons: dict[str, Any],
                      count: int, seed: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """约束筛选 → 加权随机采样 (与V1穷举打分法根本不同)"""
    rng = random.Random(seed)
    active = cons["active_sets"]
    smin, smax = cons["sum_range"]
    spmin, spmax = cons["span_range"]
    omin, omax = cons["odd_range"]
    bmin, bmax = cons["big_range"]
    momentum = cons["momentum"]
    pair_freq = cons["pair_freq"]

    # 生成所有组合并用约束过滤
    pool = []
    for comb in itertools.product(range(10), repeat=digits):
        if any(comb[p] not in active[p] for p in range(digits)): continue
        s = sum(comb)
        if not (smin <= s < smax): continue
        sp = max(comb) - min(comb)
        if not (spmin <= sp < spmax): continue
        oc = sum(1 for n in comb if n % 2 == 1)
        if not (omin <= oc <= omax): continue
        bc = sum(1 for n in comb if n >= 5)
        if not (bmin <= bc <= bmax): continue
        pool.append(comb)

    # 池太小则放宽活跃区限制
    if len(pool) < count * 3:
        for comb in itertools.product(range(10), repeat=digits):
            if comb in pool: continue
            s = sum(comb)
            if not (smin <= s < smax): continue
            sp = max(comb) - min(comb)
            if not (spmin <= sp < spmax): continue
            pool.append(comb)
            if len(pool) >= count * 10: break

    if len(pool) < count:
        pool = list(itertools.product(range(10), repeat=digits))

    # 加权（动量 + 关联对）
    weights = []
    for comb in pool:
        w = 1.0
        for p, digit in enumerate(comb):
            delta = momentum.get(p, {}).get(digit, 0)
            w += max(0, delta * 5)
        for p1 in range(digits - 1):
            for p2 in range(p1 + 1, digits):
                w += pair_freq.get((p1, p2, comb[p1], comb[p2]), 0) * 0.05
        weights.append(max(0.01, w))

    total = sum(weights)
    if total > 0: weights = [w / total for w in weights]

    chosen, seen = [], set()
    for r in range(config["sample_rounds"] * 2):
        if len(chosen) >= count: break
        rng = random.Random(seed + f"_r{r}")
        idx = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        comb = pool[idx]
        ns = "".join(str(x) for x in comb)
        if ns in seen: continue
        seen.add(ns)
        chosen.append({"rank": len(chosen)+1, "number": ns, "score": round(weights[idx]*1000, 4), "sum": sum(comb), "span": max(comb)-min(comb)})
    return chosen[:count]


def analyze_trend_v2(draws: list[Draw]) -> dict[str, Any]:
    recent = draws[-30:] if len(draws) > 30 else draws
    if not recent: return {}
    sums = [sum(d.numbers) for d in recent]
    spans = [max(d.numbers)-min(d.numbers) for d in recent]
    total_digits = len(recent) * len(recent[0].numbers)
    mods = [sum(1 for d in recent for n in d.numbers if n % 3 == m) for m in range(3)]
    return {
        "recent_window": len(recent),
        "sum_avg": round(mean(sums), 2),
        "sum_last": sums[-1] if sums else 0,
        "span_avg": round(mean(spans), 2),
        "span_last": spans[-1] if spans else 0,
        "mod3_distribution": {f"{m}路": f"{mods[m]/total_digits*100:.0f}%" for m in range(3)},
    }


def predict_v2(config: dict[str, Any]) -> dict[str, Any]:
    today = dt.datetime.now().strftime("%Y-%m-%d")
    report = {"date": today, "created_at": dt.datetime.now().isoformat(timespec="seconds"), "method": "V2-约束筛选+随机采样", "lotteries": {}}
    for key in config["lotteries"]:
        draws, source = collect_lottery(key, config)
        spec = LOTTERIES[key]
        cons = build_constraints(draws, spec["digits"], config)
        candidates = sample_candidates(draws, spec["digits"], cons, int(config["candidate_count"]), today+"_"+key, config)
        latest = draws[-1] if draws else None
        trend = analyze_trend_v2(draws)
        report["lotteries"][key] = {
            "name": spec["name"], "source": source, "history_count": len(draws),
            "latest_issue": latest.issue if latest else None, "latest_date": latest.date if latest else None,
            "latest_number": "".join(str(x) for x in latest.numbers) if latest else None,
            "method": "V2", "trend_summary": trend,
            "candidates": candidates, "top3": candidates[:3],
            "note": "随机开奖不可预测，本结果仅用于统计记录和复盘。",
        }
    save_json(REPORT_DIR / f"prediction-v2-{today}.json", report)
    write_mobile_report_v2(report)
    return report


def mean(vals: list[int]) -> float:
    return sum(vals) / max(1, len(vals))

def stddev(vals: list[int]) -> float:
    a = mean(vals)
    return math.sqrt(sum((x - a) ** 2 for x in vals) / max(1, len(vals)))


def write_mobile_report_v2(report: dict[str, Any]) -> None:
    cards = []
    for item in report["lotteries"].values():
        top3 = item.get("top3") or item.get("candidates", [])[:3]
        pills = "\n".join(f'<div class="pick"><span>{html.escape(str(c["number"]))}</span><small>#{c["rank"]} score {c["score"]}</small></div>' for c in top3)
        trend = html.escape(json.dumps(item.get("trend_summary", {}), ensure_ascii=False))
        cards.append(f'<section class="card"><div class="meta">{html.escape(str(item.get("latest_issue","")))} / {html.escape(str(item.get("latest_date","")))}</div><h2>{html.escape(str(item["name"]))} <span class="badge-v2">V2</span></h2><div class="picks">{pills}</div><div class="latest">latest: {html.escape(str(item.get("latest_number","")))}</div><details><summary>trend</summary><pre>{trend}</pre></details></section>')

    doc = f'''<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="300"><title>彩票预测 V2 - {html.escape(str(report["date"]))}</title>
<style>
:root{{color-scheme:light dark;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}}
body{{margin:0;background:#f5f7fa;color:#172033}}header{{padding:22px 16px 10px}}
h1{{margin:0;font-size:24px}}.sub{{margin-top:4px;color:#627086;font-size:13px}}
main{{padding:8px 12px 28px;display:grid;gap:12px}}
.card{{background:white;border:1px solid #dde3ec;border-radius:12px;padding:14px;box-shadow:0 8px 24px rgba(22,34,51,.06)}}
.meta{{color:#7a8699;font-size:12px}}h2{{margin:6px 0 12px;font-size:18px;display:flex;align-items:center;gap:8px}}
.badge-v2{{font-size:11px;background:#7C3AED;color:#fff;padding:2px 8px;border-radius:10px}}
.picks{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}}
.pick{{border:1px solid #cbd5e1;border-radius:10px;padding:10px 6px;text-align:center;background:#f8fafc}}
.pick span{{display:block;font-size:25px;font-weight:800;letter-spacing:2px;color:#7C3AED;overflow-wrap:anywhere}}
.pick small{{display:block;margin-top:4px;font-size:10px;color:#64748b}}
.latest{{margin-top:12px;color:#475569;font-size:13px}}
details{{margin-top:10px;font-size:12px;color:#475569}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}
footer{{padding:0 16px 22px;color:#7a8699;font-size:12px;line-height:1.5}}
.nav-bar{{display:flex;background:white;margin:-12px 16px 0;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)}}
.nav-btn{{flex:1;text-align:center;padding:12px;font-size:14px;font-weight:600;color:#6B7280;text-decoration:none}}
.nav-btn.active{{color:#7C3AED;background:#F5F3FF}}
@media(prefers-color-scheme:dark){{body{{background:#0f172a;color:#e5e7eb}}.card{{background:#111827;border-color:#263244;box-shadow:none}}.pick{{background:#172033;border-color:#334155}}.pick span{{color:#A78BFA}}.sub,.meta,.latest,details,footer,.pick small{{color:#94a3b8}}.nav-bar{{background:#1F2937}}.nav-btn.active{{background:#1E1B4B;color:#A78BFA}}}}
</style></head>
<body>
<div class="nav-bar"><a class="nav-btn" href="./">V1 打分法</a><a class="nav-btn active" href="./v2.html">V2 随机采样</a></div>
<header><h1>每日彩票 TOP3 预测 · V2</h1><div class="sub">{html.escape(str(report["date"]))} · 约束筛选+随机采样法</div></header>
<main>{"".join(cards)}</main>
<footer>⚠️ 彩票具有随机性，以上仅供娱乐参考，请理性购彩<br>对比查看: <a href="./">V1 预测</a></footer>
</body></html>'''
    path = REPORT_DIR / "v2.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lottery Predictor V2")
    parser.add_argument("command", choices=["predict", "collect", "init"])
    args = parser.parse_args(argv)
    config = load_config()
    DATA_DIR.mkdir(exist_ok=True); REPORT_DIR.mkdir(exist_ok=True)
    if args.command == "predict":
        r = predict_v2(config)
        print(f"V2 OK: {r['date']} -> {REPORT_DIR}")
    elif args.command == "collect":
        for k in config["lotteries"]:
            d, s = collect_lottery(k, config)
            print(f"{LOTTERIES[k]['name']}: {len(d)} draws from {s}")
    elif args.command == "init":
        print(f"Config: {CONFIG_PATH}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
