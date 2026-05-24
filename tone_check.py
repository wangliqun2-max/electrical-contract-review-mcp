"""扫描 docx 文件的所有批注，检测不专业的措辞

用法：
    python3 tone_check.py <annotated.docx>
    python3 tone_check.py <comments.xml>      # 直接检查解包后的 comments.xml
    python3 tone_check.py <config.py>          # 检查 comments_config.py 中的批注配置

退出码：
    0 - 通过（无问题或仅假阳性）
    1 - 发现问题词
"""

import argparse
import importlib.util
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile


# 问题词清单（分类）
PROBLEMATIC_WORDS = {
    # 符号
    '！': '感叹号',
    
    # 绝对化措辞
    '严格': '绝对化',
    '硬性': '绝对化',
    '不可妥协': '绝对化',
    '完全': '绝对化',
    
    # 命令式
    '必须': '命令式',
    
    # 主观评价副词
    '过于': '主观评价',
    '过高': '主观评价',
    '过短': '主观评价',
    '过长': '主观评价',
    '过宽': '主观评价',
    '过严': '主观评价',
    '过低': '主观评价',
    
    # 过度强调
    '远超': '过度强调',
    '严重违反': '过度强调',
    '严重偏离': '过度强调',
    '严重不足': '过度强调',
    '高达': '过度强调',
    '强烈': '过度强调',
    '极度': '过度强调',
    '极为': '过度强调',
    '极其': '过度强调',
    
    # 口语化
    '白白': '口语化',
    '死板': '口语化',
    '钻空子': '口语化',
}

# 假阳性白名单：在以下上下文中包含问题词，不算违规
FALSE_POSITIVE_CONTEXTS = [
    # "较为严格" / "较为严苛" 是中性程度描述
    "较为严格",
    "较为严苛",
    "较为严重",  # 一般不出现，但安全起见
    # "相当于" 是客观换算用语
    "相当于",
]


def extract_comments_from_docx(docx_path):
    """从 docx 文件提取批注内容
    
    返回 [(comment_id, comment_text), ...] 列表
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(docx_path, 'r') as z:
            try:
                z.extract('word/comments.xml', tmpdir)
            except KeyError:
                return []  # 没有批注
        return extract_comments_from_xml(os.path.join(tmpdir, 'word', 'comments.xml'))


def extract_comments_from_xml(xml_path):
    """从 comments.xml 提取批注内容"""
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    comments = []
    for c in root.findall('w:comment', ns):
        cid = c.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}id')
        text = ''.join(t.text or '' for t in c.iter(
            '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'
        ))
        comments.append((cid, text))
    return comments


def extract_comments_from_config(config_path):
    """从 comments_config.py 提取批注内容"""
    spec = importlib.util.spec_from_file_location("cfg", config_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    
    comments = []
    for i, item in enumerate(mod.COMMENTS):
        if len(item) >= 2:
            text = item[-1]  # 最后一项总是批注内容
            comments.append((str(i), text))
    return comments


def check_text(text):
    """检查一段文本的措辞问题
    
    返回 [(word, label, count), ...] 列表
    """
    issues = []
    
    # 检查 Markdown 加粗
    md_bold = text.count('**')
    if md_bold > 0:
        issues.append(('**', 'Markdown加粗', md_bold))
    
    # 检查每个问题词
    for word, label in PROBLEMATIC_WORDS.items():
        if word not in text:
            continue
        
        # 计算总出现次数
        total = text.count(word)
        
        # 扣除假阳性次数
        false_positive_count = 0
        for fp_context in FALSE_POSITIVE_CONTEXTS:
            if word in fp_context:
                # 这个问题词在某个假阳性上下文中
                false_positive_count += text.count(fp_context)
        
        real_count = total - false_positive_count
        if real_count > 0:
            issues.append((word, label, real_count))
    
    return issues


def main():
    parser = argparse.ArgumentParser(description="扫描批注的措辞问题")
    parser.add_argument("input_file", help="docx 文件、comments.xml 或 comments_config.py")
    parser.add_argument("--verbose", action="store_true", help="显示每条批注的完整文本")
    args = parser.parse_args()
    
    if args.input_file.endswith('.docx'):
        comments = extract_comments_from_docx(args.input_file)
    elif args.input_file.endswith('.xml'):
        comments = extract_comments_from_xml(args.input_file)
    elif args.input_file.endswith('.py'):
        comments = extract_comments_from_config(args.input_file)
    else:
        print(f"不支持的文件类型：{args.input_file}")
        sys.exit(2)
    
    print(f"批注总数：{len(comments)}")
    print()
    
    total_issues = 0
    problem_comments = 0
    for cid, text in comments:
        issues = check_text(text)
        if issues:
            problem_comments += 1
            total_issues += sum(c for _, _, c in issues)
            details = ", ".join(f"{w}({l})×{c}" for w, l, c in issues)
            print(f"#{cid} ⚠️  {details}")
            if args.verbose:
                print(f"    原文：{text[:200]}")
        else:
            print(f"#{cid} ✓")
    
    print()
    print(f"========== 含问题批注：{problem_comments}/{len(comments)} | 问题数：{total_issues} ==========")
    
    if total_issues > 0:
        print()
        print("建议：参见 references/tone-guide.md，将问题词替换为推荐的中性表述")
        sys.exit(1)
    else:
        print()
        print("✓ 通过措辞专业化检查")
        sys.exit(0)


if __name__ == "__main__":
    main()
