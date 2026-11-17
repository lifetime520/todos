"""回合結束守衛的判斷邏輯（hooks/stop-guards.sh 只負責讀輸入、呼叫這裡、印 JSON）。

來源：2026-11-17 #11 空轉 19.5 小時檢討，Cast 核准的改進 1、3：
1. 未來式檢查：回合結尾寫「我會／下一步是／等你確認後再…」卻沒提問，等於兩邊互等的死結。
   擋一次（stop_hook_active 時放行），逼這一回合把事做掉，或改用 AskUserQuestion 提問。
3. 子程序盤點：Bash 工具留下的等待迴圈會洩漏數天（實測 12 個、其中一個燒掉 32 小時 CPU）。
   同一個 PID 只提醒一次。
"""
import json
import os
import re

TAIL_CHARS = 600
FUTURE_RE = re.compile(
    r'我會(?![議員])|我將|我再來|接下來(我)?(會|要|先|就)|下一步(是|就|我|[：:](?!\s*[無没沒]))|稍後|待會|'
    r'之後再(?![有發出])')
QUESTION_RE = re.compile(r'？|\?\s*$', re.M)
# 等使用者卻沒提問：一律擋（11-17 的主因）
USER_WAIT_RE = re.compile(r'(?<![不無免])(?<!不需要)(?<!不用)(?<!不必)等(你|Cast|cast)(的)?(確認|回覆|決定|裁示|拍板|核准|同意)')
SENTENCE_SPLIT_RE = re.compile(r'[。！；;\n]')
# 條件式未來（等外部事件，會有通知喚醒）：放行
EXTERNAL_WAIT_RE = re.compile(
    r'(跑完|做完|改完|寫完|完成|結束|通過|PASS|FAIL|有結果|結果出來|回報|收到通知|部署|合入|就緒|醒來)'
    r'\s*(後|之後|時|以後)|等\s*(#\d|tester|Task|task|它|他們|agent|測試|佇列|fast|部署|#)|完成後我會收到通知|'
    r'(?<![稍之以待然隨最])後[，,]?\s*(我|就|再|接著|會)|[^\s，,。、稍待然隨以]之後[，,]?\s*(我|就|接著|會)|'
    r'完[，,]?\s*(我|就|再|接著|會)')
QUOTED_RE = re.compile(r'「[^「」]*」|`[^`\n]*`')
STALE_AGE_S = 2 * 3600
BASH_TOOL_MARK = 'shell-snapshots'
_TAIL_BYTES = 4 * 1024 * 1024


def future_guard(text, used_ask, stop_hook_active):
    """結尾有未來式承諾、又沒提問時回傳擋下理由，否則 None。"""
    if stop_hook_active or used_ask or not text:
        return None
    # 引述（「…」、`…`）裡的字不是自己的承諾，比對前先遮掉（2026-11-18 引述句型被誤擋）
    tail = QUOTED_RE.sub(lambda m: '□' * len(m.group()), text[-TAIL_CHARS:])
    if QUESTION_RE.search(tail):
        return None
    m = USER_WAIT_RE.search(tail)
    if not m:
        m = next((f for f in FUTURE_RE.finditer(tail)
                  if not EXTERNAL_WAIT_RE.search(_sentence(tail, f.start()))), None)
        if not m:
            return None
    return (f'【回合結尾有未來式承諾：「{tail[max(0, m.start() - 10):m.end() + 20].strip()}」，但沒有提問】'
            '這樣結束會變成雙方互等（2026-11-17 #11 因此空轉 19.5 小時）。請擇一：'
            '(1) 這件事現在就能做 → 這一回合直接做掉；'
            '(2) 需要使用者決策 → 用 AskUserQuestion 附具體選項（窗口角色改純文字列選項並以問句結尾）；'
            '(3) 確實在等外部訊號（tester、背景工作、Monitor）→ 確認有東西會喚醒你，'
            '並在訊息中寫明等什麼、何時醒來，然後直接結束即可。')


def _sentence(text, pos):
    """pos 所在的那一句（以句號、驚嘆號、換行切）。條件句只在承諾同一句內才算數。"""
    start = max((m.end() for m in SENTENCE_SPLIT_RE.finditer(text, 0, pos)), default=0)
    end = SENTENCE_SPLIT_RE.search(text, pos)
    return text[start:end.start() if end else len(text)]


def turn_used_ask(transcript_path):
    """本回合（最後一則真人／系統文字輸入之後）是否呼叫過 AskUserQuestion。"""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, 'rb') as f:
            f.seek(max(0, size - _TAIL_BYTES))
            lines = f.read().split(b'\n')
    except (OSError, TypeError):
        return False
    for raw in reversed(lines):
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        msg = rec.get('message') if isinstance(rec, dict) else None
        if not isinstance(msg, dict):
            continue
        content = msg.get('content')
        if rec.get('type') == 'user':
            if isinstance(content, str) or (isinstance(content, list) and any(
                    isinstance(c, dict) and c.get('type') == 'text' for c in content)):
                return False
        elif rec.get('type') == 'assistant' and isinstance(content, list):
            if any(isinstance(c, dict) and c.get('type') == 'tool_use'
                   and c.get('name') == 'AskUserQuestion' for c in content):
                return True
    return False


def _stat(pid):
    """回傳 (comm, ppid, starttime_ticks)；讀不到回 None。"""
    try:
        with open(f'/proc/{pid}/stat') as f:
            s = f.read()
        comm = s[s.index('(') + 1:s.rindex(')')]
        rest = s[s.rindex(')') + 2:].split()
        return comm, int(rest[1]), int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def find_claude_pid(start_pid):
    """沿 ppid 往上找第一個 comm 以 claude 開頭的程序。"""
    pid = start_pid
    for _ in range(32):
        st = _stat(pid)
        if not st:
            return None
        if st[0].startswith('claude'):
            return pid
        if st[1] <= 1:
            return None
        pid = st[1]
    return None


def stale_children(parent_pid, min_age_s=STALE_AGE_S):
    """parent 底下由 Bash 工具起的 shell，存活超過門檻者：[(pid, 秒數, 指令摘要)]。"""
    try:
        with open('/proc/uptime') as f:
            uptime = float(f.read().split()[0])
    except OSError:
        return []
    hz = os.sysconf('SC_CLK_TCK')
    out = []
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        st = _stat(int(d))
        if not st or st[1] != parent_pid:
            continue
        age = uptime - st[2] / hz
        if age < min_age_s:
            continue
        try:
            with open(f'/proc/{d}/cmdline', 'rb') as f:
                cmd = f.read().replace(b'\0', b' ').decode('utf-8', 'replace')
        except OSError:
            continue
        if BASH_TOOL_MARK not in cmd:
            continue
        i = cmd.find("eval '")
        out.append((int(d), int(age), (cmd[i + 6:] if i >= 0 else cmd)[:100]))
    return sorted(out)


def process_guard(stale, reported, stop_hook_active):
    """回傳 (擋下理由或 None, 本次新提醒的 pid 集合)。"""
    if stop_hook_active:
        return None, set()
    new = [s for s in stale if s[0] not in reported]
    if not new:
        return None, set()
    rows = '\n'.join(f'  pid {p}（{a // 3600} 小時）：{c}' for p, a, c in new)
    return (f'【本 session 有 {len(new)} 個存活超過 {STALE_AGE_S // 3600} 小時的 Bash 子程序】\n{rows}\n'
            '多半是等不到終點的等待迴圈（pgrep -f 比對到自己、等的檔案不存在、`do :; done` 忙迴圈）。'
            '確認不再需要就用 TaskStop 或 `kill <pid>`（只 kill 上列 pid，先確認 ppid 是本 session）；'
            '仍需要就說明理由後直接結束。同一 pid 不會再提醒。'), {s[0] for s in new}


STATE_TTL_S = 7 * 86400


def _load_reported(path):
    try:
        data = json.loads(path.read_text())
        return {int(x) for x in data} if isinstance(data, list) else set()
    except (OSError, ValueError, TypeError):
        return set()


def _prune_state(state_dir, now):
    try:
        for f in state_dir.glob('*.json'):
            if now - f.stat().st_mtime > STATE_TTL_S:
                f.unlink()
    except OSError:
        pass


def run_hook(inp, claude_pid, state_dir):
    """Stop hook 主流程；回傳要印出的 dict 或 None。任何單一守衛出錯都只略過該守衛。"""
    import time
    if not isinstance(inp, dict):
        return None
    active = bool(inp.get('stop_hook_active'))
    sid = str(inp.get('session_id') or '')
    reasons = []
    try:
        text = inp.get('last_assistant_message')
        if isinstance(text, str):
            r = future_guard(text, turn_used_ask(inp.get('transcript_path')), active)
            if r:
                reasons.append(r)
    except Exception:
        pass
    try:
        if claude_pid and sid and not active and '/' not in sid:
            state = state_dir / f'{sid}.json'
            reported = _load_reported(state)
            if state.exists():
                state.touch()  # 長壽 session 不被 7 天清理誤刪
            r, new = process_guard(stale_children(claude_pid), reported, active)
            if r:
                state_dir.mkdir(parents=True, exist_ok=True)
                _prune_state(state_dir, time.time())
                state.write_text(json.dumps(sorted(reported | new)))
                reasons.append(r)
    except Exception:
        pass
    return {'decision': 'block', 'reason': '\n\n'.join(reasons)} if reasons else None
