#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collate.py —— 古籍校勘单文件工具（仅依赖 Python 标准库）

用法：
    python3 collate.py 输入文件          # 从文件读入
    python3 collate.py                  # 从标准输入读入
    python3 collate.py --demo           # 运行内置示例（覆盖全部错误类型与级联场景）

输入格式（行指令，# 之后为注释，空行忽略）：

    VERSION <名称> <优先级整数>          # 版本定义；优先级数值越大越权威
    RULE <规则号> WHEN <条件> PREFER <倾向>
    TEXT <版本> [位置] <内容...>         # 文本流；位置可省略，省略时按该版本出现顺序递增

规则条件（逗号分隔表示"与"）：
    always                恒真
    pos<N  pos<=N  pos=N  pos>=N  pos>N   位置比较
    has:子串              任一异文包含子串
    var:版本名            该版本在此位置出现

规则倾向（PREFER 目标）：
    <版本名>   取该版本文字
    high       取在场版本中优先级最高者
    low        取在场版本中优先级最低者

裁决顺序（理由见文末说明）：
    1. 显式规则（按定义先后，先匹配先生效）
    2. 前序抉择派生的版本偏好（级联：前文抉择改变后文版本关系）
    3. 以上皆无 -> 报告"未覆盖异文"，并按版本优先级回退（不产生偏好边）

输出：校勘结果、抉择历史（可追溯父抉择）、错误清单。
错误类型：unknown_version / duplicate_entry / unknown_rule_target /
          bad_directive / uncovered_variant / preference_cycle /
          rule_target_absent
"""

import re
import sys
from dataclasses import dataclass, field


# ---------------------------------------------------------------- 数据结构

@dataclass
class VersionDef:
    name: str
    priority: int


@dataclass
class Rule:
    rid: str
    conds: list      # 条件串列表，全部满足才命中
    prefer: str      # 版本名 | "high" | "low"


@dataclass
class Decision:
    did: str
    position: int
    variants: dict   # 版本 -> 内容
    chosen: str
    basis: str       # 依据说明
    parents: list    # 派生来源（父抉择 did 列表），用于追溯


@dataclass
class Err:
    kind: str
    message: str


# ---------------------------------------------------------------- 条件匹配

COND_RE = re.compile(r"^(always|pos(?:<=|>=|=|<|>)\d+|has:.+|var:.+)$")


def match_cond(cond, pos, variants):
    if cond == "always":
        return True
    m = re.fullmatch(r"pos(<=|>=|=|<|>)(\d+)", cond)
    if m:
        op, n = m.group(1), int(m.group(2))
        return {"<": pos < n, "<=": pos <= n, "=": pos == n,
                ">": pos > n, ">=": pos >= n}[op]
    if cond.startswith("has:"):
        sub = cond[4:]
        return any(sub in c for c in variants.values())
    if cond.startswith("var:"):
        return cond[4:] in variants
    return False  # 解析期已校验，不会走到


# ---------------------------------------------------------------- 解析

def parse(text):
    versions, rules, entries, errors = {}, [], [], []
    implicit = {}  # 版本 -> 下一个隐式位置
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        head = parts[0].upper()
        if head == "VERSION":
            if len(parts) < 3 or not re.fullmatch(r"-?\d+", parts[2]):
                errors.append(Err("bad_directive",
                                  f"第{lineno}行：VERSION 需要 名称 与整数优先级"))
                continue
            name, pri = parts[1], int(parts[2])
            if name in versions:
                errors.append(Err("bad_directive",
                                  f"第{lineno}行：版本「{name}」重复定义，保留先见者"))
                continue
            versions[name] = VersionDef(name, pri)
        elif head == "RULE":
            # RULE <rid> WHEN <conds> PREFER <target>
            if (len(parts) != 6 or parts[2].upper() != "WHEN"
                    or parts[4].upper() != "PREFER"):
                errors.append(Err("bad_directive",
                                  f"第{lineno}行：RULE 语法应为 "
                                  f"RULE 编号 WHEN 条件 PREFER 倾向"))
                continue
            rid, conds, prefer = parts[1], parts[3].split(","), parts[5]
            bad = [c for c in conds if not COND_RE.match(c)]
            if bad:
                errors.append(Err("bad_directive",
                                  f"第{lineno}行：规则 {rid} 含非法条件 {bad}，规则已忽略"))
                continue
            rules.append(Rule(rid, conds, prefer))
        elif head == "TEXT":
            if len(parts) < 3:
                errors.append(Err("bad_directive",
                                  f"第{lineno}行：TEXT 需要 版本 与 内容"))
                continue
            ver, rest = parts[1], parts[2:]
            if re.fullmatch(r"\d+", rest[0]) and len(rest) > 1:
                pos, content = int(rest[0]), " ".join(rest[1:])
            else:
                pos, content = implicit.get(ver, 1), " ".join(rest)
            implicit[ver] = pos + 1
            entries.append((ver, pos, content, lineno))
        else:
            errors.append(Err("bad_directive",
                              f"第{lineno}行：无法识别的指令「{parts[0]}」"))
    return versions, rules, entries, errors


# ---------------------------------------------------------------- 校勘引擎

class Engine:
    def __init__(self, versions, rules):
        self.versions = versions
        self.rules = rules
        self.graph = {}        # x -> {y: [did,...]}  表示"x 优于 y"，边由抉择 did 建立
        self.decisions = []    # 抉择历史
        self.errors = []       # 错误清单
        self.results = []      # (位置, 选定文本, 来源版本, 依据)

    # ---- 偏好图：BFS 找 x 到 y 的路径，返回 [(节点, 建立该边的did), ...] ----
    def find_path(self, src, dst):
        if src == dst:
            return [(src, None)]
        seen = {src}
        queue = [[(src, None)]]
        while queue:
            path = queue.pop(0)
            node = path[-1][0]
            for nxt, dids in self.graph.get(node, {}).items():
                if nxt in seen:
                    continue
                newpath = path + [(nxt, dids[0])]
                if nxt == dst:
                    return newpath
                seen.add(nxt)
                queue.append(newpath)
        return None

    @staticmethod
    def fmt_path(path):
        out = path[0][0]
        for node, did in path[1:]:
            out += f" -[{did}]-> {node}"
        return out

    # ---- 加偏好边；若成环则报告并拒绝 ----
    def add_preference(self, x, y, did, pos):
        if x == y:
            return
        if y in self.graph.get(x, {}):
            self.graph[x][y].append(did)  # 同向边：追加佐证抉择
            return
        back = self.find_path(y, x)
        if back is not None:
            cyc = [did] + [d for _, d in back[1:] if d]
            self.errors.append(Err(
                "preference_cycle",
                f"位置 {pos}：抉择 {did} 欲引入「{x} 优于 {y}」，与既有偏好路径 "
                f"{self.fmt_path(back)} 构成循环；环上抉择：{', '.join(cyc)}。"
                f"该偏好边已拒绝，以免后续裁决自相矛盾"))
            return
        self.graph.setdefault(x, {}).setdefault(y, []).append(did)

    # ---- 派生裁决：某版本在偏好图上支配在场所有其他版本 ----
    def derived_choice(self, variants):
        best = None
        for x in variants:
            parents, ok = [], True
            for y in variants:
                if y == x:
                    continue
                p = self.find_path(x, y)
                if p is None:
                    ok = False
                    break
                parents.extend(d for _, d in p[1:] if d)
            if ok:
                if best is not None:
                    return None  # 不会有两个支配者（环边已被拒绝），防御性返回
                best = (x, sorted(set(parents)))
        return best

    def by_priority(self, variants, reverse=True):
        return sorted(variants,
                      key=lambda v: (self.versions[v].priority, v),
                      reverse=reverse)[0]

    # ---- 单位置裁决 ----
    def adjudicate(self, pos, variants):
        # 1. 显式规则，按定义顺序
        for rule in self.rules:
            if not all(match_cond(c, pos, variants) for c in rule.conds):
                continue
            t = rule.prefer
            if t == "high":
                return self.by_priority(variants, True), f"规则 {rule.rid}（取高优先级）", []
            if t == "low":
                return self.by_priority(variants, False), f"规则 {rule.rid}（取低优先级）", []
            if t in variants:
                return t, f"规则 {rule.rid}", []
            self.errors.append(Err(
                "rule_target_absent",
                f"位置 {pos}：规则 {rule.rid} 命中，但其倾向版本「{t}」"
                f"在此位置无文本，继续尝试后续规则"))
        # 2. 前序抉择派生（级联）
        derived = self.derived_choice(variants)
        if derived:
            x, parents = derived
            return x, f"派生自前序抉择 {', '.join(parents)}", parents
        # 3. 未覆盖：报告 + 优先级回退（回退不建立偏好边）
        detail = "；".join(f"{v}「{c}」" for v, c in sorted(variants.items()))
        self.errors.append(Err(
            "uncovered_variant",
            f"位置 {pos}：异文无任何规则覆盖，亦无前序抉择可派生：{detail}"))
        return (self.by_priority(variants, True),
                "无规则覆盖，按版本优先级回退（不产生后续影响）", [])

    # ---- 主流程 ----
    def run(self, entries):
        by_version, seen = {}, set()
        for ver, pos, content, lineno in entries:
            if ver not in self.versions:
                self.errors.append(Err(
                    "unknown_version",
                    f"第{lineno}行：版本「{ver}」未在 VERSION 中定义，该条文本已忽略"))
                continue
            key = (ver, pos)
            if key in seen:
                self.errors.append(Err(
                    "duplicate_entry",
                    f"第{lineno}行：版本「{ver}」在位置 {pos} 已有文本"
                    f"「{by_version[ver][pos]}」，重复文本「{content}」已忽略"))
                continue
            seen.add(key)
            by_version.setdefault(ver, {})[pos] = content

        positions = sorted({p for ps in by_version.values() for p in ps})
        seq = 0
        for pos in positions:
            variants = {v: ps[pos] for v, ps in by_version.items() if pos in ps}
            if len(set(variants.values())) <= 1:
                src = self.by_priority(variants, True)
                self.results.append((pos, variants[src], src, "无异文"))
                continue
            # 同一位置多版本异文：裁决
            chosen, basis, parents = self.adjudicate(pos, variants)
            seq += 1
            did = f"d{seq}"
            self.decisions.append(Decision(did, pos, dict(variants),
                                           chosen, basis, parents))
            if not basis.startswith("无规则覆盖"):
                for other in variants:
                    if other != chosen:
                        self.add_preference(chosen, other, did, pos)
            self.results.append((pos, variants[chosen], chosen,
                                 f"{basis}（抉择 {did}）"))
        return self.results, self.decisions, self.errors


# ---------------------------------------------------------------- 输出

def print_report(results, decisions, errors):
    print("=" * 60)
    print("校勘结果")
    print("=" * 60)
    for pos, text, src, basis in results:
        print(f"[{pos:>3}] {text}    （取 {src}；{basis}）")

    print()
    print("=" * 60)
    print("抉择历史（可追溯）")
    print("=" * 60)
    if not decisions:
        print("（无异文，无抉择）")
    for d in decisions:
        vs = "；".join(f"{v}「{c}」" for v, c in sorted(d.variants.items()))
        parents = ", ".join(d.parents) if d.parents else "无"
        print(f"{d.did} @位置{d.position}: 异文 {vs}")
        print(f"    => 取 {d.chosen}；依据：{d.basis}；父抉择：{parents}")

    print()
    print("=" * 60)
    print(f"错误清单（共 {len(errors)} 条）")
    print("=" * 60)
    if not errors:
        print("（无错误）")
    for i, e in enumerate(errors, 1):
        print(f"[E{i}] {e.kind}: {e.message}")


# ---------------------------------------------------------------- 内置示例

DEMO = """\
# 版本定义：甲本为祖本最权威，乙本次之，丙本最末
VERSION 甲本 100
VERSION 乙本 60
VERSION 丙本 30

# 抉择规则：
#   r1 含"虫"的异文取甲本（甲本用字存古，乙本多俗体）
#   r2 位置4取乙本（该处乙本经名家校订）——注意它与 r1 的抉择会构成传播环
RULE r1 WHEN has:虫 PREFER 甲本
RULE r2 WHEN pos=4 PREFER 乙本

TEXT 甲本 大道之行也
TEXT 甲本 天下为公
TEXT 甲本 蟲鱼之学
TEXT 甲本 礼运大同
TEXT 甲本 5 甲乙丙丁
TEXT 乙本 大道之行也
TEXT 乙本 天下为公
TEXT 乙本 虫鱼之学
TEXT 乙本 礼运大同篇
TEXT 乙本 6 仁义礼智
TEXT 丙本 大道之行也
TEXT 丙本 天下为公
TEXT 丙本 蟲鱼之学
TEXT 丙本 5 甲乙丙盯
TEXT 丙本 6 仁义礼知
TEXT 丁本 9 幽灵之文
TEXT 甲本 2 重出之文
"""


# ---------------------------------------------------------------- 入口

def main(argv):
    if "--demo" in argv:
        text = DEMO
    elif len(argv) > 1:
        with open(argv[1], encoding="utf-8") as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    versions, rules, entries, errors = parse(text)

    # 规则倾向引用的版本必须已定义
    valid_rules = []
    for r in rules:
        if r.prefer not in ("high", "low") and r.prefer not in versions:
            errors.append(Err(
                "unknown_rule_target",
                f"规则 {r.rid} 的倾向版本「{r.prefer}」未定义，该规则已忽略"))
            continue
        valid_rules.append(r)

    engine = Engine(versions, valid_rules)
    results, decisions, eng_errors = engine.run(entries)
    all_errors = errors + eng_errors
    print_report(results, decisions, all_errors)
    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
