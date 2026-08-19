from typing import Dict, List, Deque
from sortedcontainers import SortedDict
from enum import Enum
from collections import deque


class NodeStatus(Enum):
    BRANCH = 0
    DIRECT = 1
    PROXY = 2


class TrieNode:
    def __init__(self, nstat: NodeStatus):
        self.children: Dict[str, "TrieNode"] = SortedDict()
        self.status = nstat
        self.has_proxy_child: bool = False
        self.has_direct_child: bool = False


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
        """搜索域名，返回其状态

        如果返回LEAF，说明该域名不存在于 Trie 中
        """
        parts = reversed(domain.lower().split("."))  # 反转列表
        node = self.root
        for part in parts:
            if part not in node.children:
                return NodeStatus.BRANCH
            node = node.children[part]
        return node.status

    def view_tree(self):
        """查看 Trie 树结构"""
        if self.root is None:
            print()
            return

        def dfs(node: TrieNode, path: Deque[str], depth: int = 0):
            print("|-" * depth + ".".join(path), node.status)
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
            # 需要判断`node`的子节点的纯净度，即：
            # 是否全为代理 / 直连节点？
            if node.has_direct_child and node.has_proxy_child:
                # 不纯净
                for txt, each in node.children.items():
                    path.appendleft(txt)
                    dfs(each, path)
                    path.popleft()
            elif node.has_proxy_child:
                # 纯代理节点
                proxy_needed_suffixes.append(".".join(path))
            elif node.has_direct_child:
                # 纯直连节点
                direct_ok_suffixes.append(".".join(path))
            else:
                # 叶子节点
                match node.status:
                    case NodeStatus.PROXY:
                        proxy_needed_suffixes.append(".".join(path))
                    case NodeStatus.DIRECT:
                        direct_ok_suffixes.append(".".join(path))

        for txt, node in self.root.children.items():
            dfs(node, deque([txt]))
        return direct_ok_suffixes, proxy_needed_suffixes


def test():
    trie = DomainTrie()
    trie.insert("www.apple.com.cn", NodeStatus.DIRECT)
    trie.insert("www.google.com", NodeStatus.PROXY)
    trie.insert("account.google.com", NodeStatus.PROXY)
    trie.insert("a.google.com", NodeStatus.DIRECT)
    trie.view_tree()
    res = trie.compress_and_collect()
    print(res[0])
    print(res[1])


if __name__ == "__main__":
    test()
