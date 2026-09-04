# レビュー・成果物の HTML 出力ルール（常時ON / advisory）

参考: tatsuki「Claude Code のレビューを HTML で開くようにしたら、AI協業の歩留まりが
変わった話」。Markdown を流し見るより、self-contained HTML をブラウザで開くほうが
「読まれる・直される」歩留まりが上がる、という運用知見に基づく。

## 方針

「確認してほしい系の成果物」は、チャットへの要約に加えて **self-contained HTML**
（インライン CSS・JS なし・外部依存ゼロ）として書き出す。書き出すと
`PostToolUse(Write)` フック（`.claude/hooks/auto-open-review.sh`）が `.html` を
自動でブラウザ表示する（deterministic）。

- **対象**: コードレビュー（`/code-review` の findings）、プラン（plan モードの計画）、
  仕様ドラフト、調査レポート、設計検討メモ。
- **対象外**: 軽い口頭回答、短い確認、コードスニペット、進捗報告。これらは HTML 化しない
  （トークン・手間の無駄）。

## 置き場所・命名

- 出力先: 各リポジトリの `tasks/_review/`（`.gitignore` 済み。コミットしない）。
- ファイル名: `tasks/_review/<kind>-<YYYYMMDD-HHMMSS>.html`
  （`<kind>` 例: `review` / `plan` / `spec` / `report`）。
- フックは `*/tasks/_review/*.html` のみを開く。**必ずこのディレクトリに**書き出すこと。

## HTML テンプレート（骨子）

方向性は **ライト専用・ミニマル**（タイポグラフィ重視・余白多め・ハイラインの区切り）。
severity は濃い色ブロックではなく、**2px の左罫＋小さな色タグ＋ドット**で控えめに示す。
design tokens は `:root` に集約。ファイル名・行番号は `<code>`/`.loc` で `Cmd+F` 検索しやすく、
長い節は `<details>` で畳む。

```html
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code Review</title>
<style>
  :root{ --fg:#1a1a1a; --muted:#8a8f98; --line:#ececec; --bg:#fff; --code-bg:#f6f7f9;
         --accent:#2563eb; --critical:#dc2626; --warning:#d97706; --info:#16a34a; }
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,
       "Hiragino Sans","Noto Sans JP",sans-serif; color:var(--fg); background:var(--bg);
       max-width:720px; margin:0 auto; padding:4rem 1.5rem 6rem; line-height:1.75; font-size:17px;
       -webkit-font-smoothing:antialiased;}
  header{margin-bottom:1rem}
  h1{font-size:1.75rem;font-weight:650;letter-spacing:-.02em;margin:0 0 .4rem}
  .meta{color:var(--muted);font-size:.85rem;margin:0}
  .summary{display:flex;gap:1.25rem;flex-wrap:wrap;margin-top:1.25rem}
  .summary span{display:inline-flex;align-items:center;gap:.45rem;font-size:.85rem;color:var(--muted)}
  .dot{width:.5rem;height:.5rem;border-radius:50%}
  .dot.c{background:var(--critical)} .dot.w{background:var(--warning)} .dot.i{background:var(--info)}
  h2{font-size:.78rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
     margin:3rem 0 1.25rem;padding-bottom:.6rem;border-bottom:1px solid var(--line)}
  .finding{margin:0 0 2rem;padding-left:1rem;border-left:2px solid var(--line)}
  .finding.critical{border-color:var(--critical)}
  .finding.warning{border-color:var(--warning)}
  .finding.info{border-color:var(--info)}
  .tag{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
  .tag.critical{color:var(--critical)} .tag.warning{color:var(--warning)} .tag.info{color:var(--info)}
  .loc{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem}
  .finding p{margin:.5rem 0 0}
  code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code-bg);
       padding:.12em .4em;border-radius:4px;font-size:.86em}
  pre{background:var(--code-bg);padding:1rem 1.2rem;border-radius:8px;overflow-x:auto;
      font-size:.85rem;line-height:1.6}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
  table{border-collapse:collapse;width:100%;margin:1rem 0;font-size:.9rem}
  th,td{text-align:left;padding:.55rem .2rem;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.05em}
  details{margin:.5rem 0}
  summary{cursor:pointer;color:var(--muted);font-size:.85rem;list-style:none}
  summary::-webkit-details-marker{display:none}
  summary::before{content:"\203A";display:inline-block;margin-right:.5rem;transition:transform .15s}
  details[open] summary::before{transform:rotate(90deg)}
</style>
</head>
<body>
<header>
  <h1>Code Review</h1>
  <p class="meta">PR #57 · SegReg バリアント · 2026-06-22 · 19 files (+858 / −61)</p>
  <div class="summary">
    <span><i class="dot c"></i>Critical 0</span>
    <span><i class="dot w"></i>Warning 1</span>
    <span><i class="dot i"></i>Info 5</span>
  </div>
</header>

<h2>Warning</h2>
<div class="finding warning">
  <span class="tag warning">Warning</span> · <span class="loc">registry.py:71</span>
  <p>所見の本文。<code>file:line</code> は <code>&lt;code&gt;</code> で検索しやすく。</p>
</div>

<h2>Info</h2>
<div class="finding info">
  <span class="tag info">Info</span>
  <p>検証してクリーンだった点など。</p>
</div>
</body>
</html>
```

- プランの場合は同じ CSS で、`<h2>` を「背景 / 変更内容 / テスト / 検証 / リスク」等の節に
  置き換える（`.finding` の severity クラスは省略可）。before/after は下ハイラインのみの
  `<table>` で対比すると意図が伝わりやすい。
- 出力は外部 CDN・JS を使わない（1ファイルで完結・共有可能にする）。
