#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""古籍校勘级联抉择工具（单文件，纯 Python 标准库）。

输入为单个文本文件（或标准输入），分三节，节名用方括号：

    [versions]            每行: 版本名 优先级(整数, 大者优先)
    [rules]               每行: 规则名 条件 倾向
    [text]                每行: 版本名 [位置] 内容...

说明：
- 空行与 # 开头的行为注释。
- [text] 节中若第二个字段是整数则视为显式位置，否则位置取该行在文本流中的序号。
  同一位置出现多个版本即构成异文候选；内容完全相同则不视为异文。
- 规则条件（自定，理由见下）：
    always            匹配任意异文（兜底规则）
    contains:子串     任一版本内容包含子串（古籍中常按用字/通假特征裁决）
    involves:甲,乙    异文版本集合包含所列全部版本（按版本来源关系裁决）
    regex:模式        任一版本内容匹配正则（按文例/句式裁决）
- 规则倾向：
    prefer:版本       该版本在异文中即选之（版本不在异文中则规则不适用）
    priority          取静态优先级最高者
    credible          取动态可信度最高者（级联核心，见下）

级联模型：每个版本有动态可信度，初值 0。每次规则抉择使胜出方 +1、
其余异文版本 -1，并沿文本流向后传播，故前文抉择会改变后文异文的裁决
（credible 倾向读取的是被前文抉择修改过的可信度）。

传播环：抉择 j 读取了抉择 i 写入的可信度（i 影响 j），而 j 的抉择又改写了
i 裁决时所读取的可信度（j 反过来改变 i 的依据），即构成相互影响。对抉择
依赖图求强连通分量，大小 >= 2（或自环）的分量即传播环，环上抉择全部报告。

退出码：存在错误报告时为 1，否则为 0。
"""

import argparse
import re
import shlex
import sys
from dataclasses import dataclass, field


SAMPLE = """\
# 示例输入
[versions]
甲 30
乙 20
丙 10

[rules]
R1 involves:甲,乙 credible
R2 contains:也 prefer:丙

[text]
甲 1 学而时习之
乙 1 学而时习之哉
甲 2 不亦说乎
甲 2 不亦悦乎
乙 2 不亦说乎
丁 3 无名氏之文
乙 3 有朋自远方来
丙 3 有朋自远方来
甲 4 人不知而不愠
丙 4 人不知而不愠也
乙 5 君子务本
丙 5 君子務本
甲 6 孝弟也者
乙 6 孝悌也者
"""


@dataclass
class Rule:
    name: str
    condition: str
    tendency: str
    lineno: int


@dataclass
class Decision:
    ident: int
    position: int
    variants: dict
    rule: str
    winner: str
    scores_before: dict
    scores_after: dict
    reads: set
    writes: set
    deps: set
    note: str = ""


def parse_input(text):
    versions = {}
    rules = []
    entries = []
    errors = []
    section = None
    auto_pos = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in ("versions", "rules", "text"):
                errors.append("第%d行: 未知节名 [%s]" % (lineno, section))
                section = None
            continue
        if section == "versions":
            parts = line.split()
            if len(parts) != 2 or not re.fullmatch(r"-?\d+", parts[1]):
                errors.append("第%d行: 版本定义格式错误（应为: 名称 优先级整数）: %s" % (lineno, line))
                continue
            name, prio = parts[0], int(parts[1])
            if name in versions:
                errors.append("第%d行: 版本重复定义: %s" % (lineno, name))
                continue
            versions[name] = prio
        elif section == "rules":
            try:
                parts = shlex.split(line)
            except ValueError as exc:
                errors.append("第%d行: 规则解析失败: %s" % (lineno, exc))
                continue
            if len(parts) != 3:
                errors.append("第%d行: 规则格式错误（应为: 规则名 条件 倾向）: %s" % (lineno, line))
                continue
            rules.append(Rule(parts[0], parts[1], parts[2], lineno))
        elif section == "text":
            parts = line.split(None, 2)
            if len(parts) < 2:
                errors.append("第%d行: 文本行格式错误（应为: 版本 [位置] 内容）: %s" % (lineno, line))
                continue
            version = parts[0]
            if len(parts) == 3 and re.fullmatch(r"\d+", parts[1]):
                position, content = int(parts[1]), parts[2]
            else:
                auto_pos += 1
                position = auto_pos
                content = parts[1] if len(parts) == 2 else parts[1] + " " + parts[2]
            entries.append((lineno, version, position, content))
        else:
            errors.append("第%d行: 内容不在任何节内: %s" % (lineno, line))
    return versions, rules, entries, errors


def validate_rules(rules, versions, errors):
    for rule in rules:
        cond = rule.condition
        if cond == "always":
            pass
        elif cond.startswith("contains:"):
            if not cond[len("contains:"):]:
                errors.append("第%d行: 规则 %s 的 contains 条件为空" % (rule.lineno, rule.name))
        elif cond.startswith("involves:"):
            for name in cond[len("involves:"):].split(","):
                if name and name not in versions:
                    errors.append("第%d行: 规则 %s 条件引用了不存在的版本: %s" % (rule.lineno, rule.name, name))
        elif cond.startswith("regex:"):
            try:
                re.compile(cond[len("regex:"):])
            except re.error as exc:
                errors.append("第%d行: 规则 %s 正则无效: %s" % (rule.lineno, rule.name, exc))
        else:
            errors.append("第%d行: 规则 %s 条件未知: %s" % (rule.lineno, rule.name, cond))
        tend = rule.tendency
        if tend.startswith("prefer:"):
            name = tend[len("prefer:"):]
            if name not in versions:
                errors.append("第%d行: 规则 %s 倾向引用了不存在的版本: %s" % (rule.lineno, rule.name, name))
        elif tend not in ("priority", "credible"):
            errors.append("第%d行: 规则 %s 倾向未知: %s" % (rule.lineno, rule.name, tend))


def condition_matches(cond, variants):
    if cond == "always":
        return True
    if cond.startswith("contains:"):
        sub = cond[len("contains:"):]
        return any(sub in c for c in variants.values())
    if cond.startswith("involves:"):
        names = [n for n in cond[len("involves:"):].split(",") if n]
        return all(n in variants for n in names)
    if cond.startswith("regex:"):
        try:
            rx = re.compile(cond[len("regex:"):])
        except re.error:
            return False
        return any(rx.search(c) for c in variants.values())
    return False


def resolve_tendency(tendency, variants, prio, cred):
    """返回 (胜出版本, 读取的版本集)；规则不适用时返回 None。"""
    if tendency.startswith("prefer:"):
        name = tendency[len("prefer:"):]
        if name in variants:
            return name, set()
        return None
    if tendency == "priority":
        winner = max(variants, key=lambda v: (prio[v], v))
        return winner, set()
    if tendency == "credible":
        reads = set(variants)
        winner = max(variants, key=lambda v: (cred[v], prio[v], v))
        return winner, reads
    return None


def find_sccs(nodes, adj):
    visited = set()
    order = []
    for start in nodes:
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(adj[start])))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(sorted(adj[nxt]))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()
    radj = {n: set() for n in nodes}
    for u in nodes:
        for v in adj[u]:
            radj[v].add(u)
    visited.clear()
    sccs = []
    for node in reversed(order):
        if node in visited:
            continue
        comp = []
        visited.add(node)
        stack = [node]
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in radj[cur]:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        sccs.append(sorted(comp))
    return sccs


def run(text):
    errors = []
    versions, rules, entries, parse_errors = parse_input(text)
    errors.extend(parse_errors)
    validate_rules(rules, versions, errors)

    positions = {}
    for lineno, version, position, content in entries:
        if version not in versions:
            errors.append("第%d行: 文本引用了不存在的版本: %s" % (lineno, version))
            continue
        slot = positions.setdefault(position, {})
        if version in slot:
            errors.append("第%d行: 版本 %s 在位置 %d 重复出现（保留首次内容，忽略: %s）"
                          % (lineno, version, position, content))
            continue
        slot[version] = content

    prio = versions
    cred = {name: 0 for name in versions}
    last_writer = {}
    decisions = []
    results = {}

    for position in sorted(positions):
        variants = positions[position]
        distinct = set(variants.values())
        if len(distinct) == 1:
            source = max(variants, key=lambda v: (prio[v], v))
            basis = "唯一版本" if len(variants) == 1 else "各版本内容一致"
            results[position] = (variants[source], source, basis)
            continue

        chosen = None
        for rule in rules:
            if not condition_matches(rule.condition, variants):
                continue
            resolved = resolve_tendency(rule.tendency, variants, prio, cred)
            if resolved is None:
                continue
            winner, reads = resolved
            chosen = (rule, winner, reads)
            break

        if chosen is None:
            detail = ", ".join("%s: %s" % (v, c) for v, c in sorted(variants.items()))
            errors.append("位置 %d: 异文无规则覆盖（%s）；按静态优先级暂取" % (position, detail))
            winner = max(variants, key=lambda v: (prio[v], v))
            results[position] = (variants[winner], winner, "无规则覆盖，按静态优先级")
            decisions.append(Decision(
                ident=len(decisions) + 1, position=position, variants=dict(variants),
                rule="(无)", winner=winner,
                scores_before=dict(cred), scores_after=dict(cred),
                reads=set(), writes=set(), deps=set(),
                note="无规则覆盖的兜底，不改动可信度"))
            continue

        rule, winner, reads = chosen
        scores_before = dict(cred)
        deps = set()
        for name in reads:
            if name in last_writer:
                deps.add(last_writer[name])
        losers = [v for v in variants if v != winner]
        cred[winner] += 1
        for loser in losers:
            cred[loser] -= 1
        writes = set(variants)
        ident = len(decisions) + 1
        for name in writes:
            last_writer[name] = ident
        decisions.append(Decision(
            ident=ident, position=position, variants=dict(variants),
            rule="%s[%s]" % (rule.name, rule.tendency), winner=winner,
            scores_before=scores_before, scores_after=dict(cred),
            reads=reads, writes=writes, deps=deps))
        results[position] = (variants[winner], winner, "规则 %s" % rule.name)

    adj = {d.ident: set() for d in decisions}
    by_id = {d.ident: d for d in decisions}
    for d in decisions:
        for dep in d.deps:
            to_add = dep
            adj[to_add].add(d.ident)
    for j in decisions:
        for i in decisions:
            if i.ident < j.ident and (j.writes & i.reads):
                adj[j.ident].add(i.ident)
    for comp in find_sccs(sorted(adj), adj):
        is_cycle = len(comp) > 1 or (comp and comp[0] in adj[comp[0]])
        if not is_cycle:
            continue
        desc = " -> ".join("位置%d(规则%s)" % (by_id[i].position, by_id[i].rule) for i in comp)
        errors.append("抉择传播环: %s 相互影响，环上抉择需人工复核" % desc)

    return errors, results, decisions


def print_report(results, decisions, errors):
    print("=== 校勘结果 ===")
    if not results:
        print("(无文本)")
    for position in sorted(results):
        content, source, basis = results[position]
        print("位置 %-4d %s  （底本: %s；依据: %s）" % (position, content, source, basis))

    print()
    print("=== 抉择历史 ===")
    if not decisions:
        print("(无异文抉择)")
    for d in decisions:
        var_desc = ", ".join("%s: %s" % (v, c) for v, c in sorted(d.variants.items()))
        before = ", ".join("%s:%d" % (v, d.scores_before[v]) for v in sorted(d.scores_before))
        after = ", ".join("%s:%d" % (v, d.scores_after[v]) for v in sorted(d.scores_after))
        deps = ", ".join("#%d" % i for i in sorted(d.deps)) if d.deps else "无"
        line = ("#%d 位置%d 异文{%s} 规则%s 胜出:%s 可信度前{%s} 后{%s} 依赖抉择:%s"
                % (d.ident, d.position, var_desc, d.rule, d.winner, before, after, deps))
        if d.note:
            line += " 备注:" + d.note
        print(line)

    print()
    print("=== 错误清单 ===")
    if not errors:
        print("(无错误)")
    for idx, err in enumerate(errors, 1):
        print("%d. %s" % (idx, err))


def main(argv=None):
    parser = argparse.ArgumentParser(description="古籍校勘级联抉择工具（单文件，纯标准库）")
    parser.add_argument("input", nargs="?", help="输入文件路径；缺省或 - 表示标准输入")
    parser.add_argument("--sample", action="store_true", help="打印示例输入后退出")
    args = parser.parse_args(argv)

    if args.sample:
        sys.stdout.write(SAMPLE)
        return 0
    if not args.input or args.input == "-":
        text = sys.stdin.read()
    else:
        with open(args.input, encoding="utf-8") as fh:
            text = fh.read()
    errors, results, decisions = run(text)
    print_report(results, decisions, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
