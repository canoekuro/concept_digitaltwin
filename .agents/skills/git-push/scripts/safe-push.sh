#!/bin/bash
# safe-push.sh
# Safely push to GitHub by automatically converting HTTPS remotes to SSH to avoid prompt blocks.

echo "=== Starting Safe Git Push ==="

# リモート 'origin' のURLを取得
REMOTE_URL=$(git remote get-url origin 2>/dev/null)

if [ -z "$REMOTE_URL" ]; then
    echo "Error: Remote 'origin' is not set."
    echo "Please set it using: git remote add origin git@github.com:<username>/<repo>.git"
    exit 1
fi

# HTTPSの場合はSSHプロトコルに変換
if [[ "$REMOTE_URL" == https://github.com/* ]]; then
    echo "Converting HTTPS remote to SSH remote to avoid password prompts..."
    # 例: https://github.com/canoekuro/concept_digitaltwin.git -> canoekuro/concept_digitaltwin.git
    REPO_PATH=$(echo "$REMOTE_URL" | sed 's|https://github.com/||')
    NEW_SSH_URL="git@github.com:${REPO_PATH}"
    
    git remote set-url origin "$NEW_SSH_URL"
    echo "Remote 'origin' updated to: $NEW_SSH_URL"
fi

echo "Current Remote URL: $(git remote get-url origin)"
# main への直接 push は禁止(.agents/rules/git-commit-rules.md / 共通 git ルール)。
# 現在のブランチをそのまま push する。
BRANCH=$(git rev-parse --abbrev-ref HEAD)

if [ "$BRANCH" = "main" ]; then
    echo "Error: main への直接 push は禁止です。作業ブランチを切ってから実行してください。"
    exit 1
fi

echo "Pushing to origin ${BRANCH}..."

git push -u origin "$BRANCH"

if [ $? -eq 0 ]; then
    echo "=== Push Successful! ==="
else
    echo "=== Push Failed. ==="
    echo "Please check your SSH keys and ensure they are added to your GitHub account."
    exit 1
fi
