@echo off
cd /d C:\trading
python scripts/paper_trading.py check >> data\logs\paper_check.log 2>&1
