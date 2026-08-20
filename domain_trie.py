from collections import deque
from enum import Enum
from pathlib import Path
from typing import Deque, Dict, List

from sortedcontainers import SortedDict
import logging


class NodeStatus(Enum):
    BRANCH = 0
    DIRECT = 1
    PROXY = 2

    def __repr__(self):
        return self.name


class TrieNode:
    def __init__(self, nstat: NodeStatus):
        self.children: Dict[str, "TrieNode"] = SortedDict()
        self.status = nstat
        self.has_proxy_child: bool = False
        self.has_direct_child: bool = False

    def is_pure(self) -> bool:
        """判断该节点的子节点是否为纯代理/直连节点"""
        return self.has_proxy_child != self.has_direct_child or (
            not self.has_direct_child and not self.has_proxy_child
        )


class DomainTrie:
    def __init__(self):
        self.root: TrieNode = TrieNode(NodeStatus.BRANCH)

    def insert(self, domain: str, status: NodeStatus):
        """倒序插入域名，例如 a.google.com -> 插入路径: com -> google -> a"""
        parts = reversed(domain.lower().split("."))  # 反转列表
        node = self.root
        for part in parts:
            if part not in node.children:
                # 默认插入叶子节点
                node.children[part] = TrieNode(NodeStatus.BRANCH)
            # 该节点下是否全为代理/直连节点？
            if status == NodeStatus.DIRECT:
                node.has_direct_child = True
            elif status == NodeStatus.PROXY:
                node.has_proxy_child = True
            node = node.children[part]

        node.status = status

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
        if node.is_pure():
            return NodeStatus.PROXY if node.has_proxy_child else NodeStatus.DIRECT
        return last_matched_status

    def view_tree(self):
        """查看 Trie 树结构"""
        def dfs(node: TrieNode, path: Deque[str], depth: int = 0):
            if node.has_direct_child and node.has_proxy_child:
                msg = ""
            elif node.has_direct_child:
                msg = "PureDirect"
            elif node.has_proxy_child:
                msg = "PureProxy"
            else:
                msg = "LEAF"

            logging.debug(
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
            if node.is_pure() and len(path) >= 2:
                # 如果path长度仅为1，那也太宽泛了
                cur_path = ".".join(path)
                logging.info("聚合规则: {}".format(cur_path))
                if node.has_direct_child:
                    direct_ok_suffixes.append(cur_path)
                else:
                    proxy_needed_suffixes.append(cur_path)
            else:
                cur_path = ".".join(path)
                match node.status:
                    case NodeStatus.DIRECT:
                        direct_ok_suffixes.append(cur_path)
                    case NodeStatus.PROXY:
                        proxy_needed_suffixes.append(cur_path)
                for txt, each in node.children.items():
                    path.appendleft(txt)
                    dfs(each, path)
                    path.popleft()

        for txt, child in self.root.children.items():
            dfs(child, deque([txt]))
        return direct_ok_suffixes, proxy_needed_suffixes


def load_memo(trie: DomainTrie):
    whitelist_path = Path("whitelist.txt")
    blacklist_path = Path("blacklist.txt")

    def mark_as(pp: Path, stat: NodeStatus):
        nonlocal trie
        if pp.exists() and pp.is_file():
            for each in pp.read_text(encoding="utf-8").splitlines():
                trie.insert(each, stat)
        else:
            pp.touch()

    mark_as(whitelist_path, NodeStatus.DIRECT)
    mark_as(blacklist_path, NodeStatus.PROXY)


def write_memo(trie: DomainTrie):
    whitelist, blacklist = trie.compress_and_collect()
    with open("whitelist.txt", "w", encoding="utf-8") as f:
        for each in whitelist:
            print(each, file=f)
    with open("blacklist.txt", "w", encoding="utf-8") as f:
        for each in blacklist:
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
