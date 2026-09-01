"""REQ-5：repo 根目錄需要 `.claude/todo-audit.json`，消除 `todo_audit.py` 的
`⚠️  WEAK_AUDIT` 零命中降級。

背景（REQ-5）：本 repo（純 Python/Bash 工具倉庫）
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

REQ 編號的作用域：本檔的 REQ-n 是「加 per-repo 稽核設定並讓它進版控」那次交付（commit 7b28388）的需求編號。
本 repo 的 REQ 編號**逐檔案局部有效** —— 不同測試檔的 REQ-1 指涉
完全不同的需求，不要跨檔對照。原始需求文件住在交付當下的 castpower
工作目錄（`.castpower/`，被 gitignore 完全排除、不進版控），所以這裡
指的是 **commit**：`git show 7b28388` 永遠查得到，路徑不會。
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

def _find_repo_root(start):
    """從測試檔往上找 repo root —— 認的是 `.git`，不是層數。

    原本寫死 `parents[3]`（tests/ → todo-audit/ → skills/ → repo root），
    等於把 repo 佈局編進測試。任何一層目錄被搬動或多包一層，這個常數就
    指到別的地方去，而失敗訊息會是「設定檔不存在」——指向錯的結論。

    改認 `.git` 的理由：它是 repo root 的定義本身，不是佈局的巧合。
    `Path(__file__).resolve()` 會解開 symlink，所以從
    `~/.claude/skills/todo-audit`（指回本 repo 的 symlink）跑測試時，
    起點仍落在真正的 checkout 裡，往上找得到 `.git`。

    找不到時**回傳 None 而不是猜一個路徑**——由呼叫端決定怎麼報，
    見 `RepoAuditConfigFixture._require_repo_root()`。猜一個路徑會讓
    後續斷言以「設定檔不存在」的形式失敗，把「這裡不是 repo checkout」
    這個真正的原因藏起來。
    """
    for candidate in [start, *start.parents]:
        if (candidate / '.git').exists():
            return candidate
    return None


REPO_ROOT = _find_repo_root(Path(__file__).resolve().parent)
CONFIG_PATH = (REPO_ROOT / '.claude' / 'todo-audit.json') if REPO_ROOT else None


class RepoAuditConfigFixture(unittest.TestCase):
    def _require_repo_root(self):
        """先確認我們真的在一個 repo checkout 裡。

        這個守衛必須排在 `_require_config_path()` 之前：找不到 repo root
        時 `CONFIG_PATH` 是 None，後續斷言會以「設定檔不存在」的形式失敗，
        而真正的原因是「這份測試被從 repo 外的位置跑起來」——兩者的正確
        反應完全不同（前者要去建設定檔，後者要換個地方跑）。

        刻意用 fail 而不是 skipTest：靜默跳過會讓整份保護在部署位置上
        無聲失效，而測試報告仍然是綠的。
        """
        if REPO_ROOT is None:
            self.fail(
                f'從 {Path(__file__).resolve().parent} 往上找不到含 .git 的目錄'
                ' —— 這份測試驗的是 repo 根目錄的 .claude/todo-audit.json，'
                '必須在 repo checkout 裡執行。若你是從複製（而非 symlink）到'
                ' ~/.claude/skills/todo-audit 的部署位置跑的，請改到 repo 內執行。')

    def _require_config_path(self):
        """存在性檢查獨立成一個可重用斷言，讓每個測試方法失敗時的訊息
        都能直接點名「設定檔還不存在」，而不是被後續的 open()/json.loads()
        丟出的 FileNotFoundError 蓋過去。"""
        self._require_repo_root()
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

    # REQ-7（推翻 REQ-5「search_dirs 排除 tests/」的舊決策）
    #
    # 舊測試 test_search_dirs_excludes_tests_directory 鎖的是「設定長什麼
    # 樣」——斷言 search_dirs 的每個目錄都不含 'tests'。把它反過來寫成
    # 「必須包含 tests」同樣是鎖設定值：換個目錄名稱、或用別的方式涵蓋
    # 測試檔，這種寫法一樣會誤報。這裡改斷言「設定達成的行為」：套用
    # 這份設定後，符號索引的命中率不會低到觸發 todo_audit.py:1007 的
    # FATAL 門檻（0.30）。
    #
    # 真實觸發問題的符號來自正式待辦 DB（T-003/T-005/T-006 的錨點指向
    # 測試檔），但正式 DB 是全機唯一的單例，本 task 全程禁止跑
    # `todo_cli.py audit` 去讀它。改用一組「自己構造、但確實存在於本
    # repo」的探針符號來頂替：全部是只定義在 skills/todo-audit/tests/
    # 底下、在 search_dirs 目前涵蓋的其餘目錄（scripts/hooks/docs）
    # 完全不出現的 identifier ——這正是「錨點指向測試檔」這種待辦條目
    # 在符號索引裡的真實形狀，不是憑空編造的假符號。
    #
    # 之所以敢用一組固定的符號名稱而不算「鎖設定值」：這些名稱是
    # __被測物__（本 repo 現有測試碼），不是被斷言的設定值本身；斷言的
    # 落點永遠是 hit_rate 這個比例，不是 search_dirs 的字面內容。
    #
    # 兩種設定下的命中率刻意隔得很開（0% vs 100%，皆遠離 0.30 的
    # 門檻），而不是卡在臨界值附近，這樣測試才不會對「探針symbol剛好
    # 也在別處出現」這種巧合敏感。
    def test_symbols_only_defined_in_tests_dir_clear_fatal_hit_rate_threshold(self):
        self._require_config_path()

        # 每一個都已用 grep 逐一核對過：只出現在 skills/todo-audit/tests/
        # 底下的 .py 檔，在 scripts/、hooks/、docs/ 完全零命中——分別來自
        # 4 個不同的測試檔，避免探針集中在單一檔案而失去代表性。
        probe_symbols = {
            'RepoAuditConfigFixture',                                    # test_repo_audit_config.py
            'TestSetSectionStoreLevel',                                  # test_section.py
            'TestLatestSnapshotTwoStageResolution',                      # test_fixtures_resolution.py
            'test_returns_none_when_neither_real_nor_fixture_exists',    # test_fixtures_resolution.py
            'TestReadCommands',                                          # test_cli.py
        }

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
        sym_index = todo_audit.build_symbol_index(probe_symbols, files)
        hit_rate = len(sym_index) / len(probe_symbols)

        # 0.30 抄自 todo_audit.py:1007 的 FATAL 門檻字面值——那裡是行內
        # 常數，沒有具名常數可 import，所以這裡刻意重複這個數字並用
        # 註解釘住來源。若未來那個門檻改了，這條斷言要跟著同步，否則
        # 兩邊會各說各話（這是本測試唯一的維護代價，已在報告中揭露）。
        FATAL_HIT_RATE_THRESHOLD = 0.30
        self.assertGreaterEqual(
            hit_rate, FATAL_HIT_RATE_THRESHOLD,
            f'探針符號命中率僅 {hit_rate:.0%}（{len(sym_index)}/{len(probe_symbols)}，'
            f'掃描了 {len(files)} 個檔案)——探針符號 {sorted(probe_symbols)} 全部'
            '只定義在 skills/todo-audit/tests/ 底下，模擬的正是真實待辦錨點'
            '指向測試檔的情形（T-003/T-005/T-006）。命中率低於 '
            'todo_audit.py:1007 的 FATAL 門檻代表現在的 search_dirs 排除了'
            '測試目錄，真的跑 `todo_cli.py audit` 會直接中止。'
            '修法：把 skills/todo-audit/tests 加進 .claude/todo-audit.json 的'
            ' search_dirs（並視情況把 scan_exts 對齊測試檔的副檔名）。',
        )


    # REQ-3
    #
    # `skills/todo-audit/SKILL.md` 是本 skill 最長的規格文件，記錄了「血淚
    # 教訓」等長期知識，待辦錨點可能直接指向它。但它落在 skills/todo-audit/
    # 底下、不在 search_dirs 目前列出的 scripts/ 或 tests/ 子目錄內，因此
    # 掃不到——即使 docs/ 底下性質相同（「文件裡含識別字」）的檔案已被涵蓋，
    # 兩者卻是兩套待遇。
    #
    # 斷言的是行為（這個檔案有沒有出現在 collect_source_files() 的回傳
    # 集合裡），不是 search_dirs 的字面內容——理由同上面 :132-155 對
    # test_symbols_only_defined_in_tests_dir_clear_fatal_hit_rate_threshold
    # 的說明：斷言字面值只驗得到「設定長什麼樣」，換一種同樣能涵蓋
    # SKILL.md 的寫法（例如改列更精確的父目錄）測試就會誤報成失敗。
    #
    # 刻意不斷言 len(files) 的精確數字：collect_source_files() 掃的是整個
    # search_dirs 的即時內容，任何人在 scripts/、tests/、hooks/、docs/
    # 底下新增或刪除一個受 scan_exts 涵蓋的檔案，這個數字就會變動——那是
    # 這些目錄的正常演進，不是 REQ-3 這個行為（「SKILL.md 有沒有被掃到」）
    # 該負責看住的事。把它寫成 assertEqual(len(files), <某個絕對數字>)
    # 會在任何一次無關的檔案新增/刪除時誤紅，是恆真斷言的反面——
    # 「恆假」風險同樣違反 REQ-1 要根治的「斷言的對象選錯」問題。
    # 真正該鎖住的不變式，是「在 search_dirs 裡加入 SKILL.md 這條設定，
    # 前後 collect_source_files() 回傳集合的差集恰好是
    # skills/todo-audit/SKILL.md 這一個檔」——用差集而非絕對數字驗證，
    # 才不會被其他目錄的正常增減污染。下面直接用 assertIn 斷言 SKILL.md
    # 本身在回傳集合內，而不是斷言集合大小，就是同一個理由的落地。
    def test_collect_source_files_includes_skill_md(self):
        self._require_config_path()

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
        skill_md = REPO_ROOT / 'skills' / 'todo-audit' / 'SKILL.md'
        self.assertIn(
            skill_md, files,
            f'{skill_md} 不在 collect_source_files() 回傳的符號掃描集合內 —— '
            '.claude/todo-audit.json 的 search_dirs 需要新增 '
            '"skills/todo-audit/SKILL.md" 這個精確檔案路徑（REQ-3）。'
            '它是本 skill 最長的規格文件，待辦錨點指向它時目前驗不出來，'
            '而性質相同的 docs/ 底下文件已經被涵蓋，兩者不該有不同待遇。',
        )

    def test_skill_md_entry_widens_scope_by_exactly_one_file(self):
        """加入 SKILL.md 那筆設定，掃描集合只能多這一個檔 —— 不多不少。

        上面那條只驗「SKILL.md 有進集合」，驗不到「有沒有夾帶納入別的東西」。
        兩者是不同的性質：把 search_dirs 裡的 `skills/todo-audit/scripts`
        放寬成 `skills/todo-audit` 目錄，SKILL.md 一樣會進集合，上面那條
        照樣綠 —— 但那是被明確排除的做法（範圍變動大於需求，且未來有人在
        `skills/todo-audit/` 頂層新增檔案時會被無感納入）。

        這裡比對「含該筆設定」與「不含」兩種 config 的回傳集合，斷言差集
        **恰好**是 SKILL.md 一個檔。刻意不斷言集合大小的絕對數字——那會隨
        任何無關的檔案增刪而誤紅，是「恆假斷言」，同樣是斷言對象選錯。
        """
        self._require_config_path()

        empty_home = tempfile.TemporaryDirectory()
        self.addCleanup(empty_home.cleanup)
        defaults = {
            'search_dirs': list(todo_audit.SEARCH_DIRS),
            'scan_exts': list(todo_audit.SCAN_EXTS),
        }
        config, _prov, _warn = todo_config.load_config(
            REPO_ROOT, defaults, home=Path(empty_home.name))

        skill_md_entry = 'skills/todo-audit/SKILL.md'
        self.assertIn(
            skill_md_entry, config['search_dirs'],
            f'search_dirs 應包含 {skill_md_entry}（REQ-3）')

        # 只在記憶體裡拿掉那一筆，不動磁碟上的設定檔
        without = dict(config)
        without['search_dirs'] = [d for d in config['search_dirs']
                                  if d != skill_md_entry]

        with_files, _ = todo_audit.collect_source_files(REPO_ROOT, config=config)
        without_files, _ = todo_audit.collect_source_files(REPO_ROOT, config=without)

        added = set(with_files) - set(without_files)
        removed = set(without_files) - set(with_files)
        skill_md = REPO_ROOT / 'skills' / 'todo-audit' / 'SKILL.md'

        self.assertEqual(
            added, {skill_md},
            f'加入 {skill_md_entry} 後多掃到的檔案應該恰好是 SKILL.md，'
            f'實際多了 {sorted(str(p) for p in added)} —— 若這裡出現其他檔案，'
            '代表 search_dirs 那一筆被改成了涵蓋範圍更大的路徑（例如放寬成'
            ' skills/todo-audit 目錄），那是 REQ-3 明確排除的做法。')
        self.assertFalse(
            removed,
            f'加入一筆 search_dirs 不該讓任何檔案消失，卻少了 '
            f'{sorted(str(p) for p in removed)}')


class TestConfigFileIsNotGitignored(RepoAuditConfigFixture):
    """REQ-6：`.claude/todo-audit.json` 必須能進版控，不能被 `.gitignore:34`
    的 `.claude/` 規則擋住。

    背景（REQ-6）：
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
