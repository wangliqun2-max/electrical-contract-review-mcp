# 电气合同审核 MCP Server

电气制造领域合同审核工具，将审核意见以批注形式嵌入 Word 文档原文。

## 工具列表

| 工具 | 说明 |
|---|---|
| `review_contract` | 主工具：完整审核流程，输入合同文件（base64）+ 批注 JSON，输出批注版 docx |
| `check_comment_tone` | 批注措辞检查，检测感叹号、绝对化、夸张表述等 |
| `get_legal_rules` | 查询内置法务规则（付款比例、违约金标准、质保期等） |

## 部署

### Railway（推荐）

1. Fork 本仓库
2. 在 Railway 创建新项目 → 连接 GitHub 仓库
3. Railway 自动识别 `nixpacks.toml`，安装 LibreOffice + Python 依赖
4. 部署完成后获取公网 URL

### 本地运行

```bash
pip install -r requirements.txt
python mcp_tools.py
# MCP Server: http://localhost:8001/mcp
# Health:     http://localhost:8000/health
```

## 接入 Clawith

1. Clawith 管理界面 → 工具 → 新增 MCP 工具
2. 类型选择：**HTTP / SSE**
3. URL 填入：`https://<your-railway-domain>/mcp`
4. 保存后，Agent 即可使用三个工具

## review_contract 调用示例

```json
{
  "contract_base64": "<base64编码的docx内容>",
  "filename": "采购合同.docx",
  "comments_json": "[{\"target\": \"电汇、银行承兑汇票\", \"text\": \"根据乙方法务审核规则...\"}]",
  "author": "乙方法律顾问"
}
```

## 支持的合同格式

- `.docx`（直接处理）
- `.doc`（自动转换，需要 LibreOffice）
