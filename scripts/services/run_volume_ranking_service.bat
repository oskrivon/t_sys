@echo off
cd /d C:\trading
python scripts/run_volume_ranking.py >> data\logs\volume_ranking.log 2>&1
