@echo off
cd /d C:\trading
python scripts/daily_report.py --telegram >> data\logs\daily_report.log 2>&1
