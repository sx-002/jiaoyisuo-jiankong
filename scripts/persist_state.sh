#!/usr/bin/env bash
# 把本轮去重状态与报告提交到独立的 state 分支。
# 不用 actions/cache：缓存被逐出会导致每轮重发。
set -euo pipefail

STATE_BRANCH="${STATE_BRANCH:-state}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [ ! -f state/state.json ]; then
  echo "未找到 state/state.json，跳过状态写入。"
  exit 0
fi
cp state/state.json "$TMP/state.json"
if [ -f report.json ]; then
  cp report.json "$TMP/last-report.json"
else
  printf '{}\n' > "$TMP/last-report.json"
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git fetch origin "$STATE_BRANCH"
git checkout -B "$STATE_BRANCH" "origin/$STATE_BRANCH"
mkdir -p state
cp "$TMP/state.json" state/state.json
cp "$TMP/last-report.json" state/last-report.json
git add -f state/state.json state/last-report.json
if git diff --cached --quiet; then
  echo "状态无变化，无需提交。"
  exit 0
fi
git commit -m "chore: 更新去重状态与运行报告 $STAMP"
git push origin "$STATE_BRANCH"
echo "状态已提交到 $STATE_BRANCH 分支。"
