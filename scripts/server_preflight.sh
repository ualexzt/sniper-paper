#!/bin/sh
set -eu

docker --version
docker compose version
df -h / /home/ubuntu
curl -fsS --max-time 10 'https://api.bybit.com/v5/market/time'
