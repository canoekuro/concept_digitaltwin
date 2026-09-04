#!/usr/bin/env bash
# PostToolUse(Write) フック本体。
#
# Claude が「確認してほしい成果物」（コードレビュー・プラン等）を
# self-contained HTML として tasks/_review/ 配下に書き出したとき、その .html を
# OS の既定ブラウザで自動的に開く（記事 "Claude Code のレビューを HTML で開く" の
# deterministic 化に相当）。
#
# 入力: PostToolUse のペイロード JSON を標準入力で受け取る。
#   - 書き込み先パスは .tool_input.file_path（一部バージョンは .tool_response.filePath）。
# 動作条件: パスが */tasks/_review/*.html のときだけ開く。それ以外は静かに no-op。
# 環境: GUI ブラウザが無い環境（CI / リモートコンテナ等）では opener が見つからず
#   no-op する。フックは常に exit 0 で、Claude の処理をブロックしない。

payload="$(cat)"
file="$(printf '%s' "$payload" | jq -r '.tool_input.file_path // .tool_response.filePath // empty' 2>/dev/null)"

[ -n "$file" ] || exit 0
case "$file" in
  */tasks/_review/*.html) ;;   # 対象のみ
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

# クロスプラットフォームに、バックグラウンドで開く（ブロック・エラーを伝播させない）。
opener=""
for c in open xdg-open wslview; do
  if command -v "$c" >/dev/null 2>&1; then opener="$c"; break; fi
done
if [ -n "$opener" ]; then
  "$opener" "$file" >/dev/null 2>&1 &
elif command -v cmd.exe >/dev/null 2>&1; then        # Git Bash / WSL 経由の Windows
  cmd.exe /c start "" "$file" >/dev/null 2>&1 &
  opener="cmd.exe"
fi

if [ -n "$opener" ]; then
  # ブラウザを開いたことだけユーザーに通知（成果物パスを表示）。
  jq -cn --arg f "$file" \
    '{systemMessage: ("🔎 HTML成果物をブラウザで開きました: " + $f), suppressOutput: true}'
fi
exit 0
