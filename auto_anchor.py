"""
auto_anchor.py — 自动锚点生成器

核心问题：Word 文档在 XML 层面，连续文本常被切分到多个 <w:r> run 中，
导致"肉眼看起来连续"的文字在 XML 里找不到。

解决思路：
  给定目标文字，自动生成并测试候选锚点，返回能在 XML 中唯一定位的最短有效锚点。

策略（按优先级）：
  1. 原文直接匹配（最理想）
  2. 去除数字/百分号/字母等易被切分字符后，取最长纯中文连续片段
  3. 滑动窗口扫描，找最长的能匹配的子串
  4. 降级：返回能匹配（即使重复）的最优候选，并给出警告

用法：
  # 直接调用
  from auto_anchor import find_best_anchor
  anchor = find_best_anchor(doc_xml_content, "质量保函需由国有商业银行开具，有效期覆盖质保期届满后60日")
  # => "质量保函需由国有商业银行开具"

  # 批量处理
  anchors = find_anchors_for_comments(doc_xml_content, [
      ("质量保函需由国有商业银行开具，有效期覆盖质保期届满后60日", "批注内容1"),
      ("迟交1-2周，每周支付迟交货物金额的1％违约金", "批注内容2"),
  ])
"""

import re
from typing import Optional


# ── 核心函数 ────────────────────────────────────────────────

def find_best_anchor(doc_xml: str, target_text: str,
                     min_length: int = 5,
                     require_unique: bool = False) -> Optional[str]:
    """
    给定目标文字，在 doc_xml 中找到最佳可用锚点。

    Args:
        doc_xml:        word/document.xml 的完整字符串内容
        target_text:    希望锚定的合同原文片段（可以包含数字、标点）
        min_length:     锚点最短字符数（默认 5，过短会误匹配）
        require_unique: True = 只返回唯一匹配的锚点；False = 允许多匹配但给出警告

    Returns:
        可用锚点字符串，或 None（找不到任何可用锚点）
    """
    target_text = target_text.strip()

    # 策略1：原文直接能匹配？
    result = _try_anchor(doc_xml, target_text, require_unique)
    if result:
        return result

    # 策略2：切分出纯中文片段，按长度降序测试
    segments = _extract_chinese_segments(target_text, min_length)
    for seg in segments:
        result = _try_anchor(doc_xml, seg, require_unique)
        if result:
            return result

    # 策略3：滑动窗口——从目标文字中截取子串，从长到短测试
    result = _sliding_window_search(doc_xml, target_text, min_length, require_unique)
    if result:
        return result

    # 策略4：放宽 require_unique 限制，返回第一个能匹配的候选（多匹配时用第一处）
    if require_unique:
        return find_best_anchor(doc_xml, target_text, min_length, require_unique=False)

    return None


def find_anchors_for_comments(doc_xml: str,
                               comments: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """
    批量为批注列表寻找最佳锚点。

    Args:
        doc_xml:   word/document.xml 的完整字符串内容
        comments:  [(target_text, comment_text), ...] 的列表

    Returns:
        [(anchor, original_target, comment_text), ...] 的列表
        - anchor: 找到的最佳锚点（失败时为 None）
        - original_target: 原始目标文字（用于调试）
        - comment_text: 原批注内容
    """
    results = []
    for i, (target, comment_text) in enumerate(comments):
        anchor = find_best_anchor(doc_xml, target)
        if anchor is None:
            print(f"⚠️  Comment {i}: 未找到可用锚点 [{target[:40]}...]")
        elif anchor != target:
            print(f"ℹ️  Comment {i}: 锚点已自动降级")
            print(f"     原目标: {target[:60]}")
            print(f"     实际锚点: {anchor}")
        else:
            print(f"✓  Comment {i}: 锚点直接匹配")
        results.append((anchor, target, comment_text))
    return results


def validate_all_anchors(doc_xml: str, anchors: list[Optional[str]]) -> dict:
    """
    验证一组锚点，返回统计报告。

    Returns:
        {
          "total": int,
          "success": int,
          "failed": int,
          "multi_match": int,   # 锚点在文档中出现多次（可能定位到错误位置）
          "details": [(idx, status, anchor, count), ...]
        }
    """
    report = {"total": len(anchors), "success": 0, "failed": 0,
              "multi_match": 0, "details": []}

    for i, anchor in enumerate(anchors):
        if anchor is None:
            report["failed"] += 1
            report["details"].append((i, "FAILED", None, 0))
            continue
        count = doc_xml.count(anchor)
        if count == 0:
            report["failed"] += 1
            report["details"].append((i, "NOT_FOUND", anchor, 0))
        elif count == 1:
            report["success"] += 1
            report["details"].append((i, "OK", anchor, 1))
        else:
            report["multi_match"] += 1
            report["success"] += 1  # 仍可用，但用第一处
            report["details"].append((i, "MULTI", anchor, count))

    return report


# ── 内部辅助函数 ─────────────────────────────────────────────

def _try_anchor(doc_xml: str, candidate: str, require_unique: bool) -> Optional[str]:
    """测试 candidate 是否可作为锚点，可用则返回，否则返回 None"""
    if not candidate or len(candidate) < 1:
        return None
    count = doc_xml.count(candidate)
    if count == 0:
        return None
    if require_unique and count > 1:
        return None
    return candidate


def _extract_chinese_segments(text: str, min_length: int) -> list[str]:
    """
    把目标文字按非中文字符切分，提取纯中文连续片段。
    按长度降序排列（优先试最长的片段）。

    例：
    "迟交1-2周，每周支付迟交货物金额的1％违约金"
    => ["每周支付迟交货物金额的", "迟交", "周", "违约金"]
    => 过滤 min_length 后 => ["每周支付迟交货物金额的"]
    """
    # 按"非中文字符"切分（数字、字母、标点、空格等）
    pattern = r'[^\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]+'
    segments = re.split(pattern, text)
    # 过滤长度不足的
    segments = [s.strip() for s in segments if len(s.strip()) >= min_length]
    # 按长度降序
    segments.sort(key=len, reverse=True)
    # 去重（保持顺序）
    seen = set()
    unique_segments = []
    for s in segments:
        if s not in seen:
            seen.add(s)
            unique_segments.append(s)
    return unique_segments


def _sliding_window_search(doc_xml: str, text: str,
                            min_length: int, require_unique: bool,
                            max_candidates: int = 30) -> Optional[str]:
    """
    在目标文字中用滑动窗口截取子串，从长到短测试能否在 doc_xml 中匹配。
    窗口从整个文字长度开始，逐步缩短至 min_length。
    每个长度只测试前几个起始位置（避免组合爆炸）。
    """
    text_len = len(text)
    for window_size in range(text_len, min_length - 1, -1):
        candidates = []
        # 在当前窗口大小下，采样若干起始位置
        step = max(1, (text_len - window_size) // 5)
        for start in range(0, text_len - window_size + 1, step):
            sub = text[start:start + window_size]
            # 只取以中文字符开头和结尾的子串（避免首尾是标点/数字）
            if not _is_chinese_char(sub[0]) or not _is_chinese_char(sub[-1]):
                continue
            candidates.append(sub)
        # 去重
        candidates = list(dict.fromkeys(candidates))
        # 限制候选数量
        candidates = candidates[:max_candidates]
        for candidate in candidates:
            result = _try_anchor(doc_xml, candidate, require_unique)
            if result:
                return result
    return None


def _is_chinese_char(ch: str) -> bool:
    """判断单个字符是否为中文字符"""
    return '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'


# ── 命令行模式 ───────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="自动锚点生成器——给定目标文字，在 Word XML 中找到可用锚点"
    )
    parser.add_argument("doc_xml_path", help="word/document.xml 文件路径")
    parser.add_argument("target", help="目标文字（希望批注的合同原文片段）")
    parser.add_argument("--min-length", type=int, default=5, help="最短锚点长度（默认5）")
    parser.add_argument("--unique", action="store_true", help="只接受唯一匹配的锚点")
    args = parser.parse_args()

    with open(args.doc_xml_path, "r", encoding="utf-8") as f:
        xml = f.read()

    anchor = find_best_anchor(xml, args.target, args.min_length, args.unique)

    if anchor is None:
        print(f"❌ 未找到可用锚点: {args.target}")
        sys.exit(1)
    elif anchor == args.target:
        print(f"✓ 直接匹配: {anchor}")
    else:
        print(f"✓ 降级锚点: {anchor}")
        print(f"  原目标: {args.target}")
    count = xml.count(anchor)
    print(f"  文档中出现次数: {count}")
