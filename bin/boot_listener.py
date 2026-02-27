import os, sys, runpy
# Force argv exactly how listener expects:
sys.argv = ["listener.py", "--serve"]
# Run the real file in THIS interpreter (no child process):
runpy.run_path(r"C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader\listener.py",
               run_name="__main__")