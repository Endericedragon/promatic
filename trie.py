import unittest
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Deque, Dict, List

from log_utils import get_logger

LOGGER = get_logger()


class NodeStatus(Enum):
    BRANCH = 0
    DIRECT = 1
    PROXY = 2

    def __repr__(self):
        match self.value:
            case 0:
                return "❔"
            case 1:
                return "✅"
            case 2:
                return "🚀"
            case _:
                return f"NodeStatus({self.value})"


class TrieNode:
    """域名树节点

    Attributes:
        children: 子节点
        status: 节点状态
        count_proxy: 节点及其子节点中，代理节点的数量
        count_direct: 节点及其子节点中，直连节点的数量
    """

    __slots__ = (
        "children",
        "status",
        "count_proxy",
        "count_direct",
    )

    def __init__(self, nstat: NodeStatus):
        self.children: Dict[str, "TrieNode"] = dict()
        self.status = nstat
        self.count_proxy: int = 0
        self.count_direct: int = 0

    @property
    def is_pure_proxy(self) -> bool:
        """判断该节点及其子节点是否全为代理节点"""
        return self.count_proxy > 0 and self.count_direct == 0

    @property
    def is_pure_direct(self) -> bool:
        """判断该节点及其子节点是否全为直连节点"""
        return self.count_direct > 0 and self.count_proxy == 0

    def compress_and_collect(self):
        """遍历并聚合规则"""

        whitelist_suffixes: List[str] = list()
        greylist_suffixes: List[str] = list()
        acc: int = 0

        def dfs(node: TrieNode, path: Deque[str]):
            nonlocal whitelist_suffixes, greylist_suffixes, acc
            # 0. 准备
            if node.count_direct + node.count_proxy == 0:
                # 节点无效（自己是BRANCH，同时其下要么没子节点，要么也都是BRANCH）
                return
            cur_path = ".".join(path)
            #  可以聚合吗？
            if len(path) >= 2:  # 只有二级域名以上才考虑聚合
                if node.status == NodeStatus.DIRECT or node.is_pure_direct:
                    whitelist_suffixes.append(cur_path)
                    acc += 1
                    if node.status == NodeStatus.DIRECT:  # 只有纯净才能免除递归，下同
                        return
                if node.status == NodeStatus.PROXY or node.is_pure_proxy:
                    greylist_suffixes.append(cur_path)
                    acc += 1
                    if node.status == NodeStatus.PROXY:
                        return
            # 递归子节点
            for txt, each in node.children.items():
                path.appendleft(txt)
                dfs(each, path)
                path.popleft()

        dfs(self, deque())
        LOGGER.info(f"[DomainTrie]聚合了{acc}条规则!")
        return whitelist_suffixes, greylist_suffixes


class DomainTrie:
    def __init__(self):
        self.root: TrieNode = TrieNode(NodeStatus.BRANCH)
        self.is_dirty: bool = False
        self.path_whitelist = Path("whitelist.txt")
        self.path_greylist = Path("greylist.txt")
        self.path_blacklist = Path("blacklist.txt")

    def load_and_tag(self, rule_path: Path, ns: NodeStatus):
        """加载规则文件，将域名标记为指定状态"""
        if not rule_path.exists():
            rule_path.parent.mkdir(parents=True, exist_ok=True)
            rule_path.touch(exist_ok=True)
        with open(rule_path, "r", encoding="utf-8") as f:
            while line := f.readline():
                line = line.strip()
                if not line:
                    continue
                self.insert(line, ns)
        return self

    def insert(self, domain: str, status: NodeStatus):
        """倒序插入域名，例如 a.google.com -> 插入路径: com -> google -> a。"""

        parts = reversed(domain.lower().split("."))  # 反转列表
        node = self.root
        path_nodes: List[TrieNode] = [node]  # 插入路径，包含最终节点

        for part in parts:
            if part not in node.children:
                self.is_dirty = True
                # 默认插入叶子节点
                node.children[part] = TrieNode(NodeStatus.BRANCH)
            # 前往其对应的子节点
            node = node.children[part]
            path_nodes.append(node)

        old_status = node.status
        if old_status == status:
            # 无需/不准更改任何信息
            return
        # 说明新插入的域名更改了状态，需要更新路径上各个节点的计数
        self.is_dirty = True
        node.status = status
        for nn in path_nodes:
            # 1. 删除旧状态
            if old_status == NodeStatus.DIRECT:
                nn.count_direct -= 1
            elif old_status == NodeStatus.PROXY:
                nn.count_proxy -= 1
            # 2. 添加新状态
            if status == NodeStatus.DIRECT:
                nn.count_direct += 1
            elif status == NodeStatus.PROXY:
                nn.count_proxy += 1

    def search(self, domain: str) -> NodeStatus:
        """搜索域名，返回其匹配或聚合后的状态

        - 若 domain 精确匹配已有记录，返回该记录的状态
        - 若 domain 是 Trie 中某记录的子域名（Trie 记录是 domain 的后缀），继承匹配到的最近非BRANCH父规则（若有）
        - 若 domain 是 Trie 中多条记录的公共后缀，且这些子记录全为代理/直连时，聚合返回对应状态
        - 否则返回 BRANCH
        """

        parts = reversed(domain.lower().split("."))  # 反转列表
        node = self.root
        last_matched_status: NodeStatus = NodeStatus.BRANCH  # 最长匹配到的非BRANCH规则
        for idx, part in enumerate(parts):
            if part not in node.children:
                # Trie 中仅存在domain的后缀，无法继续深入匹配
                # 这里是一个启发式的判断，如果只有最后2截是一样的（例如都以com.cn结尾），则返回BRANCH
                return NodeStatus.BRANCH if idx < 2 else last_matched_status
            node = node.children[part]
            if node.status != NodeStatus.BRANCH:
                last_matched_status = node.status
        # 1. domain完美匹配已有的记录
        if node.status != NodeStatus.BRANCH:
            return node.status
        # 2. domain是Trie中某条记录的后缀
        if node.is_pure_direct:
            return NodeStatus.DIRECT
        elif node.is_pure_proxy:
            return NodeStatus.PROXY
        # 3. 实在没辙
        return last_matched_status


class ClassificationForest:
    def __init__(self) -> None:
        self.path_blacklist = Path("blacklist.txt")
        self.path_whitelist = Path("whitelist.txt")
        self.path_greylist = Path("greylist.txt")

        self.force_proxy_trie = DomainTrie().load_and_tag(
            self.path_blacklist, NodeStatus.PROXY
        )
        self.detect_trie = (
            DomainTrie()
            .load_and_tag(self.path_whitelist, NodeStatus.DIRECT)
            .load_and_tag(self.path_greylist, NodeStatus.PROXY)
        )

    def insert(self, domain: str, status: NodeStatus):
        """探测到哦可直连/需代理的域名时，加入探测树"""
        return self.detect_trie.insert(domain, status)

    def search(self, domain: str) -> NodeStatus:
        """搜索域名，返回其匹配或聚合后的状态"""
        res = self.force_proxy_trie.search(domain)
        if res != NodeStatus.BRANCH:
            return res
        return self.detect_trie.search(domain)

    def __save_memo(self):
        """存储探测规则到硬盘"""

        whitelist, greylist = self.detect_trie.root.compress_and_collect()
        with open(self.path_whitelist, "w", encoding="utf-8") as f:
            for each in sorted(whitelist, key=lambda x: (x, -len(x))):
                print(each, file=f)
        with open(self.path_greylist, "w", encoding="utf-8") as f:
            for each in sorted(greylist, key=lambda x: (x, -len(x))):
                print(each, file=f)
        self.detect_trie.is_dirty = False

    def safely_save_memo(self) -> bool:
        """安全存储探测规则到硬盘"""
        if not self.detect_trie.is_dirty:
            return False
        # 1. 备份当前规则
        backup_wlist = self.path_whitelist.rename(
            self.path_whitelist.with_suffix(".bak")
        )
        backup_blist = self.path_greylist.rename(self.path_greylist.with_suffix(".bak"))
        try:
            self.__save_memo()
        except:
            # 2. 恢复备份文件
            backup_wlist.rename(self.path_whitelist)
            backup_blist.rename(self.path_greylist)
            return False
        # 3. 一切如常，删除备份文件
        backup_wlist.unlink()
        backup_blist.unlink()
        return True


class TestForest(unittest.TestCase):
    def test_aggregate(self):
        forest = ClassificationForest()
        forest.insert("a.x.y", NodeStatus.DIRECT)
        forest.insert("b.x.y", NodeStatus.DIRECT)
        assert forest.search("x.y") == NodeStatus.DIRECT

    def test_whitelist(self):
        forest = ClassificationForest()
        assert forest.search("163.com") == NodeStatus.DIRECT
        assert forest.search("arena.ai") == NodeStatus.PROXY
        assert forest.search("non-exists.dummy") == NodeStatus.BRANCH


if __name__ == "__main__":
    unittest.main()
