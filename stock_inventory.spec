from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


project_root = Path(SPECPATH)
datas = [
    (str(project_root / "stock_rag" / "rag_docs"), "stock_rag/rag_docs"),
    (str(project_root / "stock_rag" / "vector_db"), "stock_rag/vector_db"),
]
binaries = []
hiddenimports = [
    "volcenginesdkarkruntime",
    "volcenginesdkarkruntime.exceptions",
]
datas += collect_data_files("gradio")


a = Analysis(
    [str(project_root / "desktop_app.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="StockInventoryApp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)