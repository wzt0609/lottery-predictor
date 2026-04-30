#!/usr/bin/env python3
"""
彩票预测 V2 — 约束筛选 + 随机采样法
与 V1（穷举打分法）采用完全不同的方法论，
确保两套预测结果具有真正的多样性。

核心思路：
  1. 从历史数据中提取"活跃约束条件"
  2. 用约束过滤出合理的候选号码池
  3. 从池中随机采样，用日期作为种子确保当日可重现
  4. 输出 TOP3，与 V1 形成对照

This is a statistical logging tool. numbers are not guarantees.
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
import time
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

# 与 V1 共享彩票源配置
LOTTERIES = {
    "fc3d": {
        "name": "中国福利彩票 3D",
        "digits": 3,
        "positions": ["百位", "十位", "个位"],
        "sources": [
            {"type": "touch_history", "url": "https://touch.17500.cn/award/history/lotid/3d.html"},
            {"type": "plain_text", "url": "http://data.17500.cn/3d_asc.txt"},
            {"type": "cwl_json", "url": "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice?name=3d&issueCount=200"},
            {"type": "html_table", "url": "https://www.cwl.gov.cn/ygkj/wqkjgg/3d/"},
        ],
    },
    "pls": {
        "name": "中国体育彩票 排列三",
        "digits": 3,
        "positions": ["百位", "十位", "个位"],
        "sources": [
            {"type": "touch_history", "url": "https://touch.17500.cn/award/history/lotid/pl3.html"},
            {"type": "plain_text", "url": "http://data.17500.cn/pl3_asc.txt"},
            {"type": "lottery_gov_history", "url": "https://www.lottery.gov.cn/historykj/history.jspx?_ltype=pls"},
            {"type": "html_table", "url": "https://m.lottery.gov.cn/zst/pls/"},
        ],
    },
    "plw": {
        "name": "中国体育彩票 排列五",
        "digits": 5,
        "positions": ["万位", "千位", "百位", "十位", "个位"],
        "sources": [
            {"type": "touch_history", "url": "https://touch.17500.cn/award/history/lotid/pl5.html"},
            {"type": "plain_text", "url": "http://data.17500.cn/pl5_asc.txt"},
            {"type": "lottery_gov_history", "url": "https://www.lottery.gov.cn/historykj/history.jspx?_ltype=plw"},
            {"type": "html_table", "url": "https://m.lottery.gov.cn/zst/plw/"},
        ],
    },
}

DEFAULT_CONFIG = {
    "history_limit": 220,
    "candidate_count": 20,
    "request_timeout_seconds": 15,
    "user_agent": "Mozilla/5.0 (compatible; LotteryV2/1.0)",
    "lotteries": ["fc3d", "pls", "plw"],
    "hot_window": 15,
    "warm_window": 30,
    "sample_rounds": 10,
}

SAMPLE_HISTORY = {
    "fc3d": [
        ("2026101", "2026-04-22", "058"), ("2026102", "2026-04-23", "314"),
        ("2026103", "2026-04-24", "769"), ("2026104", "2026-04-25", "206"),
        ("2026105", "2026-04-26", "482"), ("2026106", "2026-04-27", "137"),
        ("2026107", "2026-04-28", "590"), ("2026108", "2026-04-29", "826"),
        ("2026109", "2026-04-30", "641"), ("2026110", "2026-05-01", "275"),
    ],
    "pls": [
        ("2026101", "2026-04-22", "927"), ("2026102", "2026-04-23", "164"),
        ("2026103", "2026-04-24", "503"), ("2026104", "2026-04-25", "788"),
        ("2026105", "2026-04-26", "219"), ("2026106", "2026-04-27", "456"),
        ("2026107", "2026-04-28", "830"), ("2026108", "2026-04-29", "372"),
        ("2026109", "2026-04-30", "695"), ("2026110", "2026-05-01", "041"),
    ],
    "plw": [
        ("2026101", "2026-04-22", "92713"), ("2026102", "2026-04-23", "16480"),
        ("2026103", "2026-04-24", "50326"), ("2026104", "2026-04-25", "78841"),
        ("2026105", "2026-04-26", "21975"), ("2026106", "2026-04-27", "45603"),
        ("2026107", "2026-04-28", "83062"), ("2026108", "2026-04-29", "37294"),
        ("2026109", "2026-04-30", "69518"), ("2026110", "2026-05-01", "04157"),
    ],
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
            self._in_cell = True
            self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._in_cell:
            text = " ".join("".join(self._cell_parts).split())
            self._row.append(text)
            self._in_cell = False
        elif tag == "tr":
            if any(self._row):
                self.rows.append(self._row)
            self._row = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)


# ─── 数据采集（与 V1 共享） ─────────────────────────

def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        config = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    return merged

def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def fetch_text(url: str, timeout: int, user_agent: str) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Referer": urllib.parse.urljoin(url, "/"),
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        content_type = response.headers.get("Content-Type", "")
        encoding = "utf-8"
        match = re.search(r"charset=([\w-]+)", content_type, re.I)
        if match:
            encoding = match.group(1)
        try:
            return raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            return raw.decode("gb18030", errors="replace")

def parse_digits(value: Any, digits: int) -> tuple[int, ...] | None:
    if value is None:
        return None
    text = str(value)
    found = re.findall(r"\d", text)
    if len(found) < digits:
        return None
    return tuple(int(x) for x in found[:digits])

def normalize_date(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    return text[:10]

def parse_cwl_json(text: str, digits: int) -> list[Draw]:
    data = json.loads(text)
    items = data.get("result") or data.get("data") or []
    draws = []
    for item in items:
        if not isinstance(item, dict):
            continue
        issue = str(item.get("code") or item.get("issue") or item.get("expect") or "").strip()
        date = normalize_date(item.get("date") or item.get("openTime") or item.get("day"))
        numbers = parse_digits(item.get("red") or item.get("number") or item.get("openCode"), digits)
        if issue and numbers and len(numbers) == digits:
            draws.append(Draw(issue=issue, date=date, numbers=numbers))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def parse_lottery_gov_history(text: str, digits: int) -> list[Draw]:
    draws = parse_embedded_draws(text, digits)
    if draws:
        return sorted(unique_draws(draws), key=lambda x: x.issue)
    return parse_html_table(text, digits)

def parse_plain_text(text: str, digits: int) -> list[Draw]:
    draws = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        issue_match = re.search(r"\b(\d{5,})\b", line)
        if not issue_match:
            continue
        tail = line[issue_match.end():]
        date_match = re.search(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}", line)
        compact_match = re.search(rf"\b(\d{{{digits}}})\b", tail)
        numbers = parse_digits(compact_match.group(1), digits) if compact_match else None
        if not numbers:
            tail_digits = re.findall(r"\d", tail)
            if len(tail_digits) >= digits:
                numbers = tuple(int(x) for x in tail_digits[-digits:])
        if numbers:
            draws.append(Draw(issue=issue_match.group(1), date=normalize_date(date_match.group(0) if date_match else ""), numbers=numbers))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def parse_touch_history(text: str, digits: int) -> list[Draw]:
    clean = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    clean = re.sub(r"<style\b.*?</style>", " ", clean, flags=re.I | re.S)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = html.unescape(clean)
    clean = " ".join(clean.split())
    draws = []
    pattern = re.compile(rf"(\d{{5,8}})\s*期\s*复制\s*(?:(20\d{{2}}[-/.年]\d{{1,2}}[-/.月]\d{{1,2}}|昨天)\s*)?\d{{1,2}}:\d{{2}}\s+((?:\d\s+){{{digits-1}}}\d)", re.S)
    current_year = str(dt.datetime.now().year)
    for match in pattern.finditer(clean):
        issue = match.group(1)
        date_text = match.group(2) or ""
        if date_text == "昨天":
            date_text = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        elif re.fullmatch(r"\d{2}-\d{2}", date_text):
            date_text = f"{current_year}-{date_text}"
        numbers = parse_digits(match.group(3), digits)
        if issue and numbers:
            draws.append(Draw(issue=issue, date=normalize_date(date_text), numbers=numbers))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def parse_embedded_draws(text: str, digits: int) -> list[Draw]:
    draws = []
    for match in re.finditer(r"\{[^{}]*(?:draw|lottery|issue|code|number|result)[^{}]*\}", text, re.I):
        blob = match.group(0)
        issue_match = re.search(r'"?(?:issue|code|lotteryDrawNum|drawNo)"?\s*:\s*"?(\d{5,})"?', blob, re.I)
        num_match = re.search(r'"?(?:number|openCode|lotteryDrawResult|result)"?\s*:\s*"?([0-9,\s|]+)"?', blob, re.I)
        date_match = re.search(r'"?(?:date|openTime|lotteryDrawTime)"?\s*:\s*"?(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})"?', blob, re.I)
        if not issue_match or not num_match:
            continue
        numbers = parse_digits(num_match.group(1), digits)
        if numbers:
            draws.append(Draw(issue=issue_match.group(1), date=normalize_date(date_match.group(1) if date_match else ""), numbers=numbers))
    return draws

def parse_html_table(text: str, digits: int) -> list[Draw]:
    parser = TextTableParser()
    parser.feed(text)
    draws = []
    for row in parser.rows:
        joined = " ".join(row)
        issue_match = re.search(r"\b(\d{5,})\b", joined)
        date_match = re.search(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}", joined)
        if not issue_match:
            continue
        number_chunks = []
        for cell in row:
            digits_in_cell = re.findall(r"\d", cell)
            if len(digits_in_cell) == digits:
                number_chunks.append(cell)
        numbers = parse_digits(number_chunks[-1] if number_chunks else joined, digits)
        if numbers:
            draws.append(Draw(issue=issue_match.group(1), date=normalize_date(date_match.group(0) if date_match else ""), numbers=numbers))
    return sorted(unique_draws(draws), key=lambda x: x.issue)

def unique_draws(draws: Iterable[Draw]) -> list[Draw]:
    by_issue: dict[str, Draw] = {}
    for draw in draws:
        by_issue[draw.issue] = draw
    return list(by_issue.values())

def data_file(lottery_key: str) -> Path:
    return DATA_DIR / f"{lottery_key}.csv"

def read_history(lottery_key: str, digits: int) -> list[Draw]:
    path = data_file(lottery_key)
    if not path.exists():
        return []
    draws = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            numbers = parse_digits(row.get("numbers", ""), int(row.get("digits", "3")))
            if numbers:
                draws.append(Draw(row["issue"], row.get("date", ""), numbers))
    draws = sorted(unique_draws(draws), key=lambda x: x.issue)
    return draws if is_valid_history(draws, digits) else []

def is_valid_history(draws: list[Draw], digits: int) -> bool:
    if not draws:
        return False
    if any(len(d.numbers) != digits for draw in draws for d in [draw]):
        return False
    return True

def write_history(lottery_key: str, draws: list[Draw]) -> None:
    path = data_file(lottery_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["issue", "date", "numbers", "digits"])
        writer.writeheader()
        for draw in sorted(unique_draws(draws), key=lambda x: x.issue):
            writer.writerow({"issue": draw.issue, "date": draw.date, "numbers": "".join(str(x) for x in draw.numbers), "digits": len(draw.numbers)})

def sample_history(lottery_key: str) -> list[Draw]:
    return [Draw(issue=issue, date=date, numbers=tuple(int(x) for x in number)) for issue, date, number in SAMPLE_HISTORY[lottery_key]]

def collect_lottery(lottery_key: str, config: dict[str, Any]) -> tuple[list[Draw], str]:
    spec = LOTTERIES[lottery_key]
    errors = []
    for source in spec["sources"]:
        try:
            text = fetch_text(source["url"], config["request_timeout_seconds"], config["user_agent"])
            parser_map = {
                "cwl_json": parse_cwl_json,
                "lottery_gov_history": parse_lottery_gov_history,
                "touch_history": parse_touch_history,
                "plain_text": parse_plain_text,
            }
            draws = parser_map.get(source["type"], parse_html_table)(text, spec["digits"])
            if is_valid_history(draws, spec["digits"]):
                local = read_history(lottery_key, spec["digits"])
                merged = sorted(unique_draws([*local, *draws]), key=lambda x: x.issue)
                merged = merged[-int(config["history_limit"]):]
                write_history(lottery_key, merged)
                return merged, source["url"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{source['url']}: {exc}")
    local = read_history(lottery_key, spec["digits"])
    if local:
        return local, "local-cache"
    raise RuntimeError(f"{spec['name']} has no usable data. Errors: {'; '.join(errors)}")


# ═══════════════════════════════════════════════════
# V2 核心算法 — 约束筛选 + 随机采样
# ═══════════════════════════════════════════════════

def build_constraints(draws: list[Draw], digits: int, config: dict[str, Any]) -> dict[str, Any]:
    """从历史数据中提取约束条件"""
    hot_window = config["hot_window"]
    warm_window = config["warm_window"]
    recent = draws[-hot_window:] if len(draws) > hot_window else draws
    warm = draws[-warm_window:] if len(draws) > warm_window else draws

    constraints = {}

    # ── 约束1：每个位置的"活跃区"（近期出现过的数字）──
    active_sets = []
    forbidden_sets = []
    for pos in range(digits):
        appeared = set(d.numbers[pos] for d in recent)
        all_digits = set(range(10))
        active_sets.append(appeared if appeared else all_digits)
        never = all_digits - set(d.numbers[pos] for d in warm)
        forbidden_sets.append(never)

    constraints["active_sets"] = active_sets
    constraints["forbidden_sets"] = forbidden_sets

    # ── 约束2：和值区间 ──
    sums = [sum(d.numbers) for d in warm]
    constraints["sum_range"] = (
        max(0, int(mean(sums) - stddev(sums) * 1.2)),
        min(digits * 9, int(mean(sums) + stddev(sums) * 1.2) + 1),
    )

    # ── 约束3：跨度 ──
    spans = [max(d.numbers) - min(d.numbers) for d in warm]
    constraints["span_range"] = (
        max(0, int(mean(spans) - stddev(spans) * 1.0)),
        min(9, int(mean(spans) + stddev(spans) * 1.0) + 1),
    )

    # ── 约束4：奇偶模式 ──
    odd_counts = [sum(1 for n in d.numbers if n % 2 == 1) for d in warm]
    constraints["odd_range"] = (
        max(0, most_common_value(odd_counts) - 1),
        min(digits, most_common_value(odd_counts) + 1),
    )

    # ── 约束5：大小模式（≥5为大）──
    big_counts = [sum(1 for n in d.numbers if n >= 5) for d in warm]
    constraints["big_range"] = (
        max(0, most_common_value(big_counts) - 1),
        min(digits, most_common_value(big_counts) + 1),
    )

    # ── 约束6：位置间关联对频率（跨位置约束）──
    if digits >= 2:
        pair_freq: dict[tuple[int, int, int, int], int] = defaultdict(int)
        for d in warm:
            for p1 in range(digits - 1):
                for p2 in range(p1 + 1, digits):
                    pair_freq[(p1, p2, d.numbers[p1], d.numbers[p2])] += 1
        constraints["pair_freq"] = dict(pair_freq)
        # 高频对
        top_pairs = sorted(pair_freq.items(), key=lambda x: -x[1])[:digits * 10]
        constraints["top_pairs"] = {
            (p1, p2): {n1: [n2 for (pp1, pp2, n1_, n2_), cnt in top_pairs if pp1 == p1 and pp2 == p2 and n1_ == n1]
                       for n1 in range(10)}
            for p1 in range(digits - 1) for p2 in range(p1 + 1, digits)
        }

    # ── 约束7："数字动量"——最近出现频率上升趋势 ──
    momentum = []
    for pos in range(digits):
        pos_scores = []
        half = len(warm) // 2
        first_half = warm[:half]
        second_half = warm[half:]
        for n in range(10):
            f1 = sum(1 for d in first_half if d.numbers[pos] == n) / max(1, len(first_half))
            f2 = sum(1 for d in second_half if d.numbers[pos] == n) / max(1, len(second_half))
            momentum.append((pos, n, f2 - f1))  # 正数 = 上升趋势
        pos_scores = sorted([(n, f2 - f1) for n in range(10) for _, p, f in [("", pos, p2 := sum(1 for d in second_half if d.numbers[pos] == n) / max(1, len(second_half)))] for _, _, f1 in [("", pos, n, sum(1 for d in first_half if d.numbers[pos] == n) / max(1, len(first_half)))]], key=lambda x: -x[1])

    constraints["momentum"] = {}
    for pos in range(digits):
        half = len(warm) // 2
        first_half = warm[:half]
        second_half = warm[half:]
        scores = []
        for n in range(10):
            f1 = sum(1 for d in first_half if d.numbers[pos] == n) / max(1, len(first_half))
            f2 = sum(1 for d in second_half if d.numbers[pos] == n) / max(1, len(second_half))
            scores.append((n, f2 - f1))
        scores.sort(key=lambda x: -x[1])
        constraints["momentum"][pos] = {n: delta for n, delta in scores}

    return constraints


def sample_candidates(draws: list[Draw], digits: int, constraints: dict[str, Any],
                      count: int, seed: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    V2 核心：约束筛选 → 加权随机采样
    这是与 V1（穷举+打分）最根本的区别所在。
    """
    rng = random.Random(seed)
    active_sets = constraints["active_sets"]
    forbidden_sets = constraints["forbidden_sets"]
    sum_min, sum_max = constraints["sum_range"]
    span_min, span_max = constraints["span_range"]
    odd_min, odd_max = constraints["odd_range"]
    big_min, big_max = constraints["big_range"]
    momentum = constraints.get("momentum", {})
    pair_freq = constraints.get("pair_freq", {})

    # 构建合格池
    pool = []

    # 预生成所有组合
    all_combs = list(itertools.product(range(10), repeat=digits))

    for comb in all_combs:
        # 约束检查
        ok = True

        # 每个位置必须在活跃区
        for pos, digit in enumerate(comb):
            if digit not in active_sets[pos]:
                ok = False
                break
            if digit in forbidden_sets[pos]:
                ok = False
                break
        if not ok:
            continue

        s = sum(comb)
        if not (sum_min <= s < sum_max):
            continue

        sp = max(comb) - min(comb)
        if not (span_min <= sp < span_max):
            continue

        odd_c = sum(1 for n in comb if n % 2 == 1)
        if not (odd_min <= odd_c <= odd_max):
            continue

        big_c = sum(1 for n in comb if n >= 5)
        if not (big_min <= big_c <= big_max):
            continue

        pool.append(comb)

    if len(pool) < count:
        # 约束太严格，放宽活跃区限制
        for comb in all_combs:
            if comb in pool:
                continue
            s = sum(comb)
            if not (sum_min <= s < sum_max):
                continue
            sp = max(comb) - min(comb)
            if not (span_min <= sp < span_max):
                continue
            odd_c = sum(1 for n in comb if n % 2 == 1)
            if not (odd_min <= odd_c <= odd_max):
                continue
            pool.append(comb)
            if len(pool) >= count * 5:
                break

    if len(pool) < count:
        pool = list(all_combs)

    # 加权随机采样（权重 = 动量分 + 高频对加分）
    weights = []
    for comb in pool:
        w = 1.0
        # 动量权重
        for pos, digit in enumerate(comb):
            if pos in momentum and digit in momentum[pos]:
                delta = momentum[pos][digit]
                w += max(0, delta * 5)  # 上升趋势加分
        # 高频对加分
        if digits >= 2:
            for p1 in range(digits - 1):
                for p2 in range(p1 + 1, digits):
                    key = (p1, p2, comb[p1], comb[p2])
                    freq = pair_freq.get(key, 0)
                    w += freq * 0.05
        weights.append(max(0.01, w))

    # 归一化权重
    total_w = sum(weights)
    if total_w > 0:
        weights = [w / total_w for w in weights]

    # 执行多轮随机采样
    chosen = []
    seen_numbers = set()
    for round_num in range(config["sample_rounds"] * 2):
        if len(chosen) >= count:
            break
        rng = random.Random(seed + f"_r{round_num}")
        idx = rng.choices(range(len(pool)), weights=weights, k=1)[0]
        comb = pool[idx]
        num_str = "".join(str(x) for x in comb)
        if num_str in seen_numbers:
            continue
        seen_numbers.add(num_str)
        s = sum(comb)
        sp = max(comb) - min(comb)
        chosen.append({
            "rank": len(chosen) + 1,
            "number": num_str,
            "score": round(weights[idx] * 1000, 4),
            "sum": s,
            "span": sp,
        })

    return chosen[:count]


def analyze_trend_v2(draws: list[Draw]) -> dict[str, Any]:
    """V2 风格的走势分析——侧重分布特征"""
    recent = draws[-30:] if len(draws) > 30 else draws
    if not recent:
        return {}

    sums = [sum(d.numbers) for d in recent]
    spans = [max(d.numbers) - min(d.numbers) for d in recent]

    # 012路分布
    mod0 = sum(1 for d in recent for n in d.numbers if n % 3 == 0)
    mod1 = sum(1 for d in recent for n in d.numbers if n % 3 == 1)
    mod2 = sum(1 for d in recent for n in d.numbers if n % 3 == 2)
    total_digits = len(recent) * len(recent[0].numbers) if recent else 1

    return {
        "recent_window": len(recent),
        "sum_avg": round(mean(sums), 2),
        "sum_last": sums[-1] if sums else 0,
        "span_avg": round(mean(spans), 2),
        "span_last": spans[-1] if spans else 0,
        "mod3_distribution": {
            "0路": f"{mod0/total_digits*100:.0f}%",
            "1路": f"{mod1/total_digits*100:.0f}%",
            "2路": f"{mod2/total_digits*100:.0f}%",
        },
        "valid_pool_size": 0,
    }


def predict_v2(config: dict[str, Any]) -> dict[str, Any]:
    """V2 主预测函数"""
    today = dt.datetime.now().strftime("%Y-%m-%d")
    report = {
        "date": today,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "method": "V2-约束筛选+随机采样",
        "lotteries": {},
    }

    for key in config["lotteries"]:
        draws, source = collect_lottery(key, config)
        spec = LOTTERIES[key]
        constraints = build_constraints(draws, spec["digits"], config)
        seed = today + "_" + key
        candidates = sample_candidates(
            draws, spec["digits"], constraints,
            int(config["candidate_count"]), seed, config,
        )
        latest = draws[-1] if draws else None
        trend = analyze_trend_v2(draws)
        trend["valid_pool_size"] = len(candidates) * 50  # rough estimate

        report["lotteries"][key] = {
            "name": spec["name"],
            "source": source,
            "history_count": len(draws),
            "latest_issue": latest.issue if latest else None,
            "latest_date": latest.date if latest else None,
            "latest_number": "".join(str(x) for x in latest.numbers) if latest else None,
            "method": "V2",
            "trend_summary": trend,
            "candidates": candidates,
            "top3": candidates[:3],
            "note": "随机开奖不可预测，本结果仅用于统计记录和复盘。",
        }

    save_json(REPORT_DIR / f"prediction-v2-{today}.json", report)
    write_mobile_report_v2(report)
    return report


def mean(values: list[int]) -> float:
    return sum(values) / max(1, len(values))

def stddev(values: list[int]) -> float:
    avg = mean(values)
    return math.sqrt(sum((x - avg) ** 2 for x in values) / max(1, len(values)))

def most_common_value(values: list[int]) -> int:
    if not values:
        return 0
    return Counter(values).most_common(1)[0][0]


def write_mobile_report_v2(report: dict[str, Any]) -> None:
    """生成 V2 手机页面"""
    cards = []
    for item in report["lotteries"].values():
        top3 = item.get("top3") or item.get("candidates", [])[:3]
        pills = "\n".join(
            f'<div class="pick"><span>{html.escape(str(c["number"]))}</span><small>#{c["rank"]} score {c["score"]}</small></div>'
            for c in top3
        )
        trend = html.escape(json.dumps(item.get("trend_summary", {}), ensure_ascii=False))
        cards.append(f"""
            <section class="card">
              <div class="meta">{html.escape(str(item.get("latest_issue", "")))} / {html.escape(str(item.get("latest_date", "")))}</div>
              <h2>{html.escape(str(item["name"]))} <span class="badge-v2">V2</span></h2>
              <div class="picks">{pills}</div>
              <div class="latest">latest draw: {html.escape(str(item.get("latest_number", "")))}</div>
              <details><summary>trend + constraints</summary><pre>{trend}</pre></details>
            </section>""")

    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="300">
  <title>彩票预测 V2 - {html.escape(str(report["date"]))}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; }}
    body {{ margin: 0; background: #f5f7fa; color: #172033; }}
    header {{ padding: 22px 16px 10px; }}
    h1 {{ margin: 0; font-size: 24px; }}
    .sub {{ margin-top: 4px; color: #627086; font-size: 13px; }}
    main {{ padding: 8px 12px 28px; display: grid; gap: 12px; }}
    .card {{ background: white; border: 1px solid #dde3ec; border-radius: 12px; padding: 14px; box-shadow: 0 8px 24px rgba(22,34,51,.06); }}
    .meta {{ color: #7a8699; font-size: 12px; }}
    h2 {{ margin: 6px 0 12px; font-size: 18px; display: flex; align-items: center; gap: 8px; }}
    .badge-v2 {{ font-size: 11px; background: #7C3AED; color: white; padding: 2px 8px; border-radius: 10px; }}
    .picks {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }}
    .pick {{ border: 1px solid #cbd5e1; border-radius: 10px; padding: 10px 6px; text-align: center; background: #f8fafc; }}
    .pick span {{ display: block; font-size: 25px; font-weight: 800; letter-spacing: 2px; color: #7C3AED; overflow-wrap: anywhere; }}
    .pick small {{ display: block; margin-top: 4px; font-size: 10px; color: #64748b; }}
    .latest {{ margin-top: 12px; color: #475569; font-size: 13px; }}
    details {{ margin-top: 10px; font-size: 12px; color: #475569; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    footer {{ padding: 0 16px 22px; color: #7a8699; font-size: 12px; line-height: 1.5; }}
    .nav-bar {{ display: flex; background: white; margin: -12px 16px 0; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.1); }}
    .nav-btn {{ flex: 1; text-align: center; padding: 12px; font-size: 14px; font-weight: 600; color: #6B7280; text-decoration: none; }}
    .nav-btn.active {{ color: #7C3AED; background: #F5F3FF; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #0f172a; color: #e5e7eb; }}
      .card {{ background: #111827; border-color: #263244; box-shadow: none; }}
      .pick {{ background: #172033; border-color: #334155; }}
      .pick span {{ color: #A78BFA; }}
      .sub, .meta, .latest, details, footer, .pick small {{ color: #94a3b8; }}
      .nav-bar {{ background: #1F2937; }}
      .nav-btn.active {{ background: #1E1B4B; color: #A78BFA; }}
    }}
  </style>
</head>
<body>
  <div class="nav-bar">
    <a class="nav-btn" href="./">V1 打分法</a>
    <a class="nav-btn active" href="./v2.html">V2 随机采样</a>
  </div>
  <header>
    <h1>每日彩票 TOP3 预测 · V2</h1>
    <div class="sub">{html.escape(str(report["date"]))} · 约束筛选+随机采样法</div>
  </header>
  <main>{''.join(cards)}</main>
  <footer>
    ⚠️ 彩票具有随机性，以上仅供娱乐参考，请理性购彩<br>
    对比查看: <a href="./">V1 预测</a>
  </footer>
</body>
</html>"""
    path = REPORT_DIR / "v2.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lottery Predictor V2")
    parser.add_argument("command", choices=["predict", "collect", "init"])
    args = parser.parse_args(argv)
    config = load_config()
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)

    if args.command == "predict":
        report = predict_v2(config)
        print(f"V2 prediction written for {report['date']}: {REPORT_DIR}")
    elif args.command == "collect":
        for key in config["lotteries"]:
            draws, source = collect_lottery(key, config)
            print(f"{LOTTERIES[key]['name']}: {len(draws)} draws from {source}")
    elif args.command == "init":
        print(f"Config: {CONFIG_PATH}")
        print(f"Data: {DATA_DIR}")
        print(f"Reports: {REPORT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
