"""舰长礼物小助手 —— 程序入口
  双击运行（或 python main.py）：打开图形界面
  main.py --bot                 ：只运行机器人（图形界面会自动用这个方式在后台启动机器人）
"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--bot" in sys.argv:
        from app.botmain import run_bot
        run_bot()
    else:
        from app.gui import run_gui
        run_gui()
