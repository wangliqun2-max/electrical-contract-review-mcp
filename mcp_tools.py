"""
电气合同审核 MCP Server（重构版）
HTTP 传输模式 — Railway 部署

五个工具：
  upload_contract     — 上传合同文件（base64），返回 file_id
  load_contract       — 通过 session_id 加载网页端上传的合同
  insert_comments     — 根据 file_id 嵌入批注，返回结果 base64
  check_comment_tone  — 批注措辞检查
  get_legal_rules     — 查询内置法务规则

附带 Web 上传门户：用户在网页上传合同获取 session_id，
再将 session_id 提供给 bisheng 助手调用 load_contract 完成审核。
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from mcp.server.fastmcp import FastMCP

_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _DIR)

from auto_anchor import find_best_anchor
from tone_check  import check_text

# 内存缓存：存放已上传的合同文件
_file_cache: dict = {}
_CACHE_TTL  = 3600  # 1 小时过期

port = int(os.environ.get("PORT", 8000))

mcp = FastMCP(
    "电气合同审核工具",
    host="0.0.0.0",
    port=port,
    instructions=(
        "本工具用于审核电气制造领域的合同（储能、变压器、开关柜、风机、光伏等），"
        "将审核意见以批注形式嵌入 Word 文档原文。\n\n"
        "标准流程（推荐 — 用户通过网页上传合同）：\n"
        "1. 向用户索要 session_id（用户在上传页面获取）\n"
        "2. load_contract(session_id) → 确认文件已加载，获取 file_id\n"
        "3. 审核合同内容，根据 get_legal_rules 中的规则生成批注 JSON\n"
        "4. check_comment_tone(comments_json) → 确认措辞无问题\n"
        "5. insert_comments(file_id, comments_json) → 完成批注嵌入\n"
        "6. 告知用户回到上传页面下载批注版合同\n\n"
        "备用流程（直接传入 base64）：\n"
        "1. upload_contract(contract_base64, filename) → file_id\n"
        "2. check_comment_tone(comments_json) → 通过后继续\n"
        "3. insert_comments(file_id, comments_json) → result_base64"
    ),
)


@mcp.tool()
def upload_contract(contract_base64: str, filename: str) -> str:
    """
    上传合同文件到服务器，返回 file_id 供后续 insert_comments 使用。

    Args:
        contract_base64: 合同文件的 base64 编码内容（.doc 或 .docx）
        filename:        原始文件名，如 "采购合同.docx"

    Returns:
        JSON：{"status","file_id","filename","message"}
    """
    _cleanup_cache()

    try:
        file_bytes = base64.b64decode(contract_base64)
    except Exception as e:
        return _err(f"base64 解码失败: {e}")

    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".doc", ".docx"):
        return _err(f"不支持的文件格式: {ext}，仅支持 .doc / .docx")

    tmpdir     = tempfile.mkdtemp(prefix="contract_")
    input_path = os.path.join(tmpdir, f"contract{ext}")
    with open(input_path, "wb") as f:
        f.write(file_bytes)

    docx_path = _ensure_docx(input_path, tmpdir)
    if not docx_path:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return _err("文件转换失败，请检查文件是否损坏")

    file_id = str(uuid.uuid4())[:8]
    _file_cache[file_id] = {
        "path":       docx_path,
        "tmpdir":     tmpdir,
        "filename":   filename,
        "created_at": time.time(),
    }

    return json.dumps({
        "status":   "success",
        "file_id":  file_id,
        "filename": filename,
        "message":  f"上传成功，file_id={file_id}（有效期1小时）。请调用 insert_comments 嵌入批注。",
    }, ensure_ascii=False)


@mcp.tool()
def load_contract(session_id: str) -> str:
    """
    通过 session_id 加载用户在网页端上传的合同文件。

    用户在上传页面（Web 门户）上传合同后，会获得一个 session_id。
    用户将此 session_id 粘贴到对话框，你调用此工具即可加载合同。

    Args:
        session_id: 用户从上传页面获取的会话标识（8位字符）

    Returns:
        JSON：{"status","file_id","filename","message"}
        返回的 file_id 可直接用于 insert_comments
    """
    _cleanup_cache()

    if session_id not in _file_cache:
        return _err(f"session_id '{session_id}' 不存在或已过期（1小时），请让用户重新上传")

    cached = _file_cache[session_id]
    if not os.path.exists(cached["path"]):
        del _file_cache[session_id]
        return _err("文件已失效，请让用户重新上传")

    return json.dumps({
        "status":   "success",
        "file_id":  session_id,
        "filename": cached["filename"],
        "message":  f"合同文件 '{cached['filename']}' 已加载（file_id={session_id}）。"
                    "请审核合同内容，生成批注后调用 check_comment_tone 和 insert_comments。",
    }, ensure_ascii=False)


@mcp.tool()
def insert_comments(
    file_id: str,
    comments_json: str,
    author: str = "乙方法律顾问",
) -> str:
    """
    将批注嵌入已上传的合同文件，返回批注版文件的 base64 编码。

    需要先调用 upload_contract 获取 file_id。

    Args:
        file_id:       upload_contract 返回的文件标识
        comments_json: 批注列表 JSON，格式：
                       [{"target": "合同原文片段（纯中文8-20字）", "text": "批注内容"}, ...]
                       target 应为合同中连续的中文片段，不含数字/百分号/标点
        author:        批注作者名，默认"乙方法律顾问"

    Returns:
        JSON：{"status","result_base64","output_filename","total","inserted",
               "fallback_count","not_found","message"}
    """
    if file_id not in _file_cache:
        return _err(f"file_id '{file_id}' 不存在或已过期，请重新调用 upload_contract")

    cached    = _file_cache[file_id]
    docx_path = cached["path"]
    filename  = cached["filename"]

    if not os.path.exists(docx_path):
        del _file_cache[file_id]
        return _err("文件已失效，请重新调用 upload_contract 上传")

    try:
        comments_list = json.loads(comments_json)
    except json.JSONDecodeError as e:
        return _err(f"comments_json 格式错误: {e}")

    with tempfile.TemporaryDirectory() as workdir:
        work_docx = os.path.join(workdir, "contract.docx")
        shutil.copy2(docx_path, work_docx)

        unpacked = os.path.join(workdir, "unpacked")
        ok, msg  = _unpack_docx(work_docx, unpacked)
        if not ok:
            return _err(f"解包失败: {msg}")

        doc_xml_path = os.path.join(unpacked, "word", "document.xml")
        if not os.path.exists(doc_xml_path):
            return _err("无法找到 document.xml，文件可能损坏")

        with open(doc_xml_path, "r", encoding="utf-8") as f:
            doc_xml = f.read()

        resolved     = []
        not_found    = []
        fallback_cnt = 0

        for i, item in enumerate(comments_list):
            target = item.get("target", "").strip()
            text   = item.get("text",   "").strip()
            if not target or not text:
                continue
            if doc_xml.count(target) >= 1:
                resolved.append((i, target, text))
            else:
                fb = find_best_anchor(doc_xml, target)
                if fb and doc_xml.count(fb) >= 1:
                    resolved.append((i, fb, text))
                    fallback_cnt += 1
                else:
                    not_found.append(target[:60])

        if not resolved:
            return json.dumps({
                "status":    "failed",
                "message":   "所有批注均无法定位锚点。target 应为合同原文中连续的纯中文片段（8-20字，不含数字/百分号）",
                "not_found": not_found,
            }, ensure_ascii=False)

        new_xml = _insert_comment_markers(doc_xml, resolved)
        with open(doc_xml_path, "w", encoding="utf-8") as f:
            f.write(new_xml)

        _write_comment_defs(unpacked, resolved, author)

        stem            = os.path.splitext(filename)[0]
        output_filename = f"{stem}_批注版.docx"
        output_path     = os.path.join(workdir, output_filename)
        ok, msg = _pack_docx(unpacked, work_docx, output_path)
        if not ok:
            return _err(f"打包失败: {msg}")

        with open(output_path, "rb") as f:
            result_bytes = f.read()
            result_b64 = base64.b64encode(result_bytes).decode()

        # 将批注版文件存入缓存，供网页端下载
        result_tmpdir = tempfile.mkdtemp(prefix="result_")
        result_save_path = os.path.join(result_tmpdir, output_filename)
        with open(result_save_path, "wb") as f:
            f.write(result_bytes)

    # 更新缓存：标记为已完成，记录结果文件路径
    if file_id in _file_cache:
        _file_cache[file_id]["result_path"]     = result_save_path
        _file_cache[file_id]["result_filename"]  = output_filename
        _file_cache[file_id]["result_tmpdir"]    = result_tmpdir
        _file_cache[file_id]["result_ready"]     = True

    return json.dumps({
        "status":          "success" if not not_found else "partial",
        "result_base64":   result_b64,
        "output_filename": output_filename,
        "total":           len(comments_list),
        "inserted":        len(resolved),
        "fallback_count":  fallback_cnt,
        "not_found":       not_found,
        "message": (
            f"成功插入 {len(resolved)}/{len(comments_list)} 条批注"
            + (f"，{fallback_cnt} 条使用了降级锚点" if fallback_cnt else "")
            + (f"，{len(not_found)} 条未找到锚点已跳过" if not_found else "")
            + "。用户可在上传页面下载批注版合同。"
        ),
    }, ensure_ascii=False)


@mcp.tool()
def check_comment_tone(comments_json: str) -> str:
    """
    检查批注内容是否符合专业措辞规范。

    禁止：感叹号、"必须"、"严重违反"、"远超"、"过于"、"高达"、
          "完全"、"硬性"、"不可妥协"、Markdown加粗(**)等。

    Args:
        comments_json: [{"target":"...","text":"批注内容"}, ...]

    Returns:
        JSON：{"passed","total","problem_count","details","message"}
    """
    try:
        items = json.loads(comments_json)
    except json.JSONDecodeError as e:
        return _err(f"JSON 格式错误: {e}")

    details       = []
    problem_count = 0

    for i, item in enumerate(items):
        text   = item.get("text", "")
        issues = check_text(text)
        words  = [f"{w}({l})×{c}" for w, l, c in issues]
        if words:
            problem_count += 1
            details.append({
                "id":      i,
                "issues":  words,
                "preview": text[:80] + ("..." if len(text) > 80 else ""),
            })

    return json.dumps({
        "passed":        problem_count == 0,
        "total":         len(items),
        "problem_count": problem_count,
        "details":       details,
        "message": ("全部通过" if problem_count == 0
                    else f"{problem_count} 条含不专业措辞，请修改后重新检查"),
    }, ensure_ascii=False)


@mcp.tool()
def get_legal_rules(topic: str = "all") -> str:
    """
    查询内置法务规则，供生成批注时引用准确数据。

    Args:
        topic: "payment"|"penalty"|"warranty"|"dispute"|"guarantee"|"all"
    """
    rules = {
        "payment": {
            "description":    "标准付款节奏（卖方视角）",
            "schedule": [
                {"name": "预付款", "ratio": "20%", "trigger": "合同生效+履约保函到位",  "deadline_days": 15},
                {"name": "到货款", "ratio": "60%", "trigger": "货物到达+验收合格",      "deadline_days": 180},
                {"name": "投运款", "ratio": "10%", "trigger": "完成并网/系统投运",      "deadline_days": 360},
                {"name": "验收款", "ratio": "7%",  "trigger": "综合验收合格",           "deadline_days": 540},
                {"name": "质保金", "ratio": "3%",  "trigger": "质保期满且无遗留问题",   "deadline_days": 900},
            ],
            "forbidden_methods": ["商业承兑汇票", "电子商票"],
            "preferred_method":  "电汇",
        },
        "penalty": {
            "description":         "违约金标准",
            "per_week_limit":      "合同额的 0.1%/每周",
            "total_limit":         "合同额的 5%",
            "damages_total_limit": "合同额的 10%",
            "exclude_from_damages": ["间接损失", "利润损失", "惩罚性赔偿"],
        },
        "warranty": {
            "description": "质保期行业惯例",
            "by_equipment": {
                "变压器":            "24-36 个月",
                "开关柜/配电柜":     "24-36 个月",
                "储能系统(PCS/BMS)": "24-36 个月",
                "储能电芯":          "5-10 年（按容量保持率管理）",
                "风机":              "24-60 个月",
                "预制舱舱体":        "12-24 个月",
            },
            "preferred_start": "预验收证书签发之日",
            "latent_defect":   "质保期满后 1-2 年",
        },
        "dispute": {
            "preferred":    "诉讼",
            "jurisdiction": "被告所在地法院（或卖方所在地）",
            "avoid":        ["仲裁（一裁终局）", "甲方住所地法院"],
        },
        "guarantee": {
            "allowed_form":   "银行履约保函",
            "forbidden_form": ["电汇现金", "银行承兑汇票"],
            "typical_ratio":  "合同总价的 5%",
            "release":        "按实际履约进度分阶段释放",
        },
    }

    if topic == "all":
        return json.dumps(rules, ensure_ascii=False, indent=2)
    if topic in rules:
        return json.dumps(rules[topic], ensure_ascii=False, indent=2)
    return json.dumps({
        "error":     f"未知主题: {topic}",
        "available": list(rules.keys()) + ["all"],
    }, ensure_ascii=False)


# ── 内部函数 ─────────────────────────────────────────────────

def _err(msg: str) -> str:
    return json.dumps({"status": "failed", "message": msg}, ensure_ascii=False)


def _cleanup_cache() -> None:
    now     = time.time()
    expired = [fid for fid, info in _file_cache.items()
               if now - info["created_at"] > _CACHE_TTL]
    for fid in expired:
        shutil.rmtree(_file_cache[fid].get("tmpdir", ""), ignore_errors=True)
        del _file_cache[fid]


def _ensure_docx(path: str, tmpdir: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        dst = os.path.join(tmpdir, "contract.docx")
        shutil.copy2(path, dst)
        return dst
    if ext == ".doc":
        r = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "docx",
             path, "--outdir", tmpdir],
            capture_output=True, text=True
        )
        if r.returncode != 0:
            return None
        base = os.path.splitext(os.path.basename(path))[0]
        out  = os.path.join(tmpdir, f"{base}.docx")
        return out if os.path.exists(out) else None
    return None


def _unpack_docx(docx_path: str, output_dir: str) -> tuple[bool, str]:
    import zipfile
    try:
        with zipfile.ZipFile(docx_path, "r") as z:
            z.extractall(output_dir)
        return True, ""
    except Exception as e:
        return False, str(e)


def _pack_docx(unpacked_dir: str, original_docx: str,
               output_path: str) -> tuple[bool, str]:
    import zipfile
    try:
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
            ct = os.path.join(unpacked_dir, "[Content_Types].xml")
            if os.path.exists(ct):
                zout.write(ct, "[Content_Types].xml")
            for root, dirs, files in os.walk(unpacked_dir):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fname in files:
                    full    = os.path.join(root, fname)
                    arcname = os.path.relpath(full, unpacked_dir)
                    if arcname == "[Content_Types].xml":
                        continue
                    zout.write(full, arcname)
        return True, ""
    except Exception as e:
        return False, str(e)


def _find_run_positions(doc_xml: str, anchor: str) -> list[tuple[int, int]]:
    results = []
    pos = 0
    while True:
        idx = doc_xml.find(anchor, pos)
        if idx == -1:
            break
        r_start = max(doc_xml.rfind("<w:r>", 0, idx),
                      doc_xml.rfind("<w:r ", 0, idx))
        if r_start == -1:
            pos = idx + 1
            continue
        r_end = doc_xml.find("</w:r>", idx)
        if r_end == -1:
            pos = idx + 1
            continue
        r_end += len("</w:r>")
        results.append((r_start, r_end))
        pos = r_end
    return results


def _insert_comment_markers(doc_xml: str,
                              resolved: list[tuple[int, str, str]]) -> str:
    positioned = []
    for comment_id, anchor, _ in resolved:
        positions = _find_run_positions(doc_xml, anchor)
        if positions:
            positioned.append((comment_id, positions[0]))

    positioned.sort(key=lambda x: x[1][0], reverse=True)
    new_xml = doc_xml

    for comment_id, (r_start, r_end) in positioned:
        run_text    = new_xml[r_start:r_end]
        replacement = (
            f'<w:commentRangeStart w:id="{comment_id}"/>'
            + run_text
            + f'<w:commentRangeEnd w:id="{comment_id}"/>'
            + f'<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>'
            + f'<w:commentReference w:id="{comment_id}"/></w:r>'
        )
        new_xml = new_xml[:r_start] + replacement + new_xml[r_end:]

    return new_xml


def _write_comment_defs(unpacked_dir: str,
                         resolved: list[tuple[int, str, str]],
                         author: str) -> None:
    import xml.etree.ElementTree as ET

    W   = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    W14 = "http://schemas.microsoft.com/office/word/2010/wordml"

    def _w(tag): return f"{{{W}}}{tag}"

    ET.register_namespace("w",   W)
    ET.register_namespace("w14", W14)

    root = ET.Element(_w("comments"))
    root.set(f"{{{W14}}}docId", "1")

    for comment_id, anchor, text in resolved:
        c = ET.SubElement(root, _w("comment"))
        c.set(_w("id"),       str(comment_id))
        c.set(_w("author"),   author)
        c.set(_w("date"),     "2024-01-01T00:00:00Z")
        c.set(_w("initials"), author[:1])
        para = ET.SubElement(c, _w("p"))
        pPr  = ET.SubElement(para, _w("pPr"))
        pSty = ET.SubElement(pPr,  _w("pStyle"))
        pSty.set(_w("val"), "CommentText")
        run  = ET.SubElement(para, _w("r"))
        rPr  = ET.SubElement(run,  _w("rPr"))
        rSty = ET.SubElement(rPr,  _w("rStyle"))
        rSty.set(_w("val"), "CommentReference")
        ET.SubElement(run, _w("annotationRef"))
        run2 = ET.SubElement(para, _w("r"))
        t    = ET.SubElement(run2,  _w("t"))
        t.set("xml:space", "preserve")
        t.text = text

    word_dir     = os.path.join(unpacked_dir, "word")
    comments_xml = os.path.join(word_dir, "comments.xml")
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(comments_xml, xml_declaration=True,
               encoding="UTF-8", short_empty_elements=False)

    rels_path = os.path.join(word_dir, "_rels", "document.xml.rels")
    if os.path.exists(rels_path):
        with open(rels_path, "r", encoding="utf-8") as f:
            rels_content = f.read()
        if "comments.xml" not in rels_content:
            rels_content = rels_content.replace(
                "</Relationships>",
                '<Relationship Id="rIdComments" '
                'Type="http://schemas.openxmlformats.org/officeDocument'
                '/2006/relationships/comments" '
                'Target="comments.xml"/>\n</Relationships>'
            )
            with open(rels_path, "w", encoding="utf-8") as f:
                f.write(rels_content)


# ── 启动 ─────────────────────────────────────────────────────

import uvicorn
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.sse import SseServerTransport
from starlette.routing import Mount
from contextlib import asynccontextmanager

_FRONTEND_DIR = os.path.join(_DIR, "frontend")


@asynccontextmanager
async def lifespan(app):
    yield


fastapi_app = FastAPI(lifespan=lifespan)

# SSE 传输层
sse_transport = SseServerTransport("/messages/")

# 挂载消息处理路由
fastapi_app.router.routes.append(
    Mount("/messages", app=sse_transport.handle_post_message)
)


# ── Web 门户 API ─────────────────────────────────────────────

@fastapi_app.get("/")
async def serve_index():
    """返回合同上传页面"""
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))


@fastapi_app.post("/api/upload")
async def api_upload_contract(file: UploadFile = File(...)):
    """
    HTTP 文件上传端点（供前端网页调用）。
    接收 multipart/form-data 文件，存储到缓存，返回 session_id。
    """
    _cleanup_cache()

    filename = file.filename or "unknown.docx"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".doc", ".docx"):
        return JSONResponse(
            {"status": "failed", "message": f"不支持的文件格式: {ext}，仅支持 .doc / .docx"},
            status_code=400,
        )

    file_bytes = await file.read()
    if len(file_bytes) > 50 * 1024 * 1024:
        return JSONResponse(
            {"status": "failed", "message": "文件过大，限制 50MB"},
            status_code=400,
        )

    tmpdir     = tempfile.mkdtemp(prefix="contract_")
    input_path = os.path.join(tmpdir, f"upload_{uuid.uuid4().hex[:6]}{ext}")
    with open(input_path, "wb") as f:
        f.write(file_bytes)

    docx_path = _ensure_docx(input_path, tmpdir)
    if not docx_path:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return JSONResponse(
            {"status": "failed", "message": "文件转换失败，请检查文件是否损坏"},
            status_code=500,
        )

    session_id = str(uuid.uuid4())[:8]
    _file_cache[session_id] = {
        "path":         docx_path,
        "tmpdir":       tmpdir,
        "filename":     filename,
        "created_at":   time.time(),
        "result_ready": False,
    }

    return {
        "status":     "success",
        "session_id": session_id,
        "filename":   filename,
        "message":    f"上传成功，会话ID: {session_id}（有效期1小时）",
    }


@fastapi_app.get("/api/status/{session_id}")
async def api_check_status(session_id: str):
    """
    查询 session_id 对应文件的处理状态。
    """
    if session_id not in _file_cache:
        return {"status": "not_found", "message": "会话不存在或已过期"}

    cached = _file_cache[session_id]
    if cached.get("result_ready"):
        return {
            "status":          "done",
            "filename":        cached["filename"],
            "output_filename": cached.get("result_filename", ""),
            "message":         "批注版合同已就绪",
        }
    return {
        "status":   "uploaded",
        "filename": cached["filename"],
        "message":  "文件已上传，等待AI助手审核",
    }


@fastapi_app.get("/api/download/{session_id}")
async def api_download_result(session_id: str):
    """
    下载批注版合同文件。
    """
    if session_id not in _file_cache:
        return JSONResponse(
            {"status": "failed", "message": "会话不存在或已过期"},
            status_code=404,
        )

    cached = _file_cache[session_id]
    if not cached.get("result_ready"):
        return JSONResponse(
            {"status": "failed", "message": "批注尚未完成，请等待AI助手审核"},
            status_code=404,
        )

    result_path = cached.get("result_path", "")
    if not os.path.exists(result_path):
        return JSONResponse(
            {"status": "failed", "message": "结果文件已失效"},
            status_code=404,
        )

    return FileResponse(
        result_path,
        filename=cached.get("result_filename", "批注版.docx"),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ── MCP & 基础端点 ───────────────────────────────────────────

@fastapi_app.get("/health")
async def health():
    return {
        "status":       "ok",
        "service":      "electrical-contract-review",
        "tools":        ["upload_contract", "load_contract", "insert_comments",
                         "check_comment_tone", "get_legal_rules"],
        "cached_files": len(_file_cache),
    }


@fastapi_app.get("/sse")
async def handle_sse(request: Request):
    """SSE 端点 — bisheng / Clawith 连接此路径"""
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await mcp._mcp_server.run(
            read_stream,
            write_stream,
            mcp._mcp_server.create_initialization_options(),
        )


# 静态文件服务（放在所有路由之后，避免覆盖 API 路由）
fastapi_app.mount("/static", StaticFiles(directory=_FRONTEND_DIR), name="static")


if __name__ == "__main__":
    print(f"Starting on port {port}")
    print(f"Upload:  http://0.0.0.0:{port}/")
    print(f"Health:  http://0.0.0.0:{port}/health")
    print(f"SSE:     http://0.0.0.0:{port}/sse")
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
