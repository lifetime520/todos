"""回合結束守衛的判斷邏輯（hooks/stop-guards.sh 只負責讀輸入、呼叫這裡、印 JSON）。

來源：2026-11-17 #11 空轉 19.5 小時檢討（D-047）。
子程序盤點：Bash 工具留下的等待迴圈會洩漏數天（實測 12 個、其中一個燒掉 32 小時 CPU）。
   同一個 PID 只提醒一次。

原本另有「未來式檢查」（結尾有承諾卻沒提問就擋），2026-11-18 Cast 裁示全域撤掉：
它把窗口 #13 的回合結尾逼成向 Cast 提問（11-18 達 73%），#4 因此空等約 5 小時。
"""
import json
import os

STALE_AGE_S = 2 * 3600
BASH_TOOL_MARK = 'shell-snapshots'


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
