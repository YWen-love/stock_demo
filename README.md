# 智能库存分析助手

这是一个 Windows 本地库存分析工具：上传库存 Excel 后，应用会结合本地库存规则进行检索，生成库存状态、补货建议和 Markdown 报告。配置火山方舟 API 后可以使用 AI Agent 分析；没有 API 配置时仍会使用本地规则兜底计算。

## 普通开发运行

要求：Windows、Python 3.10 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，填写自己的火山方舟配置：

```dotenv
ARK_API_KEY=你的API密钥
ARK_ENDPOINT_ID=你的模型接入点ID
```

启动：

```powershell
python desktop_app.py
```

也可以双击 `launch_stock_app.bat`。浏览器会自动打开 `http://127.0.0.1:7860`。

## 如何打包成安装程序

打包必须在 Windows 上完成，建议使用与目标机器相同或更低版本的 Windows 环境。

1. 安装 [Inno Setup](https://jrsoftware.org/isinfo.php)，安装时勾选将 `iscc.exe` 加入 PATH。
2. 在项目目录打开 PowerShell。
3. 运行：

```powershell
.\build_windows.ps1
```

脚本会安装构建依赖，并执行 PyInstaller。成功后：

- 可直接运行目录：`dist\StockInventoryApp\StockInventoryApp.exe`
- 安装包：`installer_output\StockInventorySetup.exe`

如果未安装 Inno Setup，脚本仍会生成 `dist\StockInventoryApp`，但不会生成安装包。

## 给最终用户的使用说明

1. 双击 `StockInventorySetup.exe` 安装。
2. 安装后，在程序安装目录找到 `.env.example`，复制一份并重命名为 `.env`。
3. 用文本编辑器填写 `ARK_API_KEY` 和 `ARK_ENDPOINT_ID`。不填写也可以使用本地兜底模式。
4. 从桌面快捷方式启动程序，浏览器会自动打开操作页面。
5. 上传包含以下列的 `.xlsx` 文件：`商品 id`、`商品名称`、`当前库存`、`近 7 天销量`。
6. 分析完成后，可在页面中下载 Markdown 报告；报告也会保存为安装目录下的 `stock_report.md`。

## 发布注意事项

- 不要把真实 `.env`、API 密钥或 `local_demo.log` 放进安装包或提交到代码仓库。
- 安装包已经包含 `stock_rag/rag_docs` 和 `stock_rag/vector_db`，首次运行不需要重新构建向量库。
- 用户需要能写入安装目录，因为报告和日志默认保存在该目录。
- 如果企业电脑限制写入 `AppData` 或阻止本地回环地址 `127.0.0.1`，需要由管理员调整策略。

## 测试

```powershell
python -m pytest -q
```

当前测试覆盖 Excel 输入校验、RAG 向量库缺失兜底、工具调用异常和本地报告计算。