"""REQ-5：repo 根目錄需要 `.claude/todo-audit.json`，消除 `todo_audit.py` 的
`⚠️  WEAK_AUDIT` 零命中降級。

背景（`.castpower/todo-audit-testability/requirements.md` REQ-5 / analysis.md
「REQ-5：`.claude/todo-audit.json`」節）：本 repo（純 Python/Bash 工具倉庫）
沒有任何層級的 `todo-audit.json`，`collect_source_files()`
（`todo_audit.py:274-305`）在 `config is None` 時落到內建 `SEARCH_DIRS`
（BTSE Gradle 路徑：`agent/src/main`/`core/src/main`/... ），本 repo 完全沒有
這些目錄，`prod_roots` 必然零命中，「掃描 N 檔」固定卡在 N=1。

這份測試在 `.claude/todo-audit.json` 還不存在的當下必須是 RED——這是任務
本身要求的證據，不是測試寫錯。

介面契約（沿用 `test_config.py` 的呼叫慣例）：
    config, provenance, warnings = todo_config.load_config(repo_root, defaults, home=...)
    files, by_name = todo_audit.collect_source_files(repo_root, config=config)

**刻意不執行 `todo_cli.py audit`**——那會寫入正式待辦 DB
（`~/.claude/todos/.audit/*.sqlite`），本 task 全域禁止。REQ-5 的驗證改用
純記憶體呼叫 `collect_source_files(repo_root, config=<載入的設定>)`。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import todo_audit  # noqa: E402
import todo_config  # noqa: E402

# 從測試檔位置往上推：tests/ -> todo-audit/ -> skills/ -> repo root。
# 不硬編絕對路徑，也不用 os.getcwd()（跑測試時 cwd 是 skills/todo-audit，
# 不是 repo root）。
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / '.claude' / 'todo-audit.json'


class RepoAuditConfigFixture(unittest.TestCase):
    def _require_config_path(self):
        """存在性檢查獨立成一個可重用斷言，讓每個測試方法失敗時的訊息
        都能直接點名「設定檔還不存在」，而不是被後續的 open()/json.loads()
        丟出的 FileNotFoundError 蓋過去。"""
        self.assertTrue(
            CONFIG_PATH.exists(),
            f'{CONFIG_PATH} 不存在 —— REQ-5 要求 repo 根目錄要有這份設定檔，'
            'collect_source_files() 才不會落到內建 SEARCH_DIRS（BTSE Gradle '
            '路徑）而在本 repo 零命中降級為 WEAK_AUDIT',
        )

    def _load_config_json(self):
        self._require_config_path()
        raw = CONFIG_PATH.read_text(encoding='utf-8')
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self.fail(f'{CONFIG_PATH} 不是合法 JSON：{e}')
        self.assertIsInstance(
            data, dict, f'{CONFIG_PATH} 內容須是 JSON object，實際是 {type(data).__name__}')
        return data


class TestRepoAuditConfigFile(RepoAuditConfigFixture):
    """REQ-5：`.claude/todo-audit.json` 存在、為合法 JSON，且內容涵蓋本 repo
    實際使用的目錄與副檔名。"""

    # REQ-5
    def test_config_file_exists(self):
        self._require_config_path()

    # REQ-5
    def test_config_file_is_valid_json(self):
        # _load_config_json() 內部已經做完「存在 + 合法 JSON + 是 object」
        # 三件事的斷言；這裡呼叫一次即完成本測試要驗的行為。
        self._load_config_json()

    # REQ-5
    def test_search_dirs_excludes_tests_directory(self):
        # todo_audit.py:289-292 的既有註解明講：把測試碼算進生產碼符號掃描
        # 範圍，會讓死碼偵測失能——已刪除的產品碼因為符號仍出現在測試裡，
        # 會被誤判成「還活著」。search_dirs 裡的每一個目錄都不能指向測試目錄。
        data = self._load_config_json()
        search_dirs = data.get('search_dirs', [])
        for d in search_dirs:
            parts = Path(d).parts
            self.assertNotIn(
                'tests', parts,
                f'search_dirs 內的 {d!r} 指向測試目錄，會讓死碼偵測失能',
            )

    # REQ-5
    def test_scan_exts_covers_python_shell_and_markdown(self):
        # Stage 2 實測：本 repo 實際分布在 skills/（.py、SKILL.md）、
        # hooks/（.sh）、docs/（.md）。三種副檔名缺一個，對應目錄下的檔案
        # 就掃不到。
        data = self._load_config_json()
        scan_exts = set(data.get('scan_exts', []))
        required = {'.py', '.sh', '.md'}
        missing = required - scan_exts
        self.assertFalse(
            missing,
            f'scan_exts 缺少 {sorted(missing)} —— 本 repo 的程式與文件實際'
            '使用這些副檔名，少了任一個都會讓對應目錄下的檔案掃不到',
        )


class TestCollectSourceFilesWithRepoConfig(RepoAuditConfigFixture):
    """REQ-5：套用 repo 設定檔後，`collect_source_files()` 的命中檔數要 > 1
    （目前落到內建 SEARCH_DIRS 時固定是 1）。"""

    # REQ-5
    def test_collect_source_files_hits_more_than_one_file(self):
        self._require_config_path()

        # home 指向一個保證不含 todo-audit.json 的空目錄，避免這台機器上
        # 若真的存在 ~/.claude/todo-audit.json（user-global 層）干擾判定
        # ——本測試只想驗 per-repo 層單獨生效的行為。
        empty_home = tempfile.TemporaryDirectory()
        self.addCleanup(empty_home.cleanup)

        defaults = {
            'search_dirs': list(todo_audit.SEARCH_DIRS),
            'scan_exts': list(todo_audit.SCAN_EXTS),
        }
        config, provenance, warnings = todo_config.load_config(
            REPO_ROOT, defaults, home=Path(empty_home.name))

        self.assertFalse(warnings, f'載入 repo config 不應產生警告：{warnings}')
        self.assertEqual(
            provenance['search_dirs'], 'per-repo',
            'search_dirs 應該由 repo 根目錄的 .claude/todo-audit.json 決定，'
            '不是內建的 BTSE Gradle 路徑',
        )

        files, _by_name = todo_audit.collect_source_files(REPO_ROOT, config=config)
        self.assertGreater(
            len(files), 1,
            f'掃到 {len(files)} 個檔案 —— 套用 repo config 後命中數應該遠大於 1'
            '（沿用內建 SEARCH_DIRS 時，本 repo 沒有那些 Gradle 目錄，命中數固定卡在 1）',
        )


class TestConfigFileIsNotGitignored(RepoAuditConfigFixture):
    """REQ-6：`.claude/todo-audit.json` 必須能進版控，不能被 `.gitignore:34`
    的 `.claude/` 規則擋住。

    背景（`.castpower/todo-audit-testability/requirements.md` REQ-6）：
    Stage 4b 完成後對帳發現這個檔在本機存在、測試也全綠，但 `.gitignore:34`
    的 `.claude/` 規則把它擋住了——它永遠不會進入任何 commit，任何 clone
    與 CI 都拿不到它，`WEAK_AUDIT` 依舊存在。這是回歸保護測試：防的是未來
    有人再次把這個檔 ignore 掉，而 REQ-5 的其他測試在本機依然全綠、完全
    察覺不到。

    這裡刻意只跑唯讀的 `git check-ignore` / `git status --porcelain` 查詢，
    不 `git add`、不 `git commit`、不改 `.gitignore`。
    """

    # REQ-6
    def test_config_file_is_not_git_ignored(self):
        # git check-ignore 的語意：exit 0 = 該路徑「被忽略」（我們不要的
        # 結果，代表白名單例外還沒加），exit 1（或其他非零）= 未被忽略。
        # 斷言方向：非零才是我們要的行為，不能寫成 assertEqual(...,0)。
        self._require_config_path()
        result = subprocess.run(
            ['git', 'check-ignore', '-q', str(CONFIG_PATH)],
            cwd=str(REPO_ROOT),
        )
        self.assertNotEqual(
            result.returncode, 0,
            f'{CONFIG_PATH} 被 git 忽略（git check-ignore 回傳 0）—— '
            '.gitignore 的 .claude/ 規則需要加上 !.claude/todo-audit.json '
            '白名單例外，否則這份設定檔永遠不會進入任何 commit',
        )

    # REQ-6
    def test_config_file_visible_in_git_status(self):
        # `git status --porcelain` 只列出「與 HEAD/index 有差異」的路徑，
        # 這讓兩種相反狀態映到同一個觀測值（空輸出）：
        #   (a) 已 commit 且工作區乾淨（正確狀態）
        #   (b) 被 gitignore 擋住（缺陷狀態，REQ-6 要防的正是這個）
        # 只看 --porcelain 輸出是否非空，沒辦法分辨這兩者——commit 前是
        # `?? path`（綠），commit 後兩者都是空字串（無法區分，卻都被舊斷言
        # 判為紅或視情況判對）。
        #
        # 改用「已追蹤」或「未追蹤但未被忽略」的聯集作為觀測值：
        #   - tracked（`git ls-files --error-unmatch` 成功）→ 已進版控，通過
        #   - 未 tracked 但 `git status --porcelain` 顯示 `??`
        #     → 尚未 commit，但沒被 gitignore 擋住，通過
        #   - 兩者皆非（未 tracked 且未出現在 status 裡）
        #     → 被 gitignore 擋住，失敗
        # 涵蓋 commit 前、commit 後、被 ignore 三態，且 commit 前後皆綠、
        # 只有被 ignore 時紅。
        self._require_config_path()

        tracked = subprocess.run(
            ['git', 'ls-files', '--error-unmatch', '--', str(CONFIG_PATH)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        ).returncode == 0

        if tracked:
            return

        result = subprocess.run(
            ['git', 'status', '--porcelain', '--', str(CONFIG_PATH)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0,
            f'git status 執行失敗：{result.stderr}',
        )
        self.assertTrue(
            result.stdout.strip().startswith('??'),
            f'{CONFIG_PATH} 既未被 git 追蹤（ls-files 找不到），也沒有以 '
            "未追蹤檔（'??'）的身分出現在 git status --porcelain 輸出中 —— "
            '代表它被 gitignore 擋住，git 完全不認得這個檔案的存在',
        )


if __name__ == '__main__':
    unittest.main()
