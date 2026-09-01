<!-- project_path: /synthetic/tradingbot | git_remote: none -->

> 前言說明，此刻沒有作用中的條目，不是任何條目的 body

- [ ] [P0] 尚無 heading 時用標題關鍵字判斷 section
  > 💡  這筆條目出現在任何 heading 之前，section 應由標題殘留的 [P0] 推導為 urgent
  > 🏷️  fallback, no-heading

- [ ] [P2] 尚無 heading 時用標題關鍵字判斷 section 應為 later
  > 💡  這筆條目出現在任何 heading 之前，section 應由標題殘留的 [P2] 推導為 later
  > 🏷️  fallback, no-heading

- [ ] [Cast 拍板] 尚無 heading 時用標題關鍵字判斷 section 應為 decision
  > 💡  這筆條目出現在任何 heading 之前，section 應由標題殘留的 [Cast 拍板] 推導為 decision
  > 🏷️  fallback, no-heading

- [ ] 尚無 heading 且標題無特殊標記時 section 應為 normal
  > 💡  這筆條目出現在任何 heading 之前，標題沒有 [P0]/[Cast 拍板]/[P2] 任何標記，應落回預設的 normal
  > 🏷️  fallback, no-heading

## 🔴 立即處理（P0 / 資金安全）（1）

- [ ] [2026-08-01] 風控閾值檢查缺漏
  > 🔗  ⚓ RiskEngine(42)
  > 🏷️  risk, urgent
  > ⚠️  此條目在 heading 明確為 urgent 的段落下

## 🟠 待拍板決策（1）

- [ ] [2026-08-02] [Cast 拍板] 是否改用新版下單 API
  > 💡  需要決策層拍板
  > ⚖️  影響下單延遲與相容性

## 🟡 觀察中（1）

- [ ] [2026-08-03] 一般觀察項目
  > 🔍  持續觀察中
  > 一般說明文字沒有 marker
  > ✅  已確認不影響現行流程

<!-- ⚓ group-anchor-old -->
<!-- ⚓ group-anchor-new -->

- [ ] [2026-08-04] 群組錨點應套用最後一個註解
  > 💡  驗證 group_marker 取最後一個 ⚓ 註解，不是累積或取第一個

## ⚪ 觀察 / 技術債（不急）（1）

- [ ] [2026-08-05] [P0] 已被降級為 later 的事項
  > 💡  heading 說 later 就是 later，優先於標題殘留的 [P0]

## 🟢 之後再看（1）

- [ ] [2026-08-06] 另一個 later 符號
  > 💡  🟢 與 ⚪ 是不同符號但都應塌成同一個 later section
