from typing import Dict, List, Deque
from sortedcontainers import SortedDict
from enum import Enum
from collections import deque


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
        """搜索域名，返回其聚合后的状态

        Returns:
            - 若domain完全匹配已有的记录，返回该记录的状态
            - 否则，检查以domain为后缀的所有记录，若这些记录均为代理/直连，则返回该结果
            - 否则，返回 BRANCH
        """
        parts = reversed(domain.lower().split("."))  # 反转列表
        node = self.root
        for part in parts:
            if part not in node.children:
                return NodeStatus.BRANCH
            node = node.children[part]
            if node.status == NodeStatus.PROXY:
                return NodeStatus.PROXY
        if node.is_pure():
            return NodeStatus.PROXY if node.has_proxy_child else NodeStatus.DIRECT
        return node.status

    def view_tree(self):
        """查看 Trie 树结构"""
        if self.root is None:
            print()
            return

        def dfs(node: TrieNode, path: Deque[str], depth: int = 0):
            if node.has_direct_child and node.has_proxy_child:
                msg = ""
            elif node.has_direct_child:
                msg = "PureDirect"
            elif node.has_proxy_child:
                msg = "PureProxy"
            else:
                msg = "LEAF"

            print(
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
            if node.is_pure():
                cur_path = ".".join(path)
                print("聚合规则: {}".format(cur_path))
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
