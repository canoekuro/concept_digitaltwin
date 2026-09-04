---
name: git-push
description: "Safely stage, commit, and push project changes to GitHub using SSH authentication to avoid HTTPS password prompts."
---

# Git Push Skill (Gitコミット＆プッシュ)

## Overview
このスキルは、プロジェクトの変更をローカルリポジトリにコミットし、リモートのGitHubリポジトリへプッシュするための一連の手順を自動化します。
非対話型環境（AIエージェントの実行環境）でのプッシュ時に、HTTPSプロトコルでのパスワード・トークン認証が失敗（ブロック）する問題を回避するため、SSHプロトコルを使用します。

## Prerequisites
- ユーザー環境にGitHubと通信可能なSSH鍵（例: `~/.ssh/id_rsa`）が設定されていること。

## Instructions
ユーザーからコミットおよびプッシュの依頼があった場合、エージェントは以下の手順を実行してください。

1. **状態確認**: `git status` で変更内容を確認する。
2. **ステージング**: ユーザーの指示に従い `git add <files>` または `git add .` を実行する。
3. **コミット**: `.agents/rules/git-commit-rules.md` に従い、Conventional Commits形式の適切なメッセージを生成して `git commit -m "..."` を実行する。
4. **プッシュ**: 付属のスクリプト `./scripts/safe-push.sh` を使用してプッシュを実行する。このスクリプトは自動的にリモートURLをSSH形式に変換してからプッシュを行うため、認証エラーを回避できる。

## Scripts
* **`scripts/safe-push.sh`**: 現在のリモートURLを確認し、HTTPS形式（`https://github.com/...`）であればSSH形式（`git@github.com:...`）に変換した上で、`git push -u origin <現在のブランチ>` を実行するシェルスクリプト。`main` にいる場合は直接 push せずエラーで停止する（`.agents/rules/git-commit-rules.md` および共通 git ルール準拠）。
