"""打包成 Windows 程序（开发者用）：
    .venv\\Scripts\\python build_exe.py
产物在 dist\\舰长礼物小助手\\ ，整个文件夹压缩后即可发给别人，双击里面的 舰长礼物小助手.exe 运行。
"""
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
NAME = "舰长礼物小助手"


def make_icon(path: Path):
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, size - 8, size - 8), radius=56, fill="#fb7299")
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 150)
    d.text((size / 2, size / 2 + 4), "舰", font=font, fill="white", anchor="mm")
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main():
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    icon = build / "icon.ico"
    make_icon(icon)
    sep = ";" if sys.platform == "win32" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "main.py"),
        "--name", NAME, "--windowed", "--noconfirm", "--clean",
        "--icon", str(icon),
        "--distpath", str(ROOT / "dist"), "--workpath", str(build / "work"), "--specpath", str(build),
        "--add-data", f"{ROOT / 'app' / 'templates'}{sep}app/templates",
        # NoneBot 加载插件时要读取插件的 .py 源文件，必须原样带上
        "--add-data", f"{ROOT / 'app' / 'plugins' / 'guard_bot.py'}{sep}app/plugins",
        "--add-data", f"{ROOT / 'app' / 'plugins' / 'qq_official.py'}{sep}app/plugins",
        "--collect-data", "rapidocr",
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "websockets",
        "--collect-submodules", "nonebot",
        "--hidden-import", "app.plugins.guard_bot",
        "--hidden-import", "nonebot.adapters.onebot.v11",
        "--hidden-import", "app.plugins.qq_official",
        "--hidden-import", "app.joincode",
        "--hidden-import", "nonebot.adapters.qq",
        "--hidden-import", "nonebot.drivers.httpx",
        "--hidden-import", "nonebot.drivers.websockets",
        "--copy-metadata", "nonebot2",
        "--exclude-module", "tkinter",
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    out = ROOT / "dist" / NAME
    # 瘦身：去掉用不到的视频解码库、软件 OpenGL、非中文的 Qt 翻译
    internal = out / "_internal"
    for pat in ["cv2/opencv_videoio_ffmpeg*.dll", "PySide6/opengl32sw.dll"]:
        for f in internal.glob(pat):
            f.unlink()
    for f in (internal / "PySide6" / "translations").glob("*.qm"):
        if "zh_CN" not in f.name:
            f.unlink()
    shutil.copy(ROOT / "使用说明.md", out / "使用说明.md")
    # 打成压缩包，方便发给别人 / 上传到 GitHub Releases（不含 config.yaml 和 data）
    zip_base = ROOT / "dist" / "guard-gift-windows"     # GitHub 附件名不支持中文，用英文
    shutil.make_archive(str(zip_base), "zip", root_dir=ROOT / "dist", base_dir=NAME)
    print(f"\nDONE: {out}\nZIP:  {zip_base}.zip")


if __name__ == "__main__":
    main()
