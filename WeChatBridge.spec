# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 只用 QtCore/QtGui/QtWidgets。PySide6 装的时候把 WebEngine、3D、多媒体
    # 全装进来了，不排掉会让包大一圈，而且这些东西一进来就容易崩。
    excludes=[
        'tkinter',
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel',
        'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQml',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput',
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets', 'PySide6.QtCharts',
        'PySide6.QtDataVisualization', 'PySide6.QtNetworkAuth',
        'PySide6.QtPositioning', 'PySide6.QtSensors', 'PySide6.QtSerialPort',
        'PySide6.QtTextToSpeech', 'PySide6.QtWebSockets',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WeChatBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # Qt 的 DLL 被 UPX 压过之后经常加载失败，别压
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,   # Qt 的 DLL 被 UPX 压过之后经常加载失败，别压
    upx_exclude=[],
    name='WeChatBridge',
)
