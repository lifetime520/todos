"""窗口流程守衛的判斷邏輯（hooks 只負責讀輸入、呼叫這裡、印 JSON）。

來源：2026-11-16 #13 績效診斷（報告 ~/.superpowers/diagnosing-superpowers/
b78b35ae-0016-4f7c-9ac4-430642a4bde4/report.md），Cast 核准的改進 1～3：
1. 窗口禁用阻塞提問：AskUserQuestion 會讓窗口停擺，期間 peer 只能排隊
   （實測 17 次合計 3302 分鐘、26 則訊息排隊）。
2. （已撤）context 輪替提醒：2026-11-18 Cast 裁示撤掉，窗口一直請 Cast /clear。
3. 歸屬／記帳／資金費題先讀既有文件，審查 prompt 不得鎖定前提
   （S9b 被否決 5 輪；共審 prompt 寫「不要建議推翻」使審查者無從挑戰陷阱公式）。
"""
import os
import re

DESIGN_KEYWORDS = re.compile(
    r'歸屬|記帳|帳本|對帳|資金費|淨倉|拆帳|attribution|funding', re.I)  # 不含 ledger：castpower 的 run 紀錄也叫 ledger
DESIGN_DOCS = ('docs/knowledge/position-attribution-model.md', 'docs/audit/PITFALLS.md')
LOCK_PREMISE = re.compile(r'不要(建議)?推翻|不得(挑戰|推翻|質疑)|不要(挑戰|質疑)|勿(推翻|質疑)|前提不可')
# 只看 Agent 的 description／subagent_type 判定「審查型」，不掃 prompt 全文：
# prompt 常含 docs/audit/ 路徑、code-reviewer、驗收條件等字樣，掃全文會誤擋一般派工。
REVIEW_HINT = re.compile(r'審|review|audit|驗收|魔鬼|auditor', re.I)


def ask_guard(role):
    """窗口提問一律擋下，改純文字列選項；回傳 PreToolUse 的 hookSpecificOutput 片段或 None。"""
    if role != 'window':
        return None
    return {
        'permissionDecision': 'deny',
        'permissionDecisionReason': (
            '【窗口禁用阻塞提問】AskUserQuestion 會讓窗口停擺、peer 全部排隊'
            '（#13 實測 17 次合計 3302 分鐘）。'
            '先判斷要不要問（協定第 12 條，2026-11-18）：你已有明確建議時，直接照建議執行，'
            '用 todo_cli decision add 記錄並註明「窗口依建議裁示，Cast 可推翻」，在回報裡告知 Cast 即可。'
            '只有拿不定主意，或動到真實資金／下單且難以挽回時，才用純文字列出 2～3 個選項與建議問 Cast，'
            '然後結束回合，等候期間照常處理 peer 訊息。'),
    }


def design_docs_guard(tool_name, text, project_root, review_label=''):
    """歸屬類關鍵字 → 提示先讀既有文件；審查型 Agent prompt 鎖定前提 → 擋下。"""
    if not text or not DESIGN_KEYWORDS.search(text):
        return None
    docs = [d for d in DESIGN_DOCS if os.path.isfile(os.path.join(project_root or '', d))]
    if not docs:
        return None
    if (tool_name == 'Agent' and LOCK_PREMISE.search(text)
            and REVIEW_HINT.search(review_label or '')):
        return {
            'permissionDecision': 'deny',
            'permissionDecisionReason': (
                '【審查不得鎖定前提】審查 prompt 寫了「不要推翻／不得質疑」類字眼。'
                'S9b 共審曾把文件明列的陷阱公式鎖成「不要建議推翻」，三位審查者無從挑戰，'
                '設計因此被否決 5 輪。拿掉鎖定字眼，改成「以下前提若與既有文件衝突請指出」再送。'),
        }
    return {
        'additionalContext': (
            '【歸屬／記帳題：先讀既有教訓】送出前確認你本人已讀：'
            + '、'.join(docs)
            + '，並跑 todo_cli decision list <主題> 查現行裁決。'
            'S9b 第 4 次被糾正後才第一次讀這份文件。'
            '派給審查者時，把文件路徑列入必讀，不要只當背景。'),
    }


# ---------------------------------------------------------------- 收件匣分流（B 案，D-048）

PEER_TAGS = ('問題', '阻塞', '交付', '轉達', '彙整', '緊急')
# 標籤可帶編號或前後綴（【交付 T-1021】、【T-1021 交付】、[交付]），只要出現在開頭 30 字內
PEER_TAG_RE = re.compile(r'[【\[][^】\]]{0,20}?(' + '|'.join(PEER_TAGS) + r')[^】\]]{0,20}[】\]]')
PEER_TAG_WINDOW = 30
# 純確認：拿掉開頭標籤、確認詞、標點與表情後幾乎不剩字
ACK_WORDS_RE = re.compile(r'收到|了解|知悉|好的|好|謝謝|感謝|\bok\b|👍|🙏', re.I)
ACK_LEFTOVER_MAX = 0


def _is_pure_ack(msg):
    body = re.sub(r'^\s*[【\[][^】\]]*[】\]]', '', msg or '')
    if '？' in body or '?' in body:
        return False
    rest = re.sub(r'[\W_～~]+', '', ACK_WORDS_RE.sub('', body))
    return body.strip() != '' and len(rest) <= ACK_LEFTOVER_MAX and bool(ACK_WORDS_RE.search(body))


def peer_message_guard(sender, target, window, message):
    """sender／target／window 皆為 session 短名（解析不到為 None）。回傳 PreToolUse 片段或 None。"""
    if not window:
        return None
    if sender == window:
        if target and target != window and _is_pure_ack(message):
            return {'permissionDecision': 'deny', 'permissionDecisionReason': (
                '【窗口不回純確認】只回「收到／了解」會再喚醒對方一次、整份 context 重讀'
                '（D-048）。沒有裁示或指示就不用回；'
                '要回就附上實質內容。')}
        return None
    if target != window or PEER_TAG_RE.search((message or '')[:PEER_TAG_WINDOW]):
        return None
    return {'permissionDecision': 'deny', 'permissionDecisionReason': (
        f'【送窗口的訊息要帶標籤】需要 {window} 回應或處理的，開頭加上'
        f'{"／".join("【" + t + "】" for t in PEER_TAGS)}其中之一再送。'
        '不需要回應的（進度、已照辦、已推送待 tester、PASS 通知）改寫收件匣，窗口下次醒來會一次看到：'
        'python3 ~/.claude/skills/todo-audit/scripts/todo_cli.py inbox post "<內容>" --todo T-NNN'
        '（D-048：#13 實測讀完沒回的喚醒占 18% 成本）。')}


def inbox_inject_text(items):
    if not items:
        return None
    lines = [f'【收件匣 {len(items)} 則（peer 不需回應的回報，送出後標為已讀）】']
    for i in items:
        tid = f'（{i["todo_ids"]}）' if i.get('todo_ids') else ''
        lines.append(f'- {i["created_at"][11:16]} {i["from_name"] or "?"}{tid}：{i["body"]}')
    lines.append('有需要處理的照常處理；純知會不必回覆（窗口不回純確認）。')
    return '\n'.join(lines)
