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


class TrieNode:
    """域名树节点

    Attributes:
        children: 子节点
        status: 节点状态
        count_proxy: 节点及其子节点中，代理节点的数量
        count_direct: 节点及其子节点中，直连节点的数量
    """

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
        return self.count_proxy == 0 and self.count_direct > 0


class DomainTrie:
    def __init__(self):
        self.root: TrieNode = TrieNode(NodeStatus.BRANCH)

    def insert(self, domain: str, status: NodeStatus):
        """倒序插入域名，例如 a.google.com -> 插入路径: com -> google -> a"""
        parts = reversed(domain.lower().split("."))  # 反转列表
        node = self.root
        path_nodes: List[TrieNode] = [node]  # 插入路径，包含最终节点

        for part in parts:
            if part not in node.children:
                # 默认插入叶子节点
                node.children[part] = TrieNode(NodeStatus.BRANCH)
            # 前往其对应的子节点
            node = node.children[part]
            path_nodes.append(node)

        old_status = node.status
        if old_status == status:
            # 无需更改任何信息
            return
        # 说明新插入的域名更改了状态，需要更新路径上各个节点的计数
        node.status = status
        for nn in path_nodes:
            # 1. 删除旧状态
            if old_status == NodeStatus.PROXY:
                nn.count_proxy -= 1
            elif old_status == NodeStatus.DIRECT:
                nn.count_direct -= 1
            # 2. 添加新状态
            if status == NodeStatus.PROXY:
                nn.count_proxy += 1
            elif status == NodeStatus.DIRECT:
                nn.count_direct += 1

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
        for part in parts:
            if part not in node.children:
                # Trie 中仅存在domain的后缀，无法继续深入匹配
                return last_matched_status
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

    def view_tree(self):
        """查看 Trie 树结构"""

        def dfs(node: TrieNode, path: Deque[str], depth: int = 0):
            if node.count_direct and node.count_proxy:
                msg = ""
            elif node.count_direct:
                msg = "PureDirect"
            elif node.count_proxy:
                msg = "PureProxy"
            else:
                msg = "LEAF"

            LOGGER.debug(
                "| " * depth
                + ".".join(path)
                + " [{}, {}]".format(repr(node.status), msg)
            )
            for txt, each in node.children.items():
                path.appendleft(txt)
                dfs(each, path, depth + 1)
                path.popleft()

        dfs(self.root, deque())

    def compress_and_collect(self):
        """遍历并聚合规则"""
        direct_ok_suffixes: List[str] = list()
        proxy_needed_suffixes: List[str] = list()

        def dfs(node: TrieNode, path: Deque[str]):
            nonlocal direct_ok_suffixes, proxy_needed_suffixes
            # 0. 准备
            if node.count_direct + node.count_proxy == 0:
                # 节点无效（自己是BRANCH，同时其下要么没子节点，要么也都是BRANCH）
                return
            cur_path = ".".join(path)
            #  1. 可以聚合吗？
            aggregated_as: NodeStatus = NodeStatus.BRANCH
            if len(path) > 1:
                # 1.1 可以聚合成直连规则吗？
                if node.is_pure_direct:
                    direct_ok_suffixes.append(cur_path)
                    aggregated_as = NodeStatus.DIRECT
                # 1.2 可以聚合成代理规则吗？
                if node.is_pure_proxy:
                    proxy_needed_suffixes.append(cur_path)
                    aggregated_as = NodeStatus.PROXY
                # 1.3 报告聚合结果
                if aggregated_as != NodeStatus.BRANCH:
                    LOGGER.debug("聚合为{}: {}".format(repr(aggregated_as), cur_path))
                    return
            # 2. 好吧，不能聚合
            # 2.1 节点自己是否对应某条规则？
            match node.status:
                case NodeStatus.DIRECT:
                    direct_ok_suffixes.append(cur_path)
                case NodeStatus.PROXY:
                    proxy_needed_suffixes.append(cur_path)
            # 2.2 递归子节点
            for txt, each in node.children.items():
                path.appendleft(txt)
                dfs(each, path)
                path.popleft()

        # for txt, child in self.root.children.items():
        #     dfs(child, deque([txt]))
        dfs(self.root, deque())
        return direct_ok_suffixes, proxy_needed_suffixes


def load_memo(trie: DomainTrie):
    whitelist_path = Path("whitelist.txt")
    blacklist_path = Path("blacklist.txt")

    def mark_as(pp: Path, stat: NodeStatus):
        nonlocal trie
        if pp.exists() and pp.is_file():
            for each in pp.read_text(encoding="utf-8").splitlines():
                if each:  # 过滤空行
                    trie.insert(each, stat)
        else:
            pp.touch()

    mark_as(whitelist_path, NodeStatus.DIRECT)
    mark_as(blacklist_path, NodeStatus.PROXY)


def write_memo(trie: DomainTrie):
    whitelist, blacklist = trie.compress_and_collect()
    with open("whitelist.txt", "w", encoding="utf-8") as f:
        for each in sorted(whitelist, key=lambda x: (x, -len(x))):
            print(each, file=f)
    with open("blacklist.txt", "w", encoding="utf-8") as f:
        for each in sorted(blacklist, key=lambda x: (x, -len(x))):
            print(each, file=f)


def test():
    trie = DomainTrie()
    trie.insert("www.apple.com.cn", NodeStatus.DIRECT)
    trie.insert("www.google.com", NodeStatus.PROXY)
    trie.insert("account.google.com", NodeStatus.PROXY)
    trie.insert("www.baidu.com", NodeStatus.DIRECT)
    trie.view_tree()
    res = trie.compress_and_collect()
    print(res[0])
    print(res[1])
    print(trie.search("google.com"))


if __name__ == "__main__":
    test()
