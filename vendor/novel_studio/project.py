"""importer.py 需要的最小替身（本專案自己寫的，不是 novel-studio 的複本）。

novel-studio 的 ``project.py`` 綁著它的資料夾模型與中文 key，本專案不使用（見 VENDORED.md）。
importer.py 只會從這裡取兩樣東西：``ProjectError`` 與 ``count_chars``，後者的算法與原版相同
（中日文算字、英文為主的稿子算詞）。
"""
import re

CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]")


class ProjectError(Exception):
    pass


def count_chars(text):
    body = re.sub(r"^#.*$", "", text or "", flags=re.M)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    cjk = len(CJK.findall(body))
    words = len(re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*", body))
    return words if words > cjk else cjk
