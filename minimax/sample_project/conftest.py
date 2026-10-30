import os
import sys

# 让测试可以直接 import src/pricing.py
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
