@echo off
cd /d C:\trading
set SCREENER_VISION_ENABLED=true
python scripts/run_screener.py >> data\logs\screener.log 2>&1
