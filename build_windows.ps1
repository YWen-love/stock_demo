$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "[1/3] 安装构建依赖..."
python -m pip install -r requirements.txt

Write-Host "[2/3] 构建 Windows 应用目录..."
python -m PyInstaller --clean --noconfirm stock_inventory.spec

if (Get-Command iscc -ErrorAction SilentlyContinue) {
    Write-Host "[3/3] 生成安装程序..."
    iscc installer.iss
    Write-Host "安装程序已生成到 installer_output\StockInventorySetup.exe"
} else {
    Write-Warning "未找到 Inno Setup 的 iscc.exe，已完成 dist\StockInventoryApp 文件夹构建。"
    Write-Warning "安装 Inno Setup 后重新运行本脚本即可生成 exe 安装包。"
}